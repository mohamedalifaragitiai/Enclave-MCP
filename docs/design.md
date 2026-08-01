# Design Notes — Sovereign MCP Platform

Companion to `HLD.md` (architecture) and `resource-governance.md` (policy). This
file records decisions that needed justifying during the build rather than
up front. Phase 5 contributes the transport and authorisation discussion below;
Phase 6 finalises the rest against the HLD.

---

## 1. Transport: stdio vs HTTP

Both transports serve the **identical** `rag_server` object — same tool, same
resource, same prompt. Only the process boundary and the authorisation surface
differ:

```bash
python server.py                                         # stdio (default)
RAG_API_KEY=... python server.py --transport http --port 8765   # streamable HTTP
```

That the switch is a CLI flag and not a fork of the server is the point:
**transport and authorisation are separable from capability.** A tool does not
need to know how its caller reached it.

### Comparison

| | stdio | streamable HTTP |
|---|---|---|
| Network surface | **None.** No socket is opened at all | One TCP listener |
| Authentication | Implicit — you can only talk to it if you can already spawn the process | Explicit — `x-api-key` header checked at the edge |
| Client model | One client per process; the host owns the lifecycle | Many clients, concurrent, independent lifecycles |
| Remote access | Impossible by construction | Possible (deliberately bound to loopback here) |
| Failure mode | Process dies with its parent | Server outlives clients; needs supervision |
| Debuggability | stdout is the wire — a stray `print()` corrupts the protocol | stdout is free; requests inspectable with `curl` |
| Fits the air-gap requirement (HLD §7) | Yes, trivially | Only with deliberate binding and auth |

### Why stdio is the default here

For a single-laptop, single-user, air-gapped-style deployment, stdio is not a
limitation — it is the stronger security posture. An attack surface that does
not exist cannot be misconfigured. There is no port to firewall, no key to
rotate, no TLS to terminate, no session to hijack.

HLD §8 lists "stdio transport for Phases 1–4" as a trade-off whose mitigation is
that Phase 5 demonstrates the alternative is *understood, not merely avoided*.
That is exactly what the HTTP variant is for.

### The one real operational cost of stdio

**stdout is the protocol wire.** Every server in this project routes diagnostics
to stderr through a `log()` helper for that reason. A single stray `print()` in
a tool body emits a non-JSON-RPC line, the client fails to parse the stream, and
the connection drops — with an error that points at the parser, not at the
`print()`. This is the most likely way a new contributor breaks an MCP server,
and it is invisible in code review unless you know to look.

The HTTP transport does not have this hazard, which is a genuine argument in its
favour during development.

### When to switch to HTTP

- The client is on another machine, or in another container without a shared
  parent process
- Multiple concurrent clients need the same server instance and its warm state
  (here: the loaded embedding model and the open Chroma handle)
- The server should outlive any individual client session

None of these apply to the current build, so stdio remains the default and HTTP
stays a demonstrated variant.

---

## 2. Authorisation: what the API key is and is not

The HTTP variant gates every request on a shared secret in an `x-api-key`
header, checked **before** the request reaches any MCP handler.

Three implementation details are deliberate:

1. **Constant-time comparison** (`hmac.compare_digest`). A plain `==` returns
   early on the first mismatching byte, leaking key material through response
   timing. Cheap to get right; embarrassing to get wrong in a security demo.
2. **Fail closed.** With `RAG_API_KEY` unset, the server refuses to start and
   exits non-zero rather than starting unauthenticated. An auth control that
   silently disables itself on a missing environment variable is not a control.
   A minimum key length is enforced for the same reason.
3. **Loopback by default.** `--host` defaults to `127.0.0.1`. Binding `0.0.0.0`
   would put the document corpus on the LAN, which HLD §7 rules out.

### What this does not provide

Stated plainly, because "what would you add for production" is the obvious
follow-up (HLD §8 scopes RBAC out of this project entirely):

| Missing | Consequence |
|---|---|
| Per-user identity | Every caller is the same principal; nothing is attributable |
| Per-tool scoping | A key that can search can also read every chunk; no least privilege |
| Rotation / expiry | A leaked key is valid forever, and revoking it breaks every client at once |
| Audit trail | Beyond a 401 log line, there is no record of who called what |
| Transport encryption | Plain HTTP. Fine on loopback, unacceptable off-box |
| Rate limiting | One client can monopolise the single shared LLM (resource-governance §3) |

