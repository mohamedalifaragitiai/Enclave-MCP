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
