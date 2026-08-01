# Resource Governance — Sovereign MCP Platform

**Scope:** all activity under `M:\MCP_Project`, the `DockerEngine` WSL2 distro, and
the shared `llama-server` LLM instance. This document is the enforceable policy that
`SKILL.md` and the build agent must check against before and after any build step.

**Primary Python tooling:** `uv`, not bare `pip`/`venv`. `uv` is used for all host-side
dependency management (venv creation, installs) because its cache and Python-install
locations are explicitly redirectable — see the table below. Inside Dockerfiles, `uv`
or `pip` may be used interchangeably for image builds; either is fine there since the
whole Docker data-root already lives on M:\ via the `DockerEngine` distro placement.

---

## 1. Storage boundary policy

**Rule: C:\ receives zero writes from this project, ever.** No exceptions for
"temporary" files, logs, or caches.

| Item | Required location | Never allowed at |
|---|---|---|
| Project source, data, docs | `M:\MCP_Project\...` | anywhere on C:\ |
| Docker Engine data-root (`/var/lib/docker`) | Inside `M:\WSL\DockerEngine\ext4.vhdx` (by construction, via `wsl --import`) | Docker Desktop default (C:\Users\...\AppData) |
| WSL2 swap file | `M:\WSL\swap.vhdx` (`swapFile=` in `.wslconfig`, set **before** first `wsl --import`) | Default `%USERPROFILE%\AppData\Local\Temp\swap.vhdx` |
| uv package cache | `M:\MCP_Project\.cache\uv` (`UV_CACHE_DIR`) | Default `%LOCALAPPDATA%\uv` |
| uv-managed Python installs | `M:\MCP_Project\.cache\uv-python` (`UV_PYTHON_INSTALL_DIR`) | Default `%APPDATA%\uv\python` |
| Hugging Face model cache | `M:\MCP_Project\.cache\huggingface` (`HF_HOME`) | `C:\Users\...\.cache\huggingface` |
| Python venv | `M:\MCP_Project\.venv` (created via `uv venv`, run from the project root) | `C:\Users\...\venv` |
| Docker build cache | Inside the WSL distro's vhdx on M:\ | N/A if using DockerEngine distro correctly |

**Acknowledged exception:** `%USERPROFILE%\.wslconfig` (~1 KB) must reside on C:\ —
this is a hard WSL2 requirement with no override. It contains only the `swapFile=`
path pointer, no project data, and never grows. This is the sole sanctioned
exception to the zero-C:\-writes rule; treat any other C:\ write as a real leak,
not as "another one of those."

**Verification commands (run before and after every work session):**
```powershell
Get-PSDrive C | Select-Object Used,Free
Test-Path "$env:USERPROFILE\AppData\Local\Temp\swap.vhdx"   # must be False
```
If C:\ free space drops by more than a trivial amount (a few MB of Windows-side
noise is fine), or the swap.vhdx check returns True, stop and find the leak before
continuing.

---

## 2. Storage budget

| Component | Estimated size |
|---|---|
| Ubuntu WSL rootfs (distro base) | 600 MB – 1 GB |
| Docker Engine + compose plugin | 300–400 MB |
| `rag_server` image | 500–700 MB |
| `docs_server` image (unstructured + OCR) | 1.5–2.5 GB |
| `agent` image (LangGraph stack) | 400–600 MB |
| Embeddings cache | ~130 MB |
| ChromaDB data | 50–300 MB (scales with corpus) |
| Raw PDF corpus | 200 MB – 1 GB |
| Docker build cache | 1–3 GB (reclaimable) |
| **Steady-state total** | **~5–9 GB** |
| **With unpruned build iterations** | **~9–13 GB** |

**Policy: require 20 GB free on M:\ before starting a build.** If free space falls
below 10 GB at any phase boundary, run the cleanup routine (§4) before proceeding.

---

## 3. Compute / model governance

**Rule: exactly one LLM instance may be running for this project at any time.**

- The only permitted inference endpoint is the existing `llama-server` on
  `http://localhost:8001` (host) / `http://host.docker.internal:8001` (containers),
  serving `Qwen3-4B-Instruct-2507`, Q4_K_M, `-c 4096`, q8_0 KV cache.
- No component may install, download, or launch Ollama, vLLM, a second llama-server
  instance, or any cloud LLM API client — even "just for testing."
- Embeddings (`bge-small-en-v1.5`) run on CPU or share the same GPU context as
  Qwen3 — do not provision a separate GPU-resident embedding server.
- Before adding any new service that calls an LLM, check: does it point at
  port 8001? If not, it's out of policy — fix it before merging.

**Verification:**
```powershell
Get-Process llama-server        # confirm exactly one LLM process
netstat -ano | findstr :8001    # see note below before judging this output
```

**Note on the listener count.** Since the WSL port proxy was added (see
`scripts/setup-llm-proxy.ps1`), **two** listeners on 8001 are expected and
correct:

| Listener | Owner | What it is |
|---|---|---|
| `127.0.0.1:8001` | `llama-server` | the one and only model process |
| `<vEthernet (WSL) address>:8001` | `svchost` (IP Helper) | port proxy forwarding to the line above |

The proxy is a forwarder, not an inference endpoint — it loads no model and
consumes no VRAM. The rule in this section is one LLM **process**, so verify
with `Get-Process llama-server`; a raw socket count will read as two and must
not be treated as a violation. Any *third* listener, or any `llama-server`
process beyond the first, is a real breach.

---

## 4. Cleanup routine (run at each phase boundary)

1. Prune stale build cache without touching needed images:
   ```bash
   wsl -d DockerEngine
   docker builder prune -f
   ```
2. Check actual usage breakdown:
   ```bash
   docker system df -v
   ```
3. Every 2–3 phases, compact the vhdx (it grows but never auto-shrinks):
   ```powershell
   wsl --shutdown
   diskpart
   # select vdisk file="M:\WSL\DockerEngine\ext4.vhdx"
   # compact vdisk
   ```
4. Re-run the C:\ verification command from §1.

---

## 5. Change control

Any change to this document (raising the storage budget, adding a second model
endpoint, relaxing the C:\ boundary) must be a deliberate, explicit edit here —
not an implicit side effect of a Docker Compose change or a `pip install`. If
the build agent proposes something that would violate §1 or §3, it must flag the
conflict and ask before proceeding, not silently comply.