The production successor is a real identity provider issuing short-lived scoped
tokens — Keycloak and OAuth2 resource-server semantics, which the MCP Python SDK
supports natively via `token_verifier`/`AuthSettings`. That was deliberately not
used here: it would demonstrate configuring a library rather than understanding
the boundary being defended, and HLD §10 already documents Keycloak/Vault as
enterprise-scale-only.

**The honest framing:** this is the seed of an auth story — a single trust
boundary, correctly placed and correctly failing closed — not a complete one.

---

## 3. Why the LLM is reached through a port proxy

`llama-server` binds `127.0.0.1`, so it is unreachable from the WSL2 distro and
from containers. Two fixes were available:

| Option | Cost |
|---|---|
| Rebind `--host 0.0.0.0` | Restarts a GPU-resident model shared with another project, and exposes inference on the LAN |
| Windows portproxy on the WSL-facing adapter | Model untouched; still loopback-bound from the LAN's perspective |

The proxy was chosen (`scripts/setup-llm-proxy.ps1`). It listens only on the
`vEthernet (WSL)` address, forwards to loopback, and is paired with a firewall
rule scoped to the WSL subnet. The model process is never restarted, which also
respects the fact that the LLM is a *shared* dependency, not one this project
owns.

Consequence worth knowing: port 8001 now shows **two** listeners — the model and
the forwarder. resource-governance §3 was amended to state the rule in terms of
one LLM *process*, since a socket count now reads as two and would otherwise
look like a violation.

---

## 4. Retrieval honesty

Two behaviours exist specifically to stop the agent asserting things the corpus
does not support:

- **Similarity floor (0.50).** Dense retrieval always returns nearest
  neighbours however distant, so an off-topic query still yields a "best" match.
  Without a floor the model receives irrelevant context and is invited to
  hallucinate from it. Calibrated against fixtures (genuine paraphrase matches
  land at 0.60–0.80, unrelated queries at ~0.48) and **needs retuning once the
  real corpus is ingested**.
- **Unindexed-file disclosure.** `search_documents` reports corpus files that
  have no chunks in the index. The index and the corpus are not the same set,
  and conflating them is how an agent concludes "not in the corpus" when it
  means "never ingested". This is information `rag_server` legitimately owns —
  it compares its own index against its own corpus directory — so it creates no
  coupling to `docs_server`.

The second one is what makes multi-server routing work at all: without it the
agent abstained on a question whose answer sat in an un-ingested scanned PDF.

---

## 5. Prompt ownership across servers

MCP prompts are **server-scoped**. `rag_answer` belongs to `rag_server` and
ends by telling the model to abstain when search returns nothing relevant —
correct advice while `rag_server` is the only server, and actively wrong once
`docs_server` exists.

Cross-server orchestration guidance is therefore composed by the **host**, from
its live routing table, because the host is the only component that knows the
full server set. Neither server is modified to know about the other.

Practical note: the guidance is appended to the *user* message rather than the
system message. A 4B model follows the closing numbered instruction of a task
prompt over anything in the system prompt, so guidance placed in the system
message was ignored.

---

## 6. Containerisation: stdio does not survive a container boundary

This is the single most consequential finding of Phase 6, and it contradicts the
architecture diagram in HLD §2.

stdio works because a **parent process owns a child's stdin and stdout**. Two
separate containers do not share a process tree, so there is nothing for stdio
to attach to. The HLD diagram shows three sibling containers with `stdio (MCP)`
arrows between them; that topology is not realisable as drawn.

Three options existed:

| Option | Verdict |
|---|---|
| Servers become HTTP services on the compose network | Chosen. The standard remote-MCP topology, and what compose is for |
| Agent mounts `/var/run/docker.sock` and spawns `docker run -i` per server | Rejected. Access to the Docker socket is equivalent to root on the host; shipping that in a project with a security phase would be indefensible |
| One container running agent and both servers over stdio | Rejected. Does not wire three services, and forces Tesseract into the agent image |

So Phase 5's HTTP transport turned out not to be an optional demonstration after
all — it is a **prerequisite for containerisation**. `docs_server` gained the
same transport switch in Phase 6 for exactly this reason.

