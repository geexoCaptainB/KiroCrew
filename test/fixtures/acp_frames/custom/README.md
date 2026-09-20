# Custom ACP frame corpus

Two files, both synthesized. Read `../README.md` first for what a fixture is and
what the corpus does and does not prove.

| File | Provenance | Frame classes it carries |
|---|---|---|
| `handshake-synthesized.jsonl` | **synthesized** | `initialize` result with an integer `protocolVersion` of 1 and `agentInfo.version`; `session/new` result with a `sessionId` and a `configOptions` select carrying the gate value the worked example configures |
| `turn-synthesized.jsonl` | **synthesized** | `agent_message_chunk`, `tool_call`, `session/request_permission` with all four spec option kinds, `tool_call_update` with the result, and the `session/prompt` result carrying `stopReason` |

## Why nothing here is live

`ACP_BACKEND_CUSTOM` is whatever command the operator names in `agent.custom_acp`.
Two deployments can run two different harnesses under it on the same day, so no
capture of any one of them is evidence about the class -- and a live fixture here
would claim exactly that. What the id DOES promise is a dialect: ACP v1, because
the `configOptions` gate the harness must advertise is defined there. That dialect
is what this corpus is written from, using the protocol's own shapes and no
harness's `_meta` channel, so what it pins is what the dispatch parsers make of a
spec-conforming turn with nothing product-specific on the wire.

## What the handshake file is evidence for

The `session/new` result carries the one thing Kiro Crew requires of a harness under
this id before its first prompt: a `configOptions` entry whose id and one of whose
values match the pair in `agent.custom_acp` (`mode` / `read-only` in the worked
example and in this file). `agent_sdk.tool_gate.session_config_issue` looks for
exactly that entry, and `AcpClient._apply_session_permission_routing` writes the
value through `session/set_config_option` when it is there and refuses the session
when it is not. The file shows the shape that passes; `test_acp_custom_backend.py`
holds the refusal.

## What it is not evidence for

That any particular harness asks. The `session/request_permission` frame in
`turn-synthesized.jsonl` is the spec's shape of the frame, not an observation that
a harness emitted it -- the observation is per harness, made when a harness is
onboarded as a named backend with a live corpus of its own. Until then, a custom
harness's routing verdict is "advertised and applied", as for codex, and the
residual is stated on its card.
