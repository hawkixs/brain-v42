# Spike — Claude Code session join, and the real OTLP schema

**Date**: 2026-08-06
**Version measured**: Claude Code 2.1.220
**Plan**: `docs/superpowers/plans/2026-08-06-live-client-activity.md`, task 1
**Verdict**: **JOIN IMPOSSIBLE**

## Method

Contrary to what the plan announced, the spike required no interactive
session. `claude -p` launches a real Claude Code session as a subprocess.
Two disposable receivers on `127.0.0.1:4318` — one for MCP headers
(dedicated `--mcp-config`, `--strict-mcp-config`), one for OTLP — are enough
to answer both questions.

## Question 1 — does `${CLAUDE_CODE_SESSION_ID}` expand in an MCP header?

**No, not usefully.**

| Parent environment | `X-Brain-Session` header received |
|---|---|
| `CLAUDE_CODE_SESSION_ID` present | `3d7a88d7-…` — **the parent's identifier** |
| `CLAUDE_CODE_SESSION_ID` absent | `${CLAUDE_CODE_SESSION_ID}` — **unexpanded literal** |

Claude Code expands `${VAR}` from the **process's** environment, when loading
the MCP configuration. It does not set `CLAUDE_CODE_SESSION_ID` for itself
before this read: the identifier it gives itself
(`e7734ce9-…` on the second run) is never visible at expansion time.

The first run looked conclusive; it was not. The calling session's
environment leaked into the subprocess and injected the **wrong** identifier
into it. A join built on this would have silently reattached every child
session to its parent.

`${PWD}` expands correctly (`/tmp` observed): the mechanism works, it's the
variable that doesn't exist at the right time.

**Consequence**: the `unattributed` line planned by the design is not a
theoretical degraded case, it is the **nominal** case for Claude Code today.
`provenance.normalize_session` already rejects the unexpanded template and
returns `None` — the measured behavior falls exactly into the planned path.

## Question 2 — the real OTLP schema

`session.id` does exist, along with all the desired counters. Three gaps
from what the plan assumed.

### Gap 1 — `event.name` is NOT prefixed

The plan expected `claude_code.user_prompt`. Measured:

| Field | Value |
|---|---|
| `event.name` (attribute) | `user_prompt`, `api_request`, `assistant_response` |
| `body.stringValue` | `claude_code.user_prompt`, `claude_code.api_request` |

The prefix is in the **body**, not in the attribute. A decoder filtering on
`claude_code.*` via `event.name` would recognize no records at all.

Other events seen, out of scope: `hook_execution_start`,
`hook_execution_complete`, `hook_registered`, `plugin_loaded`.

### Gap 2 — `input_tokens` does not measure the context

Recorded from a real `api_request`:

| Attribute | Type | Value |
|---|---|---|
| `input_tokens` | intValue | **10** |
| `cache_read_tokens` | intValue | 11,776 |
| `cache_creation_tokens` | intValue | 6,804 |
| `output_tokens` | intValue | 43 |
| `cost_usd` | doubleValue | 0.0150106 |
| `cost_usd_micros` | intValue | 15011 |
| `duration_ms` | intValue | 1496 |
| `model` | stringValue | `claude-haiku-4-5-20251001` |

The real context of this request is ~18,590 tokens; `input_tokens` reports
10. Displaying `input_tokens` as "context tokens" — which is what the Codex
panel does with `input_token_count` — would underestimate a Claude session
by three orders of magnitude.

The useful sum is `input_tokens + cache_read_tokens + cache_creation_tokens`.

### Gap 3 — every record carries personal data

**All** records, including hook and plugin events,
carry in the clear:

| Attribute | Observed content |
|---|---|
| `user.email` | the account's email address, in the clear |
| `user.id` | a 64-character fingerprint |
| `user.account_uuid`, `user.account_id` | account identifiers |
| `organization.id` | organization identifier |

`prompt` and `response` were `<REDACTED>` — redaction is the default, but
`OTEL_LOG_USER_PROMPTS=1` lifts it. `prompt_length` and `response_length`
are always in the clear.

**Design consequence**: the receiver's allowlist projection is not a
stylistic precaution, it is the only thing preventing the operator's email
address from entering a registry exposed over HTTP. It becomes a
requirement justified by measurement, to be tested explicitly.

`terminal.type` was `non-interactive` under `claude -p`.

## What this changes in the plan

1. **Task 5** — `_KNOWN_EVENTS` becomes `{"user_prompt", "api_request"}`, with
   no prefix. The fixture `tests/fixtures/claude_otlp_logs.json` is the oracle.
2. **Task 5** — also project `cache_read_tokens` and `cache_creation_tokens`,
   and add a test proving that `user.email` does not survive the projection.
3. **Task 8** — the tokens for a Claude line are the sum of the three input
   counters, not `input_tokens` alone.
4. **Task 8** — the join test becomes an **absence-of-join** test: a
   Claude session produces a distinct OTLP-only line and an `unattributed`
   line, like Codex. The join stays implemented for the day a client
   knows how to declare its session; it simply has no client
   today.

## What would reopen the question

A populated `X-Brain-Session` assumes the client knows its session
identifier before loading its MCP configuration. Today neither Claude Code
nor Codex allows this. Re-measure at every Claude Code version bump rather
than taking this conclusion on faith.
