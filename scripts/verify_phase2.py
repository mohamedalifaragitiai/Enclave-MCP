"""Manual MCP client - Phase 2 acceptance check for rag_server.

Exercises all three MCP primitives over stdio: the upgraded vector-search tool,
the chunk:// resource, and the rag_answer prompt template.

The 'paraphrase' query below deliberately shares almost no vocabulary with the
corpus. Phase 1's keyword matcher returned nothing for queries like it; if
Phase 2 returns a sensible passage, retrieval is genuinely semantic.

Run:  .venv\\Scripts\\python.exe scripts\\verify_phase2.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SCRIPT = PROJECT_ROOT / "servers" / "rag_server" / "server.py"

QUERIES = [
    ("literal", "process hazard analysis"),
    ("paraphrase", "keeping machinery in working order to avoid breakdowns"),
    ("paraphrase", "who is accountable for overseeing machine learning risk"),
    ("no-match", "medieval falconry techniques"),
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
            print(f"server  : {init.server_info.name} v{init.server_info.version}")
            print(f"protocol: {init.protocol_version}")

            _rule("2. tools/list")
            for tool in (await session.list_tools()).tools:
                print(f"tool: {tool.name}")

            _rule("3. tools/call - search_documents (semantic)")
            first_chunk_id = None
            for kind, query in QUERIES:
                print(f"\n--- [{kind}] {query!r} ---")
                result = await session.call_tool("search_documents", {"query": query})
                for block in result.content[:2]:
                    text = getattr(block, "text", None)
                    if text:
                        print(text)
                        if first_chunk_id is None:
                            match = re.search(r"chunk_id: ([^)]+)\)", text)
                            if match:
                                first_chunk_id = match.group(1)
                        print("-" * 40)

            _rule("4. resources/templates/list")
            templates = await session.list_resource_templates()
            for template in templates.resource_templates:
                print(f"uri_template: {template.uri_template}  ({template.name})")

            _rule("5. resources/read - chunk://<id>")
            if first_chunk_id:
                uri = f"chunk://{first_chunk_id}"
                print(f"reading: {uri}")
                contents = await session.read_resource(uri)
                for item in contents.contents:
                    text = getattr(item, "text", None)
                    if text:
                        print(f"mime_type: {item.mime_type}")
                        print(text)
            else:
                print("SKIPPED - no chunk_id captured from search results")

            _rule("6. prompts/list + prompts/get - rag_answer")
            for prompt in (await session.list_prompts()).prompts:
                args = [a.name for a in (prompt.arguments or [])]
                print(f"prompt: {prompt.name}  args={args}")

            got = await session.get_prompt(
                "rag_answer", {"question": "What does OSHA require for mechanical integrity?"}
            )
            for message in got.messages:
                text = getattr(message.content, "text", None)
                print(f"\nrole: {message.role}\n{text}")

    _rule("PHASE 2 VERIFICATION COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
