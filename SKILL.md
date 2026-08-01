---
name: sovereign-mcp-platform
description: Build and extend a local, open-source, air-gapped-style MCP (Model Context Protocol) platform entirely inside M:\MCP_Project on Windows. Use this skill whenever the user asks to build MCP servers, an MCP host/client, RAG tools, document-intelligence tools, or Docker services for this project — or mentions "MCP_Project", "sovereign MCP", "MCP platform", or the local Qwen3 llama-server setup. Enforces two hard constraints from resource-governance.md: (1) zero writes to C:\ — Docker Engine runs inside a custom WSL2 distro whose vhdx lives entirely on M:\, not Docker Desktop; (2) exactly one LLM instance — always call the already-running llama-server at http://host.docker.internal:8001 (Qwen3-4B-Instruct-2507, Q4_K_M GGUF) instead of pulling/loading any model.
---

# Sovereign MCP Platform (local, open-source, M:\MCP_Project)

A phased build of a local MCP platform: a LangGraph agent (MCP host/client) talking to
local MCP servers (RAG search, document parsing) over stdio, containerized with Docker
Engine running inside a dedicated WSL2 distro, backed by the user's already-running
llama-server. Full architecture: see `HLD.md`. Full resource policy: see
`resource-governance.md`. This file is the build workflow; those two are the design
and policy references — read them, don't restate them here.

## Before doing anything: verify the environment matches the design

1. Confirm the `DockerEngine` WSL2 distro exists and is the one being used — **not**
   Docker Desktop:
   ```powershell
   wsl --list --verbose
   ```
   If `DockerEngine` isn't listed, stop and set it up first (see "First-time setup"
   below) — do not fall back to Docker Desktop even if it happens to be installed.
2. Confirm exactly one LLM listener on port 8001:
   ```powershell
   netstat -ano | findstr :8001
   ```
3. Snapshot C:\ free space before starting work (compare again at the end), and
   confirm the swap file didn't land on C:\:
   ```powershell
   Get-PSDrive C | Select-Object Used,Free
   Test-Path "$env:USERPROFILE\AppData\Local\Temp\swap.vhdx"   # must be False
   ```
Run all `docker` / `docker compose` commands via `wsl -d DockerEngine`, from
`/mnt/m/MCP_Project`, never from a Windows Docker Desktop context.

## First-time setup (only if `DockerEngine` distro doesn't exist yet)

**Step 0 — redirect the WSL2 swap file before starting any distro.** The swap file
belongs to the shared WSL2 utility VM, not to any individual distro, and gets created
on C:\ the moment the *first* distro of any kind starts — so this must happen before
`wsl --import`, not after:
```powershell
wsl --shutdown

@"
[wsl2]
swapFile=M:\\WSL\\swap.vhdx
"@ | Out-File -Encoding ascii "$env:USERPROFILE\.wslconfig"

mkdir M:\WSL -ErrorAction SilentlyContinue
```
(`.wslconfig` itself must sit at `%USERPROFILE%\.wslconfig` on C:\ — see
resource-governance.md §1 "Acknowledged exception." This is the only sanctioned
write to C:\ in this entire project.)

```powershell
mkdir M:\WSL\downloads
curl.exe -L -o M:\WSL\downloads\ubuntu.tar.gz https://cloud-images.ubuntu.com/wsl/releases/24.04/current/ubuntu-24.04-wsl-amd64-wsl.rootfs.tar.gz
mkdir M:\WSL\DockerEngine
wsl --import DockerEngine M:\WSL\DockerEngine M:\WSL\downloads\ubuntu.tar.gz --version 2
wsl -d DockerEngine
```
Inside the distro:
```bash
apt update && apt install -y docker.io docker-compose-plugin
echo -e "[boot]\nsystemd=true" >> /etc/wsl.conf
exit
```
```powershell
wsl --shutdown
wsl -d DockerEngine
docker info --format '{{.DockerRootDir}}'   # confirm it reports a path inside /var/lib/docker on this distro's own vhdx
```

## Directory layout

