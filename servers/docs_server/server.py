"""
docs_server - Phase 4 of the Sovereign MCP Platform.

The second MCP server: document intelligence over the raw corpus. Where
rag_server answers questions about already-ingested text, this one turns files
that are not yet text - scanned PDFs, images - into text.

Tools:
  list_documents()          - what is in the corpus and whether it needs OCR
  parse_document(filename)  - extract text, choosing text-native or OCR
  ocr_image(filename)       - force OCR on an image file
  chunk_document(filename)  - parse then split, ready for rag_server ingestion

Runs in a container (see Dockerfile): pytesseract needs the Tesseract binary,
which cannot be installed on the Windows host without writing to C:\\Program
Files, forbidden by resource-governance.md section 1.

Transport: stdio.

IMPORTANT: on stdio transport, stdout *is* the MCP wire. Never print() to stdout
from this process - a stray line corrupts the JSON-RPC stream. Diagnostics go to
stderr via _log().
"""

from __future__ import annotations

import argparse
import hmac
import os
import re
import sys
from pathlib import Path

from mcp.server import MCPServer

DOCS_DIR = Path(os.environ.get("DOCS_DIR", "/app/data/raw_docs"))

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".json"}
PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

# A text-native page usually yields far more than this; below it the page is
# almost certainly a scan and OCR is the only way to read it.
MIN_NATIVE_CHARS = 80

# Mirrors retrieval.py in rag_server so chunks handed back for ingestion match
# what that server would have produced itself.
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 150

server = MCPServer(
    name="docs_server",
    version="0.1.0",
    instructions=(
        "Document parsing and OCR over the local corpus. Use list_documents to "
        "see what exists, parse_document to extract text from a PDF or image, "
        "and chunk_document to get ingestion-ready pieces. This server does not "
        "answer questions - it only produces text."
    ),
)


def _log(message: str) -> None:
    """Diagnostics to stderr only - stdout carries the MCP JSON-RPC stream."""
    print(f"[docs_server] {message}", file=sys.stderr, flush=True)


def _resolve(filename: str) -> Path | None:
    """Resolve a corpus-relative filename, refusing anything outside DOCS_DIR.

    The model chooses this argument, so it is untrusted input: '../../etc/passwd'
    must not resolve. Compare resolved paths rather than trusting the string.
    """
    candidate = (DOCS_DIR / filename).resolve()
    try:
        candidate.relative_to(DOCS_DIR.resolve())
    except ValueError:
        _log(f"rejected path outside corpus: {filename!r}")
        return None
    return candidate if candidate.is_file() else None


def _extract_pdf_native(path: Path) -> str:
    """Extract embedded text from a PDF without rendering it."""
    from pdfminer.high_level import extract_text

    try:
        return (extract_text(str(path)) or "").strip()
    except Exception as exc:
        _log(f"pdfminer failed on {path.name}: {exc}")
        return ""


def _ocr_pdf(path: Path) -> str:
    """Rasterise each page and OCR it - the scanned-document path."""
    from pdf2image import convert_from_path
    import pytesseract

    pages = convert_from_path(str(path), dpi=300)
    _log(f"OCR over {len(pages)} rasterised page(s) of {path.name}")
    texts = []
    for number, image in enumerate(pages, start=1):
        texts.append(f"--- page {number} ---\n{pytesseract.image_to_string(image).strip()}")
    return "\n\n".join(texts).strip()


def _ocr_image_file(path: Path) -> str:
    import pytesseract
    from PIL import Image

    with Image.open(path) as image:
        return pytesseract.image_to_string(image).strip()


def _chunk(text: str) -> list[str]:
    """Paragraph-aware chunking, matching rag_server's retrieval.py."""
    paragraphs: list[str] = []
    for raw in re.split(r"\n\s*\n", text):
        stripped = raw.strip()
        if not stripped:
            continue
        if len(stripped) > CHUNK_CHARS:
            start = 0
            step = CHUNK_CHARS - CHUNK_OVERLAP
            while start < len(stripped):
                paragraphs.append(stripped[start : start + CHUNK_CHARS])
                start += step
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


