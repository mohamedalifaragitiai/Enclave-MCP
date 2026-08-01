"""Manual MCP client - Phase 4 acceptance check for docs_server.

docs_server runs in a container (Tesseract cannot be installed on the Windows
host without writing to C:\\Program Files), so the MCP host spawns it as:

    wsl -d DockerEngine -- docker run --rm -i -v ... docs_server:0.1.0

which is still plain stdio transport - the container's stdin/stdout are the MCP
wire, exactly as with a local process.

The key assertion is that text unique to the scanned fixture comes back from
parse_document. That page has no text layer at all (pdfminer extracts zero
characters), so any recovered text can only have come through Tesseract.

Run:  .venv\\Scripts\\python.exe scripts\\verify_phase4.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCANNED_FIXTURE = "scanned_osha_hotwork.pdf"

DOCS_SERVER_PARAMS = StdioServerParameters(
    command="wsl.exe",
    args=[
        "-d", "DockerEngine", "--",
        "docker", "run", "--rm", "-i",
        "-v", "/mnt/m/MCP_Project/data:/app/data",
        "docs_server:0.1.0",
    ],
)

# Phrases present only on the scanned page - proof OCR did the work.
OCR_MARKERS = ["hot work", "emergency", "trade secrets"]


def _rule(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _text_of(result) -> str:
    return "\n".join(
        t for t in (getattr(b, "text", None) for b in result.content) if t
    )


async def main() -> int:
    failures: list[str] = []

    async with stdio_client(DOCS_SERVER_PARAMS) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            _rule("1. initialize handshake (through wsl -> docker run -i)")
            init = await session.initialize()
            print(f"server  : {init.server_info.name} v{init.server_info.version}")
            print(f"protocol: {init.protocol_version}")

            _rule("2. tools/list")
            tools = (await session.list_tools()).tools
            for tool in tools:
                print(f"tool: {tool.name}")
            expected = {"list_documents", "parse_document", "ocr_image", "chunk_document"}
            missing = expected - {t.name for t in tools}
            if missing:
                failures.append(f"missing tools: {sorted(missing)}")

            _rule("3. list_documents")
            listing = _text_of(await session.call_tool("list_documents", {}))
            print(listing)
            if SCANNED_FIXTURE not in listing:
                failures.append(f"{SCANNED_FIXTURE} not listed by list_documents")

            _rule("4. parse_document on the scanned page (OCR fallback)")
            parsed = _text_of(await session.call_tool(
                "parse_document", {"filename": SCANNED_FIXTURE}
            ))
            print(parsed)
            lowered = parsed.lower()
            for marker in OCR_MARKERS:
                if marker not in lowered:
                    failures.append(f"OCR did not recover expected phrase {marker!r}")

            _rule("5. path traversal must be refused")
            escaped = _text_of(await session.call_tool(
                "parse_document", {"filename": "../../../etc/passwd"}
            ))
            print(escaped)
            if "root:" in escaped:
                failures.append("path traversal was NOT blocked")

            _rule("6. chunk_document produces ingestion-ready pieces")
            chunks = await session.call_tool(
                "chunk_document", {"filename": SCANNED_FIXTURE}
            )
            blocks = [b for b in chunks.content if getattr(b, "text", None)]
            print(f"chunks returned: {len(blocks)}")
            if blocks:
                print(f"\n--- first chunk ---\n{blocks[0].text[:400]}")
            else:
                failures.append("chunk_document returned nothing")

    _rule("7. multi-server routing: merged namespace + cross-server dispatch")
    sys.path.insert(0, str(PROJECT_ROOT / "agent"))
    import host  # noqa: E402  (imported here so steps 1-6 run without langchain)

    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        router, tool_specs, instructions, prompt_session = await host.connect_servers(
            stack, [host.rag_server_spec(), host.docs_server_spec()]
        )

        print(f"merged tools : {sorted(router)}")
        print(f"servers      : {sorted({b.server for b in router.values()})}")

        owners = {name: b.server for name, b in router.items()}
        if owners.get("search_documents") != "rag_server":
            failures.append("search_documents did not route to rag_server")
        if owners.get("parse_document") != "docs_server":
            failures.append("parse_document did not route to docs_server")
        if len({b.server for b in router.values()}) < 2:
            failures.append("only one server present in the routing table")

        # Dispatch to each server through the single router, proving the host
        # sends each call to whichever server advertised it. Driven directly
        # rather than via the model, whose tool choice is not deterministic.
        rag_out = await host.execute_tool_call(
            router, "search_documents", {"query": "mechanical integrity"}
        )
        docs_out = await host.execute_tool_call(
            router, "parse_document", {"filename": SCANNED_FIXTURE}
        )
        print(f"\nrag_server  -> {len(rag_out)} chars")
        print(f"docs_server -> {len(docs_out)} chars")

        if "chunk_id" not in rag_out:
            failures.append("routed rag_server call did not return search results")
        if "hot work" not in docs_out.lower():
            failures.append("routed docs_server call did not return OCR text")

        # rag_server must disclose the file it cannot see, so the host is not
        # forced to hardcode knowledge of what docs_server exists for.
        if "NOT in this search index" not in rag_out:
            failures.append("rag_server did not disclose unindexed corpus files")

    _rule("RESULT")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PHASE 4 VERIFICATION COMPLETE - all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