```
M:\MCP_Project\
├── HLD.md
├── resource-governance.md
├── SKILL.md                    # this file
├── docker-compose.yml
├── .env
├── .cache\                      # PIP_CACHE_DIR, HF_HOME — see resource-governance.md §1
├── data\
│   ├── raw_docs\
│   └── chroma_db\
├── servers\
│   ├── rag_server\{Dockerfile,requirements.txt,server.py}
│   └── docs_server\{Dockerfile,requirements.txt,server.py}
├── agent\{Dockerfile,requirements.txt,host.py}
├── scripts\ingest.py
└── docs\design.md
```

## Caches — set every session, before any uv/docker command

```powershell
$env:UV_CACHE_DIR = "M:\MCP_Project\.cache\uv"
$env:UV_PYTHON_INSTALL_DIR = "M:\MCP_Project\.cache\uv-python"
$env:HF_HOME = "M:\MCP_Project\.cache\huggingface"
$env:DOCKER_BUILDKIT = "1"
```

Python venv, using `uv` (run from `M:\MCP_Project` so the venv lands at
`M:\MCP_Project\.venv` by default):
```powershell
cd M:\MCP_Project
uv venv
uv pip install -r requirements.txt   # or: uv sync, if using a pyproject.toml
```
Never `python -m venv` on a bare C:\ path, and never run `uv` without the two env
vars above set first — check `$env:UV_CACHE_DIR` is non-empty before any `uv`
command if unsure.

## Build phases

Work through these in order; confirm each phase runs before starting the next.

### Phase 1 — single MCP server, one tool
`servers\rag_server\server.py` using the official `mcp` Python SDK. One tool:
`search_documents(query: str) -> list[str]`, naive keyword search over
`data\raw_docs\`. Run with stdio transport; verify with a manual MCP client call.

### Phase 2 — resources, prompts, real retrieval
Add `chromadb` + `bge-small-en-v1.5`; `scripts\ingest.py` chunks/embeds
`data\raw_docs\` into `data\chroma_db\`. Upgrade `search_documents` to vector
search. Add an MCP resource (retrieved chunk by ID) and an MCP prompt template.

### Phase 3 — real client (LangGraph as MCP host)
`agent\host.py`: LangGraph agent as MCP client/host, connects to `rag_server` over
stdio, calls Qwen3 via `http://host.docker.internal:8001/v1` for reasoning and
tool-call decisions. Verify Qwen3's tool-call format parses correctly with
LangGraph's tool binding before trusting agent routing — check llama-server's
function-calling template docs if calls aren't parsing.

### Phase 4 — second server, multi-server orchestration
`servers\docs_server\server.py` with `unstructured` + `pytesseract`. Update
`agent\host.py` to route between both servers. Test against a scanned page from
the OSHA or EIA corpus.

### Phase 5 — security hardening
Add an SSE/HTTP transport variant with an API-key check for one server, to
demonstrate the auth pattern. Document the stdio-vs-HTTP trade-off in
`docs\design.md` (cross-reference HLD.md §8).

### Phase 6 — containerize + finalize design doc
`docker-compose.yml` wiring `rag_server`, `docs_server`, `agent`; bind mounts only
to paths under `M:\MCP_Project`; all LLM calls via `host.docker.internal:8001`.
Finalize `docs\design.md` against `HLD.md`.

## docker-compose.yml skeleton

```yaml
services:
  rag_server:
    build: ./servers/rag_server
    volumes:
      - ./data:/app/data
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      - LLM_ENDPOINT=http://host.docker.internal:8001/v1

  docs_server:
    build: ./servers/docs_server
    volumes:
      - ./data:/app/data
    extra_hosts:
      - "host.docker.internal:host-gateway"

  agent:
    build: ./agent
    depends_on: [rag_server, docs_server]
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      - LLM_ENDPOINT=http://host.docker.internal:8001/v1
```
No top-level named `volumes:` — bind mounts only, so nothing lands in Docker's
internal volume store (which would otherwise still be safely on M:\ via the
distro placement, but bind mounts keep it visible and simple to audit).

## End-of-session checklist

1. `docker builder prune -f` if a phase involved several rebuild iterations.
2. `Get-PSDrive C | Select-Object Used,Free` — compare to session start; investigate
   any drop per resource-governance.md §1.
3. Every 2–3 phases: compact the vhdx per resource-governance.md §4.
4. If anything in this session pushed against a constraint in
   `resource-governance.md`, flag it explicitly rather than silently working
   around it — that file is the source of truth, not this one.
