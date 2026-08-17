---
title: "Codex OTLP → cockpit telemetry bridge"
status: completed
date: "2026-07-20"
summary: "Receive bounded Codex OTLP logs on the loopback metrics sidecar and expose pseudonymous, in-memory conversation activity."
tags:
  - codex
  - opentelemetry
  - cockpit
  - privacy
  - tdd
---

# Codex OTLP → cockpit telemetry bridge

## Outcome

Replace the `active_convs`, `ctx_tokens`, and `activeConvs` stubs in
`GET /api/cockpit` with best-effort Codex activity. The bridge keeps raw events,
prompts, account metadata, tool output, and raw conversation identifiers out of
logs and durable storage.

This change does not modify `BRAIN_DREAM_CAPABILITY_ENFORCEMENT=false`. Dream
capability enforcement and Codex client telemetry are independent controls.

The automated delivery ends after code verification, merge, and non-force push.
It does not edit `~/.codex/config.toml`, restart services or clients, deploy an
image, or change red-monitor.

## Verified evidence

- Base commit: `40c2a6a09f9b14688da93adb15b16698e189404f`.
- GitNexus `1.6.9` query and FTS are healthy after repair.
- `brain-metrics.service` listens on `127.0.0.1:9200`.
- The live API currently returns `active_convs=0`, `ctx_tokens=0`,
  `activeConvs=[]`, and `cost.today=null`.
