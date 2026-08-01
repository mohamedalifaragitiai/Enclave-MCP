"""
agent/host.py - Phase 3 of the Sovereign MCP Platform.

A LangGraph agent acting as an MCP *host*: it owns the reasoning loop, spawns
rag_server as an MCP client over stdio, and calls the local Qwen3 llama-server
over an OpenAI-compatible HTTP endpoint for every reasoning step.

It consumes all three MCP primitives the server exposes:

  tools     -> discovered via tools/list, converted to OpenAI function specs and
               bound to the model, so the *server* decides what the agent can do
  prompts   -> rag_answer is fetched via prompts/get and used as the task
               instruction, rather than hardcoding a prompt in this file
  resources -> the prompt directs the model to read chunk://<id> when a search
               preview is truncated

The graph is written out explicitly rather than using a prebuilt ReAct agent:
per HLD.md section 6, explicit control over tool routing is the reason LangGraph
was chosen, and an implicit loop would hide exactly the part worth showing.

Run:  .venv\\Scripts\\python.exe agent\\host.py "your question"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Annotated, Any, TypedDict

# LangSmith ships with LangChain and traces to the cloud unless disabled. Set
# before importing langchain so the client never initialises (HLD.md section 7).
os.environ.setdefault("LANGSMITH_TRACING", "false")
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")

from langchain_core.messages import (  # noqa: E402
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI  # noqa: E402
from langgraph.graph import END, StateGraph  # noqa: E402
from langgraph.graph.message import add_messages  # noqa: E402

from mcp import ClientSession, StdioServerParameters, stdio_client  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAG_SERVER = PROJECT_ROOT / "servers" / "rag_server" / "server.py"

# Containers override this to http://host.docker.internal:8001/v1 (HLD.md
# section 2). Either way it must resolve to the single llama-server on 8001.
LLM_ENDPOINT = os.environ.get("LLM_ENDPOINT", "http://localhost:8001/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "Qwen3-4B-Instruct-2507")

# A 4B model can loop on tool calls. Bound it rather than trusting it to stop.
MAX_TOOL_ROUNDS = 4


def log(message: str) -> None:
    """Host diagnostics to stderr; stdout stays clean for the final answer."""
    print(f"[agent] {message}", file=sys.stderr, flush=True)


class AgentState(TypedDict):
    """Graph state: the running transcript plus a tool-round counter."""

    messages: Annotated[list[AnyMessage], add_messages]
    rounds: int


def mcp_tool_to_openai_spec(tool: Any) -> dict[str, Any]:
    """Convert an MCP tool declaration into an OpenAI function spec.

    This is the whole MCP-to-LangChain bridge. The server's JSON Schema is
    passed through untouched, so adding a tool or changing its schema on the
    server side requires no change here - which is the point of MCP.
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.input_schema,
        },
    }


# MCP resources are host-controlled: the model cannot address chunk:// URIs on
# its own. The server's rag_answer prompt tells it to read them, so the host has
# to make that possible - it exposes resource reading as a synthetic tool and
# translates the call back into a resources/read request. The resource stays a
# resource on the server; bridging it is the host's job.
READ_CHUNK_TOOL = "read_chunk"

READ_CHUNK_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": READ_CHUNK_TOOL,
        "description": (
            "Read the full untruncated text of a corpus chunk by its chunk_id, "
            "as returned by search_documents. Use when a search preview is cut "
            "off and the detail matters."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {
                    "type": "string",
                    "description": "chunk_id from a search_documents result",
                }
            },
            "required": ["chunk_id"],
        },
    },
}


async def execute_tool_call(session: ClientSession, name: str, args: dict[str, Any]) -> str:
    """Run one model-requested call against the MCP server, returning its text.

    Kept at module level rather than nested in the graph so the routing - in
    particular the read_chunk -> resources/read translation - can be exercised
    directly by scripts/verify_phase3.py without depending on whether the model
    happens to choose that tool.
    """
    if name == READ_CHUNK_TOOL:
        uri = f"chunk://{args['chunk_id']}"
        log(f"translating to resources/read {uri}")
        read = await session.read_resource(uri)
        blocks = [text for text in (getattr(c, "text", None) for c in read.contents) if text]
    else:
        result = await session.call_tool(name, args)
        blocks = [text for text in (getattr(b, "text", None) for b in result.content) if text]

    return "\n\n".join(blocks) or "(tool returned no content)"


