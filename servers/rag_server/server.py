"""
rag_server - Phase 1 of the Sovereign MCP Platform.

Exposes one MCP tool, `search_documents`, doing naive keyword search over
data/raw_docs/. Phase 2 swaps the scoring below for ChromaDB + bge-small-en-v1.5
vector retrieval; the tool name and signature are deliberately kept stable so
the agent-facing contract does not change when that lands (see HLD.md section 9).

Transport: stdio.

IMPORTANT: on stdio transport, stdout *is* the MCP wire. Never print() to stdout
from this process - a single stray line corrupts the JSON-RPC stream and the
client drops the connection. All diagnostics go to stderr via _log().
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from mcp.server import MCPServer

# --- configuration ---------------------------------------------------------

# Containers bind-mount the project's ./data at /app/data (HLD.md section 5).
# Locally we resolve relative to this file, so the server behaves identically
# whether it is run from the venv or from inside the image.
_DEFAULT_RAW_DOCS = Path(__file__).resolve().parents[2] / "data" / "raw_docs"
RAW_DOCS_DIR = Path(os.environ.get("RAW_DOCS_DIR", _DEFAULT_RAW_DOCS))

# Phase 1 is plain keyword matching, so only text-native files are searchable.
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".json"}
# These need docs_server (unstructured + pytesseract), which arrives in Phase 4.
BINARY_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}

MAX_RESULTS = 5
SNIPPET_CHARS = 300

server = MCPServer(
    name="rag_server",
    version="0.1.0",
    instructions=(
        "Keyword search over the local raw-document corpus. "
        "Phase 1: exact term matching only, no semantic similarity."
    ),
)


def _log(message: str) -> None:
    """Diagnostics to stderr only - stdout belongs to the MCP protocol."""
    print(f"[rag_server] {message}", file=sys.stderr, flush=True)


def _load_documents() -> tuple[list[tuple[Path, str]], list[Path]]:
    """Return (readable text documents, files skipped as non-text)."""
    docs: list[tuple[Path, str]] = []
    skipped: list[Path] = []

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
        except OSError as exc:  # unreadable file should not kill the whole search
            _log(f"skipping unreadable file {path}: {exc}")

    return docs, skipped


def _snippet(text: str, position: int) -> str:
    """A single-line excerpt of text centred on position."""
    half = SNIPPET_CHARS // 2
    start = max(0, position - half)
    end = min(len(text), position + half)
    excerpt = " ".join(text[start:end].split())
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{excerpt}{suffix}"


@server.tool(
    name="search_documents",
    description=(
        "Keyword-search the local document corpus in data/raw_docs. "
        "Returns up to 5 matching excerpts, best match first. "
        "Matching is literal whole-word matching, not semantic - a query that "
        "shares no words with the corpus will return no results."
    ),
)
def search_documents(query: str) -> list[str]:
    """Naive keyword search over data/raw_docs.

    Args:
        query: Free-text query. Split into whole words; documents are ranked by
            how many distinct query terms they contain, then by total hit count.

    Returns:
        Up to MAX_RESULTS formatted excerpts. If nothing matches, a single
        explanatory string rather than an empty list, so the calling model gets
        a usable signal instead of silence.
    """
    if not RAW_DOCS_DIR.is_dir():
        _log(f"corpus directory not found: {RAW_DOCS_DIR}")
        return [f"No document corpus found at {RAW_DOCS_DIR}."]

    terms = re.findall(r"\w+", query.lower())
    if not terms:
        return ["Empty query - provide at least one search word."]

    documents, skipped = _load_documents()
    _log(f"query={query!r} terms={terms} docs={len(documents)} skipped={len(skipped)}")

    if not documents:
        note = f"No searchable text documents in {RAW_DOCS_DIR}."
        if skipped:
            note += (
                f" {len(skipped)} non-text file(s) were ignored - PDF/image"
                " extraction arrives with docs_server in Phase 4."
            )
        return [note]

    scored: list[tuple[int, int, Path, str]] = []
    for path, text in documents:
        lowered = text.lower()
        hits = 0
        matched_terms = 0
        first_position: int | None = None

        for term in set(terms):
            positions = [m.start() for m in re.finditer(rf"\b{re.escape(term)}\b", lowered)]
            if positions:
                matched_terms += 1
                hits += len(positions)
                if first_position is None or positions[0] < first_position:
                    first_position = positions[0]

        if matched_terms:
            scored.append((matched_terms, hits, path, _snippet(text, first_position or 0)))

    if not scored:
        note = f"No matches for {query!r} across {len(documents)} document(s)."
        if skipped:
            note += (
                f" Note: {len(skipped)} non-text file(s) were not searched"
                " (PDF/image extraction arrives in Phase 4)."
            )
        return [note]

    # Rank by distinct terms matched first, then raw hit count, then name for
    # a stable ordering across runs.
    scored.sort(key=lambda row: (-row[0], -row[1], row[2].name))

    results: list[str] = []
    for matched_terms, hits, path, excerpt in scored[:MAX_RESULTS]:
        relative = path.relative_to(RAW_DOCS_DIR)
        results.append(
            f"{relative} [{matched_terms}/{len(set(terms))} terms, {hits} hits]\n{excerpt}"
        )

    _log(f"returning {len(results)} of {len(scored)} matching document(s)")
    return results


if __name__ == "__main__":
    _log(f"starting on stdio; corpus={RAW_DOCS_DIR}")
    server.run("stdio")
