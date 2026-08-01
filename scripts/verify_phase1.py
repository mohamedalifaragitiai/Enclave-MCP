"""Manual MCP client - Phase 1 acceptance check for rag_server.

Spawns servers/rag_server/server.py over stdio exactly as a real MCP host would,
performs the initialize handshake, lists tools, then calls search_documents with
a few representative queries.

Run:  .venv\\Scripts\\python.exe scripts\\verify_phase1.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SCRIPT = PROJECT_ROOT / "servers" / "rag_server" / "server.py"

QUERIES = [
    "process hazard analysis",          # expect the OSHA fixture
    "natural gas electric power",       # expect the EIA fixture
    "measure AI risk",                  # expect the NIST fixture
    "zebra quantum bicycle",            # expect a clean no-match message
]


def _rule(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT)],
        cwd=str(PROJECT_ROOT),
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            _rule("1. initialize handshake")
            init = await session.initialize()
            print(f"server name     : {init.server_info.name}")
            print(f"server version  : {init.server_info.version}")
            print(f"protocol version: {init.protocol_version}")
            print(f"capabilities    : {init.capabilities.model_dump(exclude_none=True)}")

            _rule("2. tools/list")
            tools = await session.list_tools()
            for tool in tools.tools:
                print(f"name       : {tool.name}")
                print(f"description: {tool.description}")
                print(f"inputSchema: {tool.input_schema}")
            assert any(t.name == "search_documents" for t in tools.tools), \
                "search_documents not advertised by the server"

            _rule("3. tools/call - search_documents")
            for query in QUERIES:
                print(f"\n--- query: {query!r} ---")
                result = await session.call_tool("search_documents", {"query": query})
                print(f"is_error: {result.is_error}")
                for block in result.content:
                    text = getattr(block, "text", None)
                    if text is not None:
                        print(text)
                        print("-" * 40)

    _rule("PHASE 1 VERIFICATION COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
