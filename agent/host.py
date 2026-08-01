"""
agent/host.py - Phases 3-4 of the Sovereign MCP Platform.

A LangGraph agent acting as an MCP *host*: it owns the reasoning loop, connects
to one or more MCP servers over stdio, and calls the local Qwen3 llama-server
over an OpenAI-compatible HTTP endpoint for every reasoning step.

Servers (Phase 4 adds the second):
  rag_server  - local process; semantic search over the ingested corpus
  docs_server - container; PDF/OCR parsing of files not yet ingested

Routing is capability-driven, not hardcoded: the host merges both servers'
tools/list into one namespace, hands the merged set to the model, and dispatches
each call back to whichever server advertised it. Adding a third server requires
no change to the reasoning loop.

It consumes all three MCP primitives:

  tools     -> discovered via tools/list, converted to OpenAI function specs and
               bound to the model, so the *servers* decide what the agent can do
  prompts   -> rag_answer is fetched via prompts/get and used as the task
               instruction, rather than hardcoding a prompt in this file
  resources -> chunk:// is host-bridged into a read_chunk tool (resources are
               host-controlled and not model-addressable)

The graph is written out explicitly rather than using a prebuilt ReAct agent:
per HLD.md section 6, explicit control over tool routing is the reason LangGraph
was chosen, and an implicit loop would hide exactly the part worth showing.

Run:  .venv\\Scripts\\python.exe agent\\host.py "your question"
      .venv\\Scripts\\python.exe agent\\host.py "..." --no-docs   (rag only)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
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

# docs_server runs in a container because Tesseract cannot be installed on the
# Windows host without writing to C:\ (resource-governance.md section 1).
DOCS_IMAGE = os.environ.get("DOCS_IMAGE", "docs_server:0.1.0")
DOCS_DATA_MOUNT = os.environ.get("DOCS_DATA_MOUNT", "/mnt/m/MCP_Project/data:/app/data")

# A 4B model can loop on tool calls. Bound it rather than trusting it to stop.
# Multi-server work legitimately needs more rounds (parse, then search).
MAX_TOOL_ROUNDS = 6

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


def log(message: str) -> None:
    """Host diagnostics to stderr; stdout stays clean for the final answer."""
    print(f"[agent] {message}", file=sys.stderr, flush=True)


class AgentState(TypedDict):
    """Graph state: the running transcript plus a tool-round counter."""

    messages: Annotated[list[AnyMessage], add_messages]
    rounds: int


@dataclass
class ServerSpec:
    """An MCP server the host should connect to, over either transport.

    Exactly one of params (stdio) or url (HTTP) is set. The reasoning loop never
    looks at this - only connect_servers does - so the agent behaves identically
    whether a server is a local subprocess or a container across the network.
    """

    name: str
    params: StdioServerParameters | None = None
    url: str | None = None
    api_key: str | None = None

    @property
    def transport(self) -> str:
        return "stdio" if self.params is not None else "http"


@dataclass
class ToolBinding:
    """Where a model-visible tool actually lives."""

    session: ClientSession
    server: str
    remote_name: str  # the name the server knows, before any collision rename


def rag_server_spec() -> ServerSpec:
    """rag_server over HTTP if RAG_SERVER_URL is set, else as a local subprocess.

    Compose sets the URL; a bare checkout does not, so the same entry point works
    containerised and on the host.
    """
    url = os.environ.get("RAG_SERVER_URL")
    if url:
        return ServerSpec(name="rag_server", url=url, api_key=os.environ.get("RAG_API_KEY"))
    return ServerSpec(
        name="rag_server",
        params=StdioServerParameters(
            command=sys.executable,
            args=[str(RAG_SERVER)],
            cwd=str(PROJECT_ROOT),
        ),
    )


def docs_server_spec() -> ServerSpec:
    """docs_server over HTTP if DOCS_SERVER_URL is set, else stdio into a container.

    The stdio form ('docker run -i') is still plain stdio transport - the
    container's stdin and stdout are the MCP wire, exactly as for a local
    subprocess. It only works because the *host* is outside the container and
    owns that process; under compose, where the agent is itself a container,
    stdio cannot reach a sibling service and HTTP is required.
    """
    url = os.environ.get("DOCS_SERVER_URL")
    if url:
        return ServerSpec(name="docs_server", url=url, api_key=os.environ.get("DOCS_API_KEY"))
    return ServerSpec(
        name="docs_server",
        params=StdioServerParameters(
            command="wsl.exe",
            args=[
                "-d", "DockerEngine", "--",
                "docker", "run", "--rm", "-i",
                "-v", DOCS_DATA_MOUNT,
                DOCS_IMAGE,
            ],
        ),
    )


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


async def execute_tool_call(
    router: dict[str, ToolBinding], name: str, args: dict[str, Any]
) -> str:
    """Run one model-requested call against whichever server owns that tool.

    Kept at module level rather than nested in the graph so the routing - in
    particular the read_chunk -> resources/read translation - can be exercised
    directly by the verification scripts without depending on whether the model
    happens to choose that tool.
    """
    binding = router.get(name)
    if binding is None:
        return f"No such tool: {name!r}. Available: {sorted(router)}"

    if binding.remote_name == READ_CHUNK_TOOL:
        uri = f"chunk://{args['chunk_id']}"
        log(f"[{binding.server}] translating to resources/read {uri}")
        read = await binding.session.read_resource(uri)
        blocks = [t for t in (getattr(c, "text", None) for c in read.contents) if t]
    else:
        log(f"[{binding.server}] tools/call {binding.remote_name}({args})")
        result = await binding.session.call_tool(binding.remote_name, args)
        blocks = [t for t in (getattr(b, "text", None) for b in result.content) if t]

    return "\n\n".join(blocks) or "(tool returned no content)"


def build_graph(router: dict[str, ToolBinding], tool_specs: list[dict[str, Any]]):
    """Compile the two-node reasoning graph: agent <-> tools."""
    llm = ChatOpenAI(
        base_url=LLM_ENDPOINT,
        api_key="not-needed",  # llama-server ignores it; must be non-empty
        model=LLM_MODEL,
        temperature=0,
        timeout=300,
    )
    llm_with_tools = llm.bind_tools(tool_specs)

    async def call_model(state: AgentState) -> dict[str, Any]:
        response = await llm_with_tools.ainvoke(state["messages"])
        if getattr(response, "tool_calls", None):
            log(f"model requested tool(s): {[c['name'] for c in response.tool_calls]}")
        else:
            log("model produced a final answer")
        return {"messages": [response]}

    async def call_tools(state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        outputs: list[ToolMessage] = []

        for call in last.tool_calls:
            try:
                content = await execute_tool_call(router, call["name"], call["args"])
            except Exception as exc:
                # Surface the failure to the model instead of crashing the run;
                # it can then answer that the operation failed.
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


async def connect_servers(
    stack: AsyncExitStack, specs: list[ServerSpec]
) -> tuple[dict[str, ToolBinding], list[dict[str, Any]], list[str], ClientSession | None]:
    """Open every server and merge their capabilities into one tool namespace.

    Returns the routing table, the merged OpenAI specs, each server's
    instructions, and the session that owns the rag prompt (if any).
    """
    router: dict[str, ToolBinding] = {}
    tool_specs: list[dict[str, Any]] = []
    instructions: list[str] = []
    prompt_session: ClientSession | None = None

    for spec in specs:
        if spec.transport == "stdio":
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(spec.params)
            )
        else:
            import httpx2

            from mcp.client.streamable_http import streamable_http_client

            headers = {"x-api-key": spec.api_key} if spec.api_key else {}
            client = await stack.enter_async_context(httpx2.AsyncClient(headers=headers))
            streams = await stack.enter_async_context(
                streamable_http_client(spec.url, http_client=client)
            )
            read_stream, write_stream = streams[0], streams[1]

        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        init = await session.initialize()
        log(
            f"connected to {init.server_info.name} v{init.server_info.version} "
            f"({spec.transport})"
        )

        if init.instructions:
            instructions.append(f"{spec.name}: {init.instructions}")

        for tool in (await session.list_tools()).tools:
            exposed = tool.name
            if exposed in router:
                # Two servers offering the same tool name would be ambiguous.
                # Namespace the later one rather than silently shadowing it.
                exposed = f"{spec.name}_{tool.name}"
                log(f"tool name collision on {tool.name!r}; exposing as {exposed!r}")
            openai_spec = mcp_tool_to_openai_spec(tool)
            openai_spec["function"]["name"] = exposed
            tool_specs.append(openai_spec)
            router[exposed] = ToolBinding(session, spec.name, tool.name)

        templates = (await session.list_resource_templates()).resource_templates
        if any(t.uri_template.startswith("chunk://") for t in templates):
            tool_specs.append(READ_CHUNK_SPEC)
            router[READ_CHUNK_TOOL] = ToolBinding(session, spec.name, READ_CHUNK_TOOL)
            log(f"[{spec.name}] bridged chunk:// resource as tool {READ_CHUNK_TOOL}")

        if any(p.name == "rag_answer" for p in (await session.list_prompts()).prompts):
            prompt_session = session

    log(f"merged tool namespace: {sorted(router)}")
    return router, tool_specs, instructions, prompt_session


def routing_guidance(router: dict[str, ToolBinding]) -> str:
    """Host-level guidance for choosing between servers.

    A server's prompt only knows about that server. rag_answer tells the model
    to search and then abstain if nothing relevant comes back - correct advice
    when rag_server is the only server, actively wrong once docs_server exists,
    because "not in the index" and "not in the corpus" stop being the same
    thing. Cross-server orchestration is the host's concern, so the guidance is
    composed here from the live routing table rather than written into either
    server's prompt.
    """
    by_server: dict[str, list[str]] = {}
    for exposed, binding in router.items():
        by_server.setdefault(binding.server, []).append(exposed)

    lines = ["You are connected to more than one MCP server. Route by capability:"]
    for server, tools in sorted(by_server.items()):
        lines.append(f"  {server}: {', '.join(sorted(tools))}")
    lines.append(
        "\nImportant: search_documents only sees documents that have already "
        "been ingested into the vector index. If it returns nothing relevant, "
        "the content may still exist in a file that was never ingested - "
        "scanned PDFs and images in particular. Before concluding the corpus "
        "does not cover something, call list_documents, and parse_document on "
        "any file whose name suggests it is relevant. Only then abstain."
    )
    return "\n".join(lines)


async def run(question: str, show_trace: bool, use_docs: bool) -> int:
    specs = [rag_server_spec()]
    if use_docs:
        specs.append(docs_server_spec())

    async with AsyncExitStack() as stack:
        router, tool_specs, instructions, prompt_session = await connect_servers(
            stack, specs
        )

        # The task instruction comes from the server's prompt template, not from
        # this file - the server owns how its corpus should be used.
        if prompt_session is not None:
            prompt = await prompt_session.get_prompt("rag_answer", {"question": question})
            instruction = "\n\n".join(
                t
                for t in (getattr(m.content, "text", None) for m in prompt.messages)
                if t
            )
        else:
            instruction = question

        # Only inject routing guidance when there is actually a choice to make.
        # It is appended to the *user* message rather than the system message:
        # rag_answer ends with "if the passages do not contain the answer, say
        # so plainly", and a 4B model follows that closing numbered instruction
        # over anything in the system prompt. The guidance has to come after it
        # to override it.
        if len(specs) > 1:
            instruction = f"{instruction}\n\n{routing_guidance(router)}"

        messages: list[AnyMessage] = []
        if instructions:
            messages.append(SystemMessage(content="\n\n".join(instructions)))
        messages.append(HumanMessage(content=instruction))

        app = build_graph(router, tool_specs)
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
    parser = argparse.ArgumentParser(description="LangGraph MCP host over local servers.")
    parser.add_argument("question", help="question to answer from the local corpus")
    parser.add_argument(
        "--trace", action="store_true", help="print the full message transcript"
    )
    parser.add_argument(
        "--no-docs",
        action="store_true",
        help="connect to rag_server only (skips the docs_server container)",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.question, args.trace, use_docs=not args.no_docs))


if __name__ == "__main__":
    raise SystemExit(main())
