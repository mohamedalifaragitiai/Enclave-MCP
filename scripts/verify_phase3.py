"""Manual acceptance check - Phase 3 LangGraph MCP host.

Verifies the host's three responsibilities without depending on what a 4B model
chooses to do on any given run:

  1. tool discovery       - MCP tools/list becomes OpenAI function specs
  2. resource bridging    - read_chunk translates to an MCP resources/read
  3. grounded reasoning   - a real graph run answers from the corpus, and
                            abstains when the corpus does not cover the question

Step 2 is driven directly through execute_tool_call rather than by prompting the
model, because tool selection on a small model is not deterministic and a test
that depends on it would be flaky.

Run:  .venv\\Scripts\\python.exe scripts\\verify_phase3.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "agent"))

import host  # noqa: E402

from mcp import ClientSession, StdioServerParameters, stdio_client  # noqa: E402


def _rule(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


async def main() -> int:
    failures: list[str] = []

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(host.RAG_SERVER)],
        cwd=str(PROJECT_ROOT),
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()

            _rule("1. tool discovery -> OpenAI function specs")
            tools = (await session.list_tools()).tools
            specs = [host.mcp_tool_to_openai_spec(t) for t in tools]
            for spec in specs:
                print(f"{spec['function']['name']}: {spec['function']['parameters']}")
            if not any(s["function"]["name"] == "search_documents" for s in specs):
                failures.append("search_documents was not converted to a function spec")

            _rule("2. resource bridging - read_chunk -> resources/read")
            templates = (await session.list_resource_templates()).resource_templates
            bridged = any(t.uri_template.startswith("chunk://") for t in templates)
            print(f"chunk:// template advertised: {bridged}")
            if not bridged:
                failures.append("chunk:// resource template not advertised")

            # Get a real chunk_id from a search, then force the bridge directly.
            search_text = await host.execute_tool_call(
                session, "search_documents", {"query": "mechanical integrity"}
            )
            chunk_id = None
            for token in search_text.replace(")", " ").split():
                if "::" in token:
                    chunk_id = token
                    break
            print(f"chunk_id from search: {chunk_id}")

            if chunk_id:
                bridged_text = await host.execute_tool_call(
                    session, host.READ_CHUNK_TOOL, {"chunk_id": chunk_id}
                )
                print(f"\n--- read_chunk({chunk_id}) ---\n{bridged_text}")
                if "source:" not in bridged_text:
                    failures.append("read_chunk did not return chunk contents")
                # The resource must return more than the truncated search preview.
                if len(bridged_text) < 200:
                    failures.append("read_chunk returned suspiciously little text")
            else:
                failures.append("could not extract a chunk_id from search results")

            _rule("3. graph run - grounded question")
            specs.append(host.READ_CHUNK_SPEC)
            app = host.build_graph(session, specs)

            from langchain_core.messages import HumanMessage, SystemMessage

            grounded_q = "What does OSHA require for mechanical integrity?"
            prompt = await session.get_prompt("rag_answer", {"question": grounded_q})
            instruction = "\n\n".join(
                t for t in (getattr(m.content, "text", None) for m in prompt.messages) if t
            )
            result = await app.ainvoke(
                {
                    "messages": [
                        SystemMessage(content=init.instructions or ""),
                        HumanMessage(content=instruction),
                    ],
                    "rounds": 0,
                }
            )
            answer = str(result["messages"][-1].content)
            print(answer)
            tool_used = any(getattr(m, "tool_calls", None) for m in result["messages"])
            if not tool_used:
                failures.append("grounded question did not trigger a tool call")
            if "integrity" not in answer.lower():
                failures.append("grounded answer does not mention the retrieved topic")

            _rule("4. graph run - out-of-corpus question must abstain")
            absent_q = "What was the average rainfall in Muscat in 1997?"
            prompt = await session.get_prompt("rag_answer", {"question": absent_q})
            instruction = "\n\n".join(
                t for t in (getattr(m.content, "text", None) for m in prompt.messages) if t
            )
            result = await app.ainvoke(
                {
                    "messages": [
                        SystemMessage(content=init.instructions or ""),
                        HumanMessage(content=instruction),
                    ],
                    "rounds": 0,
                }
            )
            answer = str(result["messages"][-1].content)
            print(answer)
            abstained = any(
                phrase in answer.lower()
                for phrase in ("does not contain", "not contain", "no information",
                               "does not cover", "not covered", "cannot", "unable")
            )
            print(f"\nabstained: {abstained}")
            if not abstained:
                failures.append("model did not abstain on an out-of-corpus question")

    _rule("RESULT")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PHASE 3 VERIFICATION COMPLETE - all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
