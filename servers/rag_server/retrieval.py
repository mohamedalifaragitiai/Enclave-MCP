"""Shared retrieval layer for rag_server (Phase 2).

Owns every decision that both the ingest script and the MCP server must agree
on: where the corpus and vector store live, how documents are chunked, what the
chunk IDs look like, and which embedding model is used. Keeping this in one
module means scripts/ingest.py and servers/rag_server/server.py cannot drift
into producing/expecting different chunk IDs or vector dimensions.

Embeddings: BAAI/bge-small-en-v1.5 (HLD.md section 6) served through fastembed's
ONNX Runtime rather than sentence-transformers, which would pull PyTorch and
breach the image-size budget in resource-governance.md section 2.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Containers bind-mount ./data at /app/data, so both are overridable by env.
RAW_DOCS_DIR = Path(os.environ.get("RAW_DOCS_DIR", PROJECT_ROOT / "data" / "raw_docs"))
CHROMA_DIR = Path(os.environ.get("CHROMA_DIR", PROJECT_ROOT / "data" / "chroma_db"))
FASTEMBED_CACHE = os.environ.get(
    "FASTEMBED_CACHE_PATH", str(PROJECT_ROOT / ".cache" / "fastembed")
)

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
COLLECTION_NAME = "documents"

# Phase 1 keyword search is retired, but the file-type split still applies:
# PDFs and images need docs_server (unstructured + pytesseract) in Phase 4.
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".json"}
BINARY_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}

# ~1000 chars keeps chunks well inside bge-small's 512-token window while
# staying large enough to carry a coherent idea.
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 150

_embedder = None


def log(message: str) -> None:
    """Diagnostics to stderr only - stdout carries the MCP JSON-RPC stream."""
    print(f"[rag_server] {message}", file=sys.stderr, flush=True)


def iter_documents() -> tuple[list[tuple[Path, str]], list[Path]]:
    """Return (readable text documents, files skipped as non-text)."""
    docs: list[tuple[Path, str]] = []
    skipped: list[Path] = []

    if not RAW_DOCS_DIR.is_dir():
        return docs, skipped

    for path in sorted(RAW_DOCS_DIR.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in BINARY_SUFFIXES:
            skipped.append(path)
            continue
        if suffix not in TEXT_SUFFIXES:
            continue
        try:
            docs.append((path, path.read_text(encoding="utf-8", errors="replace")))
        except OSError as exc:
            log(f"skipping unreadable file {path}: {exc}")

    return docs, skipped


def _split_long(paragraph: str) -> list[str]:
    """Hard-split a paragraph that alone exceeds the chunk size."""
    pieces: list[str] = []
    start = 0
    step = CHUNK_CHARS - CHUNK_OVERLAP
    while start < len(paragraph):
        pieces.append(paragraph[start : start + CHUNK_CHARS])
        start += step
    return pieces


def chunk_text(text: str) -> list[str]:
    """Split text into overlapping, paragraph-aware chunks.

    Paragraphs are packed together up to CHUNK_CHARS. Each new chunk carries the
    trailing CHUNK_OVERLAP characters of the previous one, so a sentence
    straddling a boundary is still retrievable from at least one chunk.
    """
    paragraphs: list[str] = []
    for raw in re.split(r"\n\s*\n", text):
        stripped = raw.strip()
        if not stripped:
            continue
        if len(stripped) > CHUNK_CHARS:
            paragraphs.extend(_split_long(stripped))
        else:
            paragraphs.append(stripped)

    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) + 2 > CHUNK_CHARS:
            chunks.append(current)
            tail = current[-CHUNK_OVERLAP:] if CHUNK_OVERLAP else ""
            current = f"{tail}\n\n{para}".strip() if tail else para
        else:
            current = f"{current}\n\n{para}" if current else para

    if current:
        chunks.append(current)
    return chunks


def make_chunk_id(path: Path, index: int) -> str:
    """Stable, human-readable chunk ID, also usable inside a chunk:// URI.

    Path separators are flattened to '__' so the ID stays a single URI path
    segment even when the corpus grows subdirectories.
    """
    relative = path.relative_to(RAW_DOCS_DIR).as_posix().replace("/", "__")
    return f"{relative}::{index}"


def get_embedder():
    """Lazily construct the ONNX embedder (first call downloads the weights)."""
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding

        log(f"loading embedding model {EMBED_MODEL} (cache={FASTEMBED_CACHE})")
        _embedder = TextEmbedding(model_name=EMBED_MODEL, cache_dir=FASTEMBED_CACHE)
    return _embedder


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts into plain Python lists (Chroma wants lists)."""
    return [vector.tolist() for vector in get_embedder().embed(texts)]


def get_collection(create: bool = False):
    """Open the persistent Chroma collection.

    embedding_function=None is deliberate: every vector is computed by fastembed
    and passed in explicitly. If Chroma were left to its default it would try to
    download its own ONNX MiniLM model, which is both a second embedding model
    (resource-governance.md section 3) and network egress (HLD.md section 7).
    """
    import chromadb
    from chromadb.config import Settings

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(
        path=str(CHROMA_DIR),
        settings=Settings(anonymized_telemetry=False),
    )

    if create:
        return client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=None,
            metadata={"hnsw:space": "cosine"},
        )
    return client.get_collection(name=COLLECTION_NAME, embedding_function=None)