@server.tool(
    name="list_documents",
    description=(
        "List files in the raw document corpus with their type and whether they "
        "will need OCR. Call this first when unsure what is available."
    ),
)
def list_documents() -> list[str]:
    """Inventory the corpus so the agent can pick a real filename."""
    if not DOCS_DIR.is_dir():
        return [f"No corpus directory at {DOCS_DIR}."]

    entries: list[str] = []
    for path in sorted(DOCS_DIR.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in TEXT_SUFFIXES:
            kind = "text (already readable)"
        elif suffix in PDF_SUFFIXES:
            kind = "pdf (native text or scanned)"
        elif suffix in IMAGE_SUFFIXES:
            kind = "image (needs OCR)"
        else:
            continue
        relative = path.relative_to(DOCS_DIR).as_posix()
        entries.append(f"{relative} [{kind}, {path.stat().st_size} bytes]")

    return entries or [f"No parseable documents in {DOCS_DIR}."]


@server.tool(
    name="parse_document",
    description=(
        "Extract text from a corpus document by filename. PDFs are tried as "
        "text-native first and fall back to OCR automatically when the page "
        "turns out to be a scan. Images always go through OCR."
    ),
)
def parse_document(filename: str) -> str:
    """Extract text, choosing the cheapest strategy that actually works."""
    path = _resolve(filename)
    if path is None:
        return f"No such document in the corpus: {filename!r}. Try list_documents."

    suffix = path.suffix.lower()

    if suffix in TEXT_SUFFIXES:
        _log(f"{path.name}: reading as plain text")
        return path.read_text(encoding="utf-8", errors="replace")

    if suffix in IMAGE_SUFFIXES:
        _log(f"{path.name}: OCR (image)")
        text = _ocr_image_file(path)
        return text or f"OCR produced no text from {filename!r}."

    if suffix in PDF_SUFFIXES:
        native = _extract_pdf_native(path)
        if len(native) >= MIN_NATIVE_CHARS:
            _log(f"{path.name}: text-native PDF ({len(native)} chars, no OCR needed)")
            return native
        _log(f"{path.name}: only {len(native)} native chars - falling back to OCR")
        text = _ocr_pdf(path)
        return text or f"OCR produced no text from {filename!r}."

    return f"Unsupported file type {suffix!r} for {filename!r}."


@server.tool(
    name="ocr_image",
    description=(
        "Force OCR on an image file in the corpus and return the recognised "
        "text. Use parse_document instead unless you specifically need to skip "
        "the text-native path."
    ),
)
def ocr_image(filename: str) -> str:
    """OCR an image unconditionally."""
    path = _resolve(filename)
    if path is None:
        return f"No such document in the corpus: {filename!r}. Try list_documents."
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        return f"{filename!r} is not an image; use parse_document."

    _log(f"{path.name}: forced OCR")
    return _ocr_image_file(path) or f"OCR produced no text from {filename!r}."


@server.tool(
    name="chunk_document",
    description=(
        "Parse a document and split it into overlapping chunks sized for "
        "embedding, ready to be ingested into the vector store."
    ),
)
def chunk_document(filename: str) -> list[str]:
    """Parse then chunk, so extracted text can flow into rag_server's index."""
    text = parse_document(filename)
    if text.startswith("No such document") or text.startswith("Unsupported file type"):
        return [text]

    chunks = _chunk(text)
    _log(f"{filename}: {len(chunks)} chunk(s)")
    return chunks or [f"No text to chunk in {filename!r}."]


# ---------------------------------------------------------------------------
# Phase 6 - HTTP transport, mirroring the pattern established for rag_server in
# Phase 5.
#
# This is needed because stdio cannot cross a container boundary: it works by a
# parent process owning a child's stdin/stdout, which two separate containers do
# not share. Once each server is its own compose service, MCP has to travel over
# the network. See docs/design.md section 6.
#
# The ~40 lines below are duplicated from rag_server rather than shared. Each
# server is an independently deployable image with its own requirements.txt and
# no common package; introducing a shared library would mean a build-context
# parent directory and coupled release cycles for the sake of one middleware
# class. The duplication is the cheaper trade here.
# ---------------------------------------------------------------------------

API_KEY_ENV = "DOCS_API_KEY"
API_KEY_HEADER = "x-api-key"
MIN_KEY_LENGTH = 16


def _build_http_app(api_key: str, host: str):
    """Wrap the MCP Starlette app with an API-key gate."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class ApiKeyMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            provided = request.headers.get(API_KEY_HEADER, "")
            # Constant-time compare: a plain == leaks key material through
            # timing differences on early-mismatching bytes.
            if not hmac.compare_digest(provided, api_key):
                _log(f"401 {request.method} {request.url.path} (bad or missing API key)")
                return JSONResponse(
                    {"error": "unauthorized", "detail": f"valid {API_KEY_HEADER} required"},
                    status_code=401,
                )
            return await call_next(request)

    app = server.streamable_http_app(host=host)
    app.add_middleware(ApiKeyMiddleware)
    return app


def _run_http(host: str, port: int) -> int:
    import uvicorn

    api_key = os.environ.get(API_KEY_ENV, "")
    # Fail closed rather than starting unauthenticated on a forgotten env var.
    if not api_key:
        _log(f"refusing to start: {API_KEY_ENV} is not set")
        return 2
    if len(api_key) < MIN_KEY_LENGTH:
        _log(f"refusing to start: {API_KEY_ENV} shorter than {MIN_KEY_LENGTH} characters")
        return 2

    _log(f"starting on http://{host}:{port}/mcp (API key required)")
    uvicorn.run(_build_http_app(api_key, host), host=host, port=port, log_level="warning")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="docs_server MCP server.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help="stdio (default) or http (streamable HTTP + API key)",
    )
    # 0.0.0.0 is the correct default *inside a container*: the only routes in are
    # the compose network and explicitly published ports, and nothing is
    # published for this service. On a host this would be wrong.
    parser.add_argument("--host", default=os.environ.get("MCP_HTTP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_HTTP_PORT", "8766")))
    args = parser.parse_args()

    if args.transport == "http":
        return _run_http(args.host, args.port)

    _log(f"starting on stdio; corpus={DOCS_DIR}")
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