def build_graph(session: ClientSession, tool_specs: list[dict[str, Any]]):
    """Compile the two-node reasoning graph: agent <-> tools."""
    llm = ChatOpenAI(
        base_url=LLM_ENDPOINT,
        api_key="not-needed",  # llama-server ignores it; must be non-empty
        model=LLM_MODEL,
        temperature=0,
        timeout=180,
    )
    llm_with_tools = llm.bind_tools(tool_specs)

    async def call_model(state: AgentState) -> dict[str, Any]:
        response = await llm_with_tools.ainvoke(state["messages"])
        if getattr(response, "tool_calls", None):
            names = [call["name"] for call in response.tool_calls]
            log(f"model requested tool(s): {names}")
        else:
            log("model produced a final answer")
        return {"messages": [response]}

    async def call_tools(state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        outputs: list[ToolMessage] = []

        for call in last.tool_calls:
            log(f"calling MCP tool {call['name']}({call['args']})")
            try:
                content = await execute_tool_call(session, call["name"], call["args"])
            except Exception as exc:
                # Surface the failure to the model instead of crashing the run;
                # it can then answer that retrieval failed.
                log(f"tool {call['name']} failed: {exc}")
                content = f"Tool call failed: {exc}"

            outputs.append(
                ToolMessage(content=content, tool_call_id=call["id"], name=call["name"])
            )

        return {"messages": outputs, "rounds": state.get("rounds", 0) + 1}

    def should_continue(state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            if state.get("rounds", 0) >= MAX_TOOL_ROUNDS:
                log(f"tool-round cap ({MAX_TOOL_ROUNDS}) reached - stopping")
                return END
            return "tools"
        return END

    graph = StateGraph(AgentState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", call_tools)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


async def run(question: str, show_trace: bool) -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(RAG_SERVER)],
        cwd=str(PROJECT_ROOT),
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            log(f"connected to MCP server {init.server_info.name} v{init.server_info.version}")

            tools = (await session.list_tools()).tools
            tool_specs = [mcp_tool_to_openai_spec(tool) for tool in tools]
            log(f"discovered tool(s): {[t.name for t in tools]}")

            # Expose the server's chunk:// resource to the model as a tool.
            templates = (await session.list_resource_templates()).resource_templates
            if any(t.uri_template.startswith("chunk://") for t in templates):
                tool_specs.append(READ_CHUNK_SPEC)
                log(f"bridged resource template chunk:// as tool {READ_CHUNK_TOOL}")

            # The task instruction comes from the server's prompt template, not
            # from this file - the server owns how its corpus should be used.
            prompt = await session.get_prompt("rag_answer", {"question": question})
            instruction = "\n\n".join(
                text
                for text in (getattr(m.content, "text", None) for m in prompt.messages)
                if text
            )

            messages: list[AnyMessage] = []
            if init.instructions:
                messages.append(SystemMessage(content=init.instructions))
            messages.append(HumanMessage(content=instruction))

            app = build_graph(session, tool_specs)
            log(f"invoking graph against {LLM_ENDPOINT} ({LLM_MODEL})")
            final = await app.ainvoke({"messages": messages, "rounds": 0})

    if show_trace:
        print("\n" + "=" * 70)
        print("TRANSCRIPT")
        print("=" * 70)
        for message in final["messages"]:
            kind = type(message).__name__
            if isinstance(message, AIMessage) and message.tool_calls:
                print(f"\n[{kind}] tool_calls: {message.tool_calls}")
            else:
                body = str(message.content)
                if len(body) > 600:
                    body = body[:600] + " ..."
                print(f"\n[{kind}] {body}")

    print("\n" + "=" * 70)
    print("ANSWER")
    print("=" * 70)
    print(final["messages"][-1].content)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LangGraph MCP host over rag_server.")
    parser.add_argument("question", help="question to answer from the local corpus")
    parser.add_argument(
        "--trace", action="store_true", help="print the full message transcript"
    )
    args = parser.parse_args()
    return asyncio.run(run(args.question, args.trace))


if __name__ == "__main__":
    raise SystemExit(main())
