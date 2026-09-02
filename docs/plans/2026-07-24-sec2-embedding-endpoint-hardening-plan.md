# SEC2-A — Network boundary and resources of the embedding endpoint

Date: 2026-07-24

Branch: `feat/sec2-embedding-hardening`

Base: `main` at `c5fd9e1`

Brain ticket: `530d796a-42e8-48d9-91a2-5d2a17fdb53b`

## Objective

Harden the versioned state of the canonical embedding service: prepare a host publish that is
loopback-only and reject, before any compute, bodies, batches and concurrency that exceed an
explicit contract. The runtime stays unchanged and LAN-wide until a separate operator rollout.
The change must preserve the envelopes of the known local Brain and Docker consumers, keep the
end-to-end healthcheck, and not present authentication as delivered while the active
`auto-discord` client has not received the same secret.

## Observed state

- `embedding-shim` and the `embedding` rollback currently publish `8003:8003`, i.e. every IPv4/IPv6
  interface of the host.
- The shim reads `request.json()` with no body limit, accepts batches of arbitrary size, and does
  not bound the number of concurrent computations. Only each text is truncated to 20,000
  characters in the backend.
- The internal llama server is not published on the host and runs with `-np 1`.
- Active Brain processes use `http://localhost:8003`. The optional gateway uses the Docker DNS
  name `embedding-shim:8003`.
- `auto-discord` actively calls `brain_v42_embedding_shim:8003` over the external `hawkixs-infra`
  network, with no authentication header. Its traffic does not go through the host publish.
- The only hardcoded LAN endpoint found in `red-shrik` is `192.168.1.11:8003`; it points to the
  old `red-data`/nomic service on the dev-pc, not to the canonical Brain shim on the PC serveur.
- Logs observed over five days show 4,263 `/embed` calls from `auto-discord` and no distinct LAN
  consumer identified. `docker-proxy` masks the source address of any calls made through the
  publish, though: this observation is not proof of absolute absence.
- The llama.cpp digest is already pinned by OPS1. The `deploy/dev-pc` path is explicitly
  superseded for active Brain traffic.

## Contract decisions

1. The publishes of the canonical shim and of the legacy rollback become
   `127.0.0.1:8003:8003`. Uvicorn stays on `0.0.0.0:8003` inside the container so it can serve
   Docker networks.
2. The maximum raw body of a compute request is **8 MiB**. This envelope accepts the actual JSON
   serialization from maintained `httpx` clients for 100 texts of 20,000 characters, even at four
   UTF-8 bytes per character; a test builds this maximum envelope instead of assuming its
   overhead. The limit is checked against `Content-Length` when present, then against the bytes
   actually received; exactly 8 MiB is still accepted, the first extra byte produces
   `413 {"detail":"Request body too large"}`.
3. Reading a body in full must finish within **5 seconds**. A stream that is too slow produces
   `408 {"detail":"Request body timeout"}`. At most **8 bodies** are read/validated concurrently
   per worker, which bounds raw memory to 64 MiB; an extra admission gets
   `503 {"error":"ingress_busy"}` with `Retry-After: 1`. This distinct response is never counted
   as GPU contention. The timeout stops eight slowloris connections from holding slots
   indefinitely.
4. `/embed` accepts at most **100 texts**, the maximum of the maintained backfill CLI, and
   `/rerank` at most **128 candidates**, above the observed coalesced maximum of 120. An overflow
   produces respectively
   `400 {"detail":"texts must contain at most 100 items"}` or
   `400 {"detail":"candidates must contain at most 128 items"}`, with no backend call. Empty
   batches and the legacy query-param stay valid. The unbounded `regen_embeddings` CLI is aligned
   to 100 in this batch.
5. After reading/validation, at most **1 embedding computation** and **1 rerank computation** are
   active per worker. Embedding saturation produces `503 {"error":"gpu_busy"}`; rerank saturation
   produces `503 {"error":"service_busy"}`. Both responses carry `Retry-After: 1`. These 5xx
   responses reuse the existing retries/fallbacks, unlike a `429` or another 4xx.