The agent still supports both. `RAG_SERVER_URL` / `DOCS_SERVER_URL` being set is
what switches it from spawning subprocesses to HTTP; unset, a bare checkout runs
identically to Phase 4. The reasoning loop never learns which transport is in
use — only `connect_servers` does.

### Reaching the LLM from a container

`extra_hosts` maps `host.docker.internal` to the **`vEthernet (WSL)` address**,
not to `host-gateway`. This is not interchangeable: `host-gateway` resolves to
the Docker bridge gateway, which is the DockerEngine distro itself, not Windows.
The LLM listens on the Windows side, so `host-gateway` would resolve and then
silently fail to connect. The address is parameterised as `WSL_HOST_IP` because
it can change across reboots.

### What compose enforces

- **Bind mounts only**, no top-level `volumes:` block, so all state is visible
  under `./data` (HLD §5). Verified: `docker volume ls` is empty.
- **No published ports.** Both servers are reachable only on the compose
  network. `docker compose ps` lists `8765/tcp` for rag_server because the
  Dockerfile `EXPOSE`s it — that is documentation, not a host mapping.
- **Keys are required, not defaulted.** `${RAG_API_KEY:?...}` fails the whole
  `compose up` if unset, rather than starting an unauthenticated server.
- **Auth verified from inside the network**, not just from outside: a keyless
  POST to rag_server on the compose network returns 401.

---

## 7. Cold start was an air-gap violation, not just a slow first request

The first containerised run failed with `SSE stream ended without a response` on
`search_documents`. The cause was worse than a timeout: the container was
downloading the ONNX embedding weights **from huggingface.co while serving the
request**. That is runtime network egress, which HLD §7 forbids outright, and it
was slow enough that the client gave up mid-call.

Both halves are fixed:

- the weights are baked into the image at build time (+68 MB), so the running
  container never reaches the network;
- the model is loaded at start-up before the listener opens, so the first
  request does not pay for it. Only in HTTP mode — under stdio the process is
  spawned per session, so warming there would tax every run for no benefit.

Verified after the fix: zero requests to huggingface.co in the running
container's logs.

The general lesson is worth stating: **lazy initialisation quietly converts a
build-time dependency into a runtime one.** In an air-gapped design that is not
a performance detail, it is a correctness violation.

---

## 8. Reconciliation with HLD.md

Phase 6 requires this document to be finalised against the HLD. Three
discrepancies remain open; none is silently patched here, since `HLD.md` is the
design baseline and `resource-governance.md` §5 requires changes to be
deliberate.

| # | HLD says | Reality after Phase 6 |
|---|---|---|
| 1 | §2 diagram: `agent --stdio--> rag_server` and `--stdio--> docs_server` between sibling containers | Not realisable (§6 above). Under compose both edges are HTTP. The diagram needs redrawing, or a note that stdio applies only to the non-containerised topology |
| 2 | §3 table: `rag_server` transport is "stdio (Phase 1-4), SSE+API-key (Phase 5 variant)" | Accurate, but understates it: HTTP is now the *default* under compose, not a variant |
| 3 | resource-governance §2: `rag_server` image 500-700 MB | **Actual: 933 MB.** See below |

### The image-size overrun

`rag_server` exceeds its documented budget by ~33%. The drivers are measured,
not guessed:

| Component | Size |
|---|---|
| `pip install` layer (chromadb + fastembed + onnxruntime tree) | 492 MB |
| Python 3.12-slim base | ~46 MB |
| Baked embedding weights | 68 MB |

Within site-packages the largest single entry is the **Kubernetes client at
83 MB**, pulled in by chromadb for its distributed deployment mode and entirely
unused by a file-based, single-laptop store. `grpc` (18 MB) arrives the same
way. Removing them plausibly recovers ~100 MB, which would still leave the image
around 850 MB — over budget.

The other two images are within budget: `docs_server` 1.23 GB (against
1.5-2.5 GB) and `agent` 355 MB (against 400-600 MB), so the **total** stack is
comfortably inside the steady-state envelope in §2. The overrun is specific to
one line item whose estimate predates knowing chromadb's dependency tree.

This is flagged rather than fixed or amended away, because §5 makes budget
changes a deliberate edit, not a side effect of a Dockerfile.
