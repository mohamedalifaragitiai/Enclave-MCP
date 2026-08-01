"""Manual acceptance check - Phase 5 HTTP transport with API-key auth.

Starts rag_server in HTTP mode as a subprocess, then checks the auth boundary
from the outside:

  1. no API key            -> 401
  2. wrong API key         -> 401
  3. correct API key       -> full MCP session works over HTTP
  4. server refuses to start unauthenticated when the key env var is missing

Check 4 matters as much as the others: an auth control that silently disables
itself on a missing env var is not a control.

Run:  .venv\\Scripts\\python.exe scripts\\verify_phase5.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx2

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SCRIPT = PROJECT_ROOT / "servers" / "rag_server" / "server.py"

HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}/mcp"

GOOD_KEY = "phase5-demo-key-do-not-reuse"
BAD_KEY = "phase5-demo-key-WRONG-value!"


def _rule(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _start_server(api_key: str | None) -> subprocess.Popen:
    env = dict(os.environ)
    if api_key is None:
        env.pop("RAG_API_KEY", None)
    else:
        env["RAG_API_KEY"] = api_key
    return subprocess.Popen(
        [sys.executable, str(SERVER_SCRIPT), "--transport", "http",
         "--host", HOST, "--port", str(PORT)],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_until_listening(proc: subprocess.Popen, timeout: float = 30.0) -> bool:
    """Poll the port until the server answers (any status means it is up)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            httpx2.post(URL, json={}, timeout=2.0)
            return True
        except Exception:
            time.sleep(0.4)
    return False


async def _mcp_session_with_key(api_key: str) -> tuple[str, int]:
    """Open a full MCP session over HTTP with the given key."""
    headers = {"x-api-key": api_key}
    async with httpx2.AsyncClient(headers=headers) as client:
        async with streamable_http_client(URL, http_client=client) as streams:
            read_stream, write_stream = streams[0], streams[1]
            async with ClientSession(read_stream, write_stream) as session:
                init = await session.initialize()
                tools = (await session.list_tools()).tools
                result = await session.call_tool(
                    "search_documents", {"query": "mechanical integrity"}
                )
                text = "\n".join(
                    t for t in (getattr(b, "text", None) for b in result.content) if t
                )
                return f"{init.server_info.name} v{init.server_info.version}", len(tools), text


def main() -> int:
    failures: list[str] = []

    # ---- checks 1-3: server running WITH a key --------------------------
    proc = _start_server(GOOD_KEY)
    try:
        if not _wait_until_listening(proc):
            print("server failed to start")
            print(proc.stderr.read() if proc.stderr else "")
            return 1

        _rule("1. no API key -> must be rejected")
        r = httpx2.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}, timeout=10)
        print(f"status: {r.status_code}  body: {r.text[:120]}")
        if r.status_code != 401:
            failures.append(f"unauthenticated request returned {r.status_code}, expected 401")

        _rule("2. wrong API key -> must be rejected")
        r = httpx2.post(
            URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers={"x-api-key": BAD_KEY},
            timeout=10,
        )
        print(f"status: {r.status_code}  body: {r.text[:120]}")
        if r.status_code != 401:
            failures.append(f"wrong-key request returned {r.status_code}, expected 401")

        _rule("3. correct API key -> full MCP session over HTTP")
        server_info, tool_count, text = asyncio.run(_mcp_session_with_key(GOOD_KEY))
        print(f"server: {server_info}")
        print(f"tools : {tool_count}")
        print(f"\nsearch_documents result:\n{text[:400]}")
        if tool_count < 1:
            failures.append("authenticated session discovered no tools")
        if "chunk_id" not in text:
            failures.append("authenticated tool call returned no search results")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # ---- check 4: server must refuse to start with no key ---------------
    _rule("4. missing RAG_API_KEY -> server must refuse to start")
    proc = _start_server(None)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        failures.append("server kept running without RAG_API_KEY set")
    else:
        stderr = (proc.stderr.read() if proc.stderr else "").strip()
        print(f"exit code: {proc.returncode}")
        print(f"stderr   : {stderr}")
        if proc.returncode == 0:
            failures.append("server exited 0 without an API key instead of failing closed")
        if "refusing to start" not in stderr:
            failures.append("server did not log a refusal reason")

    _rule("RESULT")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PHASE 5 VERIFICATION COMPLETE - all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