- Codex `0.144.5` supports user-level OTLP/HTTP JSON export. Project
  `.codex/config.toml` cannot set `otel`. See the
  [Codex manual](https://developers.openai.com/codex/codex-manual.md).
- OTLP/HTTP JSON uses `POST /v1/logs` and the Protobuf JSON shape. See the
  [OTLP specification](https://opentelemetry.io/docs/specs/otlp/).

One redacted, ephemeral probe ran on 2026-07-20. The probe wrote no raw payload
to disk and printed only attribute names, types, and allowlisted counters. It
confirmed:

- `event.name`, `conversation.id`, and `model` are log-record attributes;
- the log-record body is not the event name;
- token counts appear as both `stringValue` and `intValue`;
- records also contain `user.email`, `user.account_id`, `prompt`, and
  `mcp_servers`;
- one `codex exec` emitted one user-prompt event and two
  `event.kind=response.completed` events.

The committed fixture must therefore be synthetic. It must not derive from a
raw capture.

## Reproducible blast radius

The following GitNexus parameters apply to every row:
`direction=upstream`, `maxDepth=3`, `includeTests=true`,
`summaryOnly=true`, repository `brain_v42`.

| Symbol | Risk | Direct | Total | Consequence |
| --- | --- | ---: | ---: | --- |
| `MetricsServer` | CRITICAL | 41 | 60 | Preserve constructor compatibility and all existing routes. |
| `MetricsServer._build_app` | HIGH | 27 | 39 | Register the receiver only for a loopback bind. |
| `CockpitCollector` | MEDIUM | 8 | 27 | Keep the registry optional for direct callers. |
| `CockpitCollector._build` | MEDIUM | 6 | 16 | Replace only the three telemetry stubs. |
| `MetricsRuntime` | HIGH | 15 | 22 | Do not modify. |
| `Settings` | CRITICAL | 95 | 305 | Do not add configuration in this slice. |

The route reuses the existing sidecar rather than introducing another
lifecycle or port.

## Architecture

```text
Codex app / CLI / IDE
  user-level OTLP/HTTP JSON, prompts redacted
                 |
                 | POST 127.0.0.1:9200/v1/logs
                 v
MetricsServer
  bind guard + peer guard + route-local limits
                 |
                 v
bounded decoder → strict record projection → atomic registry update
                                              |
                                              v
                           CodexConversationRegistry
                           process-local HMAC ids
                           TTL + cardinality caps
                                              |
                                              v
CockpitCollector → active_convs / ctx_tokens / activeConvs
```

Add `brain_v42.metrics.codex_telemetry`. It contains the bounded OTLP decoder
and `CodexConversationRegistry`; it is not a general OpenTelemetry collector.
It has no database or logging dependency.

`MetricsServer` owns one registry by default and accepts an injected registry
for tests. It passes the same registry to `CockpitCollector`. Existing direct
`CockpitCollector` callers omit the registry and retain `0`, `0`, and `[]`.

The registry is synchronous and guarded by a lock. Each request is fully
decoded and validated before an atomic state replacement. Concurrent requests
cannot expose a partially applied batch.

## Projection and state contract

Only log-record attributes participate. Resource attributes, scope attributes,
body, trace IDs, span IDs, headers, and unknown fields never enter state.

The decoder recognizes only:

- `codex.conversation_starts`;
- `codex.user_prompt`;
- `codex.sse_event` with `event.kind=response.completed`;
- `codex.websocket_event` with `event.kind=response.completed`.

Other events, including unknown `codex.*` names, are complete no-ops and do
not refresh activity.

The decoder reads these fields:

| Field | Validation | Use |
| --- | --- | --- |
| `conversation.id` | canonical UUID, at most 36 characters | HMAC input only |
| `event.name` | exact recognized value | event classification |
| `event.kind` | exact `response.completed` when required | event classification |
| `model` | 1–128 ASCII slug characters | stored model or `unknown` |
| `input_token_count` | non-negative bounded integer from `stringValue` or `intValue` | latest context estimate |
| `timeUnixNano` | 1–20 decimal digits | ordering and dedup only |

Duplicate recognized attributes make that record invalid. Unused token
counters such as cached, output, reasoning, and tool tokens are ignored.

The raw UUID exists only in the decoder's request-local projection. The
registry immediately replaces it with:

```text
HMAC-SHA-256(process_random_32_bytes,
            "codex-conversation-id\0" || canonical_uuid)
```

The API exposes the first 128 digest bits as `codex-<32 lowercase hex>`.
The process-local key is never serialized.

The cockpit fields mean:

- `metrics.active_convs`: valid conversations currently resident in the
  bounded registry.
- `metrics.ctx_tokens`: sum of each resident conversation's latest accepted
  `input_token_count`. Cached input is already included and is not added.
- `activeConvs[].turns`: distinct `codex.user_prompt` records observed during
  the entry's current residency. Model completions do not increment it.
- `activeConvs[].tokens`: latest accepted input count.
- `activeConvs[].id`: process-local pseudonym.
- `activeConvs[].topic`: fixed `"[redacted]"`.
- `activeConvs[].agent`: fixed `"codex"`.
- `activeConvs[].started`: server first-receipt time in local `HH:MM` format.
- `activeConvs[].model`: validated model slug or `"unknown"`.
- `activeConvs[].cost`: always `null`.

Entries are ordered by most recent server receipt. Expiry or cardinality
eviction resets `started` and `turns` if the conversation reappears. At the
64-entry cap, `active_convs` intentionally reports the bounded resident set,
not an unbounded global total.

The bridge never calls `MetricsCollector.record_cost`. Existing independent
top-level cost measurements remain unchanged; only `activeConvs[*].cost` is
guaranteed to be `null`.

## Ordering and replay

The monotonic server clock alone controls TTL and last-seen activity. A
validated OTLP `timeUnixNano` orders token snapshots but never controls expiry.

- A token snapshot replaces the current value only when its OTLP timestamp is
  newer.
- Equal timestamps keep the first accepted snapshot.
- A record without a valid timestamp may refresh a known event's activity but
  cannot replace an ordered token snapshot.
- A successful completion is a recognized SSE/WebSocket completion with a
  valid input-token count. Failed completion records without that count do not
  update tokens.

Counter-changing records use a domain-separated HMAC fingerprint over the
pseudonymous conversation id, event name, event kind, OTLP timestamp, and
projected numeric value. The registry retains only the digest and server
receipt time. Exact retries inside the fingerprint TTL are no-ops. Two events
with distinct timestamps remain distinct even when every projected value is
equal. Without a stable timestamp, the registry does not deduplicate.

## Fixed limits

| Limit | Value |
| --- | ---: |
| Request body after transfer decoding | 262,144 bytes |
| In-flight OTLP requests | 4 |
| Log records per request | 256 |
| Attributes per log record | 64 |
| JSON nesting depth | 12 |
| JSON containers traversed | 4,096 |
| Active conversations | 64 |
| Activity TTL | 600 seconds |
| Fingerprints | 1,024 |
| Fingerprint TTL | 600 seconds |
| Model slug | 128 bytes |
| Token count | 2,147,483,647 |

All caps are constants in the focused module or server and are directly
tested. The request limit is route-local; do not set aiohttp's global
`client_max_size`, because that would alter webhook behavior.

## HTTP contract

Register `POST /v1/logs` only when the configured metrics bind is loopback
(`127.0.0.0/8`, `::1`, or `localhost`). The handler independently requires a
loopback TCP peer and ignores forwarding headers.

The route:

1. rejects non-loopback peers with `403`;
2. rejects non-identity `Content-Encoding`, including gzip, with `415`;
3. accepts only `application/json`, with an optional charset;
4. rejects known or streamed bodies above the route cap with `413`;
5. returns `503` and `Retry-After: 1` when all in-flight slots are occupied;
6. decodes the full batch before mutation;
7. returns `400` for malformed JSON or an invalid OTLP envelope;
8. returns `413` when record, attribute, depth, or container caps are exceeded;
9. returns `200`, `Content-Type: application/json`, and `{}` for a fully
   processed request, including an empty or irrelevant batch.

Error responses use a static JSON `google.rpc.Status`-compatible body and
never echo payloads, headers, exceptions, identifiers, or attribute values.

## Privacy proof

The synthetic fixture includes obvious sentinels in body, prompt, email,
account id, MCP server list, unknown resource attributes, and request headers.
Tests must prove the sentinels and raw fake UUID are absent from:

- registry snapshots and their serialized form;
- `GET /api/cockpit`;
- captured structlog/standard logs;
- the collector's recent-log buffer;
- calls to database-backed collector methods during `POST /v1/logs`.

Tests also assert `record_cost` is never called. Production code must never log
parser exceptions or input-derived values.

## Failure-first delivery tasks

Every task uses its own sequential branch and worktree from the updated feature
branch. Each task must show the named RED failure before production edits,
then GREEN, then run `gitnexus_detect_changes(scope="all", worktree=...)`
before its commit.

### Task 1 — bounded decoder and registry

Allowed files:

- `tests/fixtures/codex_otlp_logs.json`;
- `tests/unit/metrics/test_codex_telemetry_decoder.py`;
- `tests/unit/metrics/test_codex_telemetry_registry.py`;
- `src/brain_v42/metrics/codex_telemetry.py`.

RED:

```bash
pytest tests/unit/metrics/test_codex_telemetry_decoder.py \
       tests/unit/metrics/test_codex_telemetry_registry.py -v
```

The initial failure must be an import or missing-behavior failure. Tests cover
the probe shape, `AnyValue` variants, unknown fields, duplicate attributes,
all limits, UUID/model validation, TTL boundaries, compaction-like token
decreases, old-after-new ordering, equal timestamps, one prompt plus two
completions, exact replay, two legitimate equal-valued records, pseudonyms,
eviction, and `cost is None`.

Before commit, run the targeted tests, full unit suite, Ruff check and format
check over `src/ tests/ scripts/`, and mypy. Commit:
`feat(metrics): normalize bounded Codex OTLP telemetry`.

### Task 2 — loopback OTLP endpoint

Before editing, run fresh upstream GitNexus impact on `MetricsServer`,
`MetricsServer.__init__`, `MetricsServer._build_app`, and the new handler.

Allowed files:

- `tests/unit/test_codex_telemetry_endpoint.py`;
- `tests/unit/test_metrics_server.py`;
- `src/brain_v42/metrics/server.py`.

RED:

```bash
pytest tests/unit/test_codex_telemetry_endpoint.py -v
```

Tests cover success body and media type, empty/irrelevant batch, malformed
JSON, known and chunked oversize requests, record/attribute/depth caps,
unsupported media and encoding, non-loopback bind, non-loopback peer,
forwarding-header spoofing, saturation, atomic failure, concurrent requests,
and webhook/metrics route non-regression.

Before commit, run the same full task gates and GitNexus change detection.
Commit: `feat(metrics): receive Codex OTLP logs on loopback`.

### Task 3 — cockpit projection

Before editing, run fresh upstream GitNexus impact on `MetricsServer`,
`MetricsServer._handle_cockpit`, `CockpitCollector`,
`CockpitCollector.__init__`, and `CockpitCollector._build`.

Allowed files:

- `tests/unit/test_metrics_cockpit_collector.py`;
- `tests/unit/test_cockpit_endpoint.py`;
- `tests/unit/test_codex_telemetry_endpoint.py`;
- `tests/integration/test_cockpit_endpoint_e2e.py`;
- `src/brain_v42/metrics/server.py`;
- `src/brain_v42/metrics/cockpit.py`.

RED:

```bash
pytest tests/unit/test_metrics_cockpit_collector.py \
       tests/unit/test_cockpit_endpoint.py \
       tests/unit/test_codex_telemetry_endpoint.py -v
```

Tests preserve the no-registry fallback, then POST the synthetic batch and GET
the cockpit in one hermetic pytest-aiohttp test. They assert one user turn,
the latest ordered token count, pseudonymous id, redacted topic, `cost=null`,
unchanged top-level cost, cache behavior, expiry, and privacy sentinels. The
live e2e test validates the list schema instead of requiring `activeConvs=[]`.

Before commit, run the same full task gates and GitNexus change detection.
Commit: `feat(metrics): project Codex activity into cockpit`.

## Coordinator verification

Run on the integrated feature branch:

```bash
pytest tests/unit/ -v --tb=short
pytest tests/unit/ --cov=brain_v42 --cov-report=term-missing \
  --cov-fail-under=60
ruff check src/ tests/ scripts/
ruff format --check src/ tests/ scripts/
mypy src/
git diff --check main...HEAD
```

Run `pytest tests/integration/ -v --tb=short` only against a dedicated
`BRAIN_V42_TEST_DB_URL`; never fall back to the production database. Build the
Dockerfile without pushing an image. Record explicit skips or unavailable
local infrastructure as evidence; they do not count as a passing gate.

Run `gitnexus_detect_changes(scope="compare", base_ref="main")` and review all
affected symbols and execution flows. Then perform one whole-branch
`red-reviewer` pass. Fix every High/Critical finding and rerun the affected
gates.

## Merge and push

1. Fetch `origin` and confirm the feature branch still merges cleanly.
2. Require a clean, synchronized `main`.
3. Merge the feature branch into `main` with `--no-ff`.
4. Repeat the full coordinator gates and GitNexus change detection on merged
   `main`.
5. Push `main` with a normal, non-force push.
6. Update Brain with the final commits, tests, review findings, residual risk,
   and operator handoff. Keep the current Brain session open.

## Separate operator rollout

After deployment, an operator may add this user-level configuration:

```toml
[otel]
environment = "local"
log_user_prompt = false
trace_exporter = "none"
metrics_exporter = "none"
exporter = { otlp-http = { endpoint = "http://127.0.0.1:9200/v1/logs", protocol = "json" } }
```

The operator then restarts `brain-metrics.service` and each Codex client that
must load the setting. Never enable `log_user_prompt`.

Post-deployment acceptance:

1. After the sidecar receives an OTLP batch, the conversation appears in
   `GET /api/cockpit` after at most the one-second sidecar cache.
2. `ctx_tokens` follows the newest OTLP timestamp and may decrease after
   compaction.
3. The entry expires after ten minutes without a recognized event.
4. Raw UUID, prompt, email, account id, MCP servers, and tool output do not
   appear in the API, service logs, or PostgreSQL.
5. `activeConvs[*].cost` remains `null`; top-level independent cost remains
   unchanged.
6. `/metrics`, `/api/cockpit`, webhook behavior, and automation ownership
   remain healthy.

Codex exporter batching and red-monitor's proxy/UI caches are outside the
one-second sidecar guarantee. Measure end-to-end latency separately.

## Rollback

Exporter rollback: set `exporter="none"` or remove the user-level `[otel]`
block, then restart Codex clients.

Software rollback: deploy the preceding known-good commit or image, restart
the sidecar, and smoke `GET /metrics` plus `GET /api/cockpit`. No data
migration or cleanup is required because the bridge stores no durable data.

## Out of scope

- OTLP traces, metrics, gRPC, binary Protobuf, gzip, or a general collector.
- Parsing `~/.codex/sessions` or extending `codex_dream_parser`.
- Exact session-close detection or durable conversation history.
- Prompt-derived topics, raw identities, tool output, or user attribution.
- Token-price lookup or ChatGPT subscription cost estimation.
- red-monitor UI work.
- Changes to `Settings`, `MetricsRuntime`,
  `BRAIN_DREAM_CAPABILITY_ENFORCEMENT`, or SEC1 rollout state.