6. A missing or zero-length body keeps the historical query-param fallback. A body made only of
   whitespace or syntactically invalid JSON produces
   `400 {"detail":"Invalid JSON body"}`. `{}`, `null` and `[]` remain payloads with no value:
   the query-param can replace them on `/embed/query` and `/embed/single`; `/embed` and
   `/rerank` respond with their existing contract error.
7. The Compose healthcheck stays the real `POST /embed`, with no secret bypass or reserved
   capacity. Under GPU saturation, it quickly gets `503 gpu_busy`; after release, the same probe
   goes back to `200`. This unavailability is observable and must not be masked by a shallow
   `/health`. The rollout will need to verify Docker status behavior under load.
8. Errors are short JSON and echo back neither body, nor text, nor secret in the response or the
   logs.
9. The static per-file `0600` bearer stays the authentication target. It is not enabled in this
   batch: enabling it server-side only would break the `auto-discord` hourly pipelines. A
   cross-project ticket must make this client compatible and prepare the atomic cutover. The main
   SEC2 ticket stays open after SEC2-A.

## Acceptance criteria

1. The Compose root's only two `:8003` publishes are explicitly loopback; no host network mode is
   introduced and the internal Docker URLs stay unchanged.
2. The shim applies the limits above to the four compute routes before the backend, including with
   a streamed body with no `Content-Length` or deliberately slow.
3. Tests prove the exact N/N+1 boundaries for the body, `/embed`, `/rerank`, ingress, and both
   resources, as well as the absence of a backend call on every rejection.
4. Tests prove the real `POST /embed` of the healthcheck under saturation then after recovery, and
   the release of each capacity after success or exception. After cancellation, the GPU lease is
   only released once its backend task ends; the ONNX lease stays held until the thread's physical
   end, never merely at the client's departure.
5. The existing `/embed`, `/embed/query`, `/embed/single`, `/rerank`, `/`, `/healthz` and
   `/health` contracts stay compatible for their valid cases.
6. The shared README/CLAUDE/architecture documentation contract distinguishes the loopback target
   configuration from the still-LAN-wide runtime before rollout. It describes the shim's limits
   and the auth/Docker network/legacy remainder without claiming full WAN isolation.
7. A coordinated ticket documents the `auto-discord` client to migrate to a bearer read from a
   mounted secret; a separate ticket documents the historical `red-shrik` URL to make
   configurable without implicitly changing the model.
8. Sentinels present in invalid and oversized bodies are absent from the captured responses and
   logs.
9. The targeted tests, the full suite, Ruff, format, mypy, shim compilation, Compose, and
   `git diff --check` are green. Two independent reviews of the full diff conclude `SHIP` with no
   open P0–P3 finding.
10. Before each commit, `gitnexus_detect_changes` confirms an expected radius. After merge, both
   `main` remotes point at the same SHA and that SHA's GitLab pipeline is green.

## Non-goals and boundaries

- Do not deploy, recreate, or restart the shim, the model, MCP, or `auto-discord`.
- Do not modify the `auto_discord` or `red-shrik` repository on this branch.
- Do not enable a bearer in partial mode, do not create a secret, and do not write a token into
  Git, command arguments, or logs.
- Do not remove the shim from the shared `hawkixs-infra` network yet; that requires a coordinated
  `auto-discord` Compose change and the creation of a dedicated client network.
- Do not modify `deploy/dev-pc`, its supervisor, or its Docker socket in this batch: this path is
  superseded and remains a separate SEC2 sub-batch if it needs to be kept as a rollback.
- Do not modify the models, dimensions, normalization, text truncation, or scores.
- Do not claim to close SEC2 globally: authentication, the dedicated Docker network, and the
  supervisor remainder stay open with explicit owners and proof.
- The application caps in this batch belong to the canonical shim. The legacy PyTorch rollback
  becomes loopback but stays unbounded, and its DNS name does not preserve `auto-discord`; it
  must not be presented as a safe SEC2 rollback before a dedicated ingress/alias batch.

## TDD breakdown

### Task 1 — Request limits and concurrency in the shim

Files: `services/embedding_shim/shim_app.py`, `services/embedding_shim/main.py`,
`services/embedding_shim/shim_backends.py`, `scripts/regen_embeddings.py`,
`tests/unit/test_embedding_shim.py`, and the relevant regeneration CLI tests.

Before editing, analyze the upstream impact of `create_app`, `_json_or_none`, and the relevant new
extension points; warn before continuing if GitNexus returns HIGH or CRITICAL.

RED: add the tests for maximum `httpx` serialization, declared/streamed body N/N+1, read timeout,
exact JSON shapes, batch 100/101, candidates 128/129, eight blocked reads then `ingress_busy`,
separate GPU and ONNX gates, the real Compose probe under saturation/recovery, and capacity
release on every path. Use `httpx.AsyncClient` + `ASGITransport`, an embedding backend driven by
`asyncio.Event`, and an ONNX backend driven by `threading.Event`. After cancelling the rerank
client, prove the second computation stays refused until the thread actually unblocks. Test the
ASGI layer directly for streamed reads and the timeout. Record the actual failures.

GREEN: introduce an immutable limits contract, a bounded/timed body read, an ingress gate, and two
resource gates restricted to the compute routes. Align the regeneration CLI to the 100 maximum and
fill in the three missing shim annotations so its mypy gate is actually green. Keep the responses
and the nominal path minimal.

### Task 2 — Loopback bind and documentation contract

Files: `docker-compose.yml`, `tests/unit/test_documentation_contract.py`, `README.md`,
`CLAUDE.md`, `docs/ARCHITECTURE.md`,
`docs/plans/2026-07-11-sol-ultra-audit-roadmap-plan.md`, and this plan.

RED: make the documentation contract expect the two loopback bindings and the new boundary text;
prove the baseline still fails on `8003:8003` and the open-LAN statement.

GREEN: modify only the two root publishes, align the documents on "code-ready, not deployed", and
validate the resolved Compose. The internal networks and URLs stay identical.

### Task 3 — SEC2-A coordination and proof

Create a `brain-v42 → auto-discord` ticket in Brain for the bearer migration + dedicated client
network, as well as a `brain-v42 → red-shrik` ticket to make the QA URL configurable and clarify
model ownership. Add to the main SEC2 ticket the commits, counters, limits, and remaining
sub-batches; do not resolve it. Also record the unbounded legacy and the shim Dockerfile's CI
build as remainders, without silently expanding this batch.

## Verification

```text
uv run pytest tests/unit/test_embedding_shim.py tests/unit/test_documentation_contract.py -q
uv run pytest tests/unit/test_docker_compose.py -q
docker compose config --quiet
uv run python -m compileall -q services/embedding_shim
uv run python scripts/check_container_image_pins.py
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ services/embedding_shim
uv run pytest -q
git diff --check
git diff --check main...HEAD
```

Run `gitnexus_detect_changes` before each commit. After merge: repeat the proportional gates from
`main`, push without force to `origin/main` and `gitlab/main`, verify the remote refs, then wait
for the exact GitLab pipeline.

## Rollback

Reverting the commit(s) restores the previous publishes and behavior. No data, image, Docker
resource, live configuration, or secret is modified by this delivery. If the loopback bind must be
applied later, the deployment runbook will need to check local host traffic, the `auto-discord`
Docker DNS, the four compute routes, the healthchecks, and the rollback before any soak.

## Delivery evidence

- Plan critiqued from three independent angles then validated `SHIP`: commit `34ca3e5`.
- RED matrix verified on limits, saturation, and CLI: commit `210660d`.
- Shim GREEN: commit `13e53c0`; 61 targeted shim/CLI tests pass, along with a widened
  embedding/reranking matrix of 174 tests including these 61.
- 8 MiB body, pathological JSON, ingress 8+1, compute 1+1, errors/cancellations, and sanitized
  detached logs are covered. Shim mypy, Ruff, format, and `git diff --check` pass.
- Four independent reviews — plan, security, quality, and history — conclude `SHIP` with no open
  P0–P3 finding.
- Coordination tickets created: `9ef5c69d-cfd3-4f07-93c5-2c599ea2197b` for `auto-discord`
  and `89140780-b853-437b-b902-86dab64cd866` for `red-shrik`.
- No deployment, restart, secret, or live network was created. The main SEC2 ticket stays open for
  authentication, the dedicated network, the rollout, and the legacy/supervisor remainders.
- The final SHA, remote concordance, and the exact GitLab pipeline will be recorded in the SEC2
  Brain ticket, since this evidence is produced after this immutable documentation commit.
