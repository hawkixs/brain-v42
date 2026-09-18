# Migration 054 (delivery attestations) — production receipt

**Status: FINAL — production reads `054`, release `9bdb3812` is live on the eight writers,
and the idempotence canary passed: one `gate_passed` attestation emitted twice under the same
idempotency key landed as ONE row.** Brain ticket `04bc1f4a`, resolution criterion (2).

Operator: Hawixs, one session, 2026-09-18 14:08 → 14:24 CEST (12:08 → 12:24 UTC), outside
the Dream window. Procedure: [`2026-09-18-delivery-attestations-054-apply.md`](../runbooks/2026-09-18-delivery-attestations-054-apply.md)
over the scripted path of the 2026-09-15 handoff (`~/.local/state/brain-v42-delivery/handoff-2026-09-18/`,
copy of the 09-15 script patched by three anchor-unique patch files kept beside it).
`VERSION=0.6.0` and `MEMBER_VERSION=0.2.0` unchanged. Feature: the append-only attestation
ledger — table `delivery_attestations`, tools `brain_delivery_attest` and
`brain_delivery_attestation_list`, API v1.0 frozen with red-rail and published as data in
`docs/contracts/delivery_attestations.json` (`contract_version` 1, last written by `8349944a`,
SHA-256 `b654b9a3…`).

## Release record

| | `9bdb3812` |
|---|---|
| Source | merge of PR #151 (`feat/delivery-attestations-054`) at `2026-09-18T10:51:40Z`, nine App `15368` checks `SUCCESS`, head `005d1776` |
| Predecessor | `e11e3660`, measured on `brain-mcp-http` `ExecStart` at 12:08 UTC — never copied |
| Window (UTC) | `20260918T121649Z`: build 12:16:49 → 12:17:03, prove 12:18:52 → 12:19:09, cutover 12:19:41 → 12:19:52, canary 12:19:59 → 12:20:00 |
| Wheels | `brain_v42-0.6.0` `808757f0…`, `headless_agents-0.2.0` `33dad2e1…` (316 package files, 69 source files, 15 member files) |
| Source archive / manifest | `877c8076…` / `871c36bd…` (lock `a42138e9…`, interpreter `a38cb57f…`, guarded minimum `fcc9328f…`) |
| Migration in the wheel | `brain_v42/alembic/versions/054_delivery_attestations.py` `17d2a9a6…`, smoke `alembic head 054` |
| Alembic 053 → 054, production | started `2026-09-18T12:19:42Z`, finished `2026-09-18T12:19:43Z`, `Running upgrade 053 -> 054`, writers quiescent (eight `MainPID=0`) |
| Post-checks | head `054`; 9 `delivery_%` tables; 4 indexes on `delivery_attestations` (`pkey`, `ix_…_issuer_emitted`, `ix_…_ticket_emitted`, `uq_delivery_attestation_idempotency`); 0 rows before the canary |
| Dormant health | `{"status":"ok","version":"0.6.0","alembic_head":"054"}` (`88cd2e4d…`) |
| Dormant preflight | `{"schema_revision":"054","source_sha":"9bdb3812…","status":"ok","timestamp":"1789733990"}` (`cd79ee2f…`) |
| Active preflight | mode `canary`, probe PR 151, observer required active: `status ok`, `054`, timestamp `1789734215` (`7bf7f9c4…`) |
| Drop-ins | 8 rendered, identical to live under SHA mask; `91-delivery-mode.conf` republished unchanged; no `zz-` drop-in before, during or after |
| `unit-state` before = after | `a391e319…` — Dream unit kept in its measured `failed` state (night of 09-18, Codex quota) |
| Triggers frozen and restored | 4 (`6f66cf78…`, the same list as every release since 09-13), watchdog last; observer restarted, active and enabled |

`brain_test` was measured at `054` the same afternoon, upgraded through the final chain on
09-18 before the merge; the repository head and both databases agree.

## Backup and disposable recovery proof (before any production write)

Dump `brain-v42-pre054-9bdb38122fb7603927f18d1aaec8544ae4b1300d.dump`, custom format,
owners and ACLs preserved, taken from the production container with head `053` read before
and after: SHA-256 `a3ca75b4e50cfebc635f84862d302b953c348581e512854cc0aac5ea47374219`;
globals `ef4bb852…`. Restored with `--create --exit-on-error` into a disposable container on
the pinned image `sha256:b295c2aa…` (bridge, tmpfs, no port, no bind, no volume), removed
after the proof. The clone was proved twice, as the runbook prescribes:

| Proof | Head | Contract | Result | Receipt SHA-256 |
|---|---|---|---|---|
| As restored | `053` | v10 (`brain-v42/postgresql-recovery/v10`, 43 tables, 46 FK, 156 indexes) | 30/30, ACL 1/1 | `c448881e…`, ACL `ec784a19…` |
| After `alembic upgrade 054` with the release venv | `054` | v11 (`brain-v42/postgresql-recovery/v11`, 44 tables, 48 FK, 160 indexes) | 30/30, ACL 1/1 | `ef0ba5c9…`, ACL `00a4bdd1…` |

Recovery summary `recovery-proof-summary.json` SHA-256
`c35c94ca09b77b676ac4ddc8ade70c1cbd5c0212d40bfeddcc62a544373b3a62`. The v11 assets were
pinned by the three SHA-256 values the runbook names before the proof ran.

## The idempotence canary

Subject: ticket `78fc643a-f3ca-45fe-8a0d-497283ac7d5d`, `actor_project` `brain-v42`,
through the reference client (Streamable HTTP on the loopback MCP, bearer read raw from the
private file `~/.config/brain-v42/delivery-canary-mcp-token-9bdb3812…` named by the
`mcp_token_file` key of a per-invocation private JSON retained beside the result, headers
`X-Brain-Tool-Profile: native` and `X-Brain-Agent: release-canary-054`), from the release venv
after activation, 12:20:00 UTC.

```
canary ok 7ac69792-d953-4dd8-a57c-f8485c0db167 9546e3fd7e992c3e91a05f0dc7b87e90c92d17b180f9e2f605e4a5bfe9651753
```

The four facts frozen with red-rail, proved live: the replay returned the same `id`; a
reused key with other content was refused with `idempotency_key_reused`; a float payload was
refused with `invalid_payload` and wrote nothing (0 rows under `canary-054-float:…`); the
fact is readable in both scopes (`brain_delivery_attestation_list` with `issuer_project`,
and `brain_delivery_get` on the ticket) with the digest `canonical_digest(payload,
domain="attestation")`. Row (`attestation-canary.row.txt`, `78ce9c98…`): `issuer_project`
`brain-v42`, `issuer_identity` `release-canary-054`, `contract_revision` null, `emitted_at`
`12:20:00.388Z`, `recorded_at` `12:20:00.445Z`. `SELECT count(*), count(DISTINCT id)` under
`canary-054:9bdb3812…` reads `1|1`; the table holds exactly this one row. It stays: it is a
true attestation about this release.

## Three defects the window found in the written procedure, and their corrections

None of the three touched production; each was caught by a fail-closed check before or
after the gesture it guards, and each is corrected in the runbook by this pull request.

1. **The private-target attestation refused on a stale literal.** The 09-07 block, inherited
   by reference, requires `BRAIN_DELIVERY_REPOSITORY_REGISTRY` to equal the single-project
   literal of 2026-09-08. The GitHub App cutover of 2026-09-11 extended the registry to every
   ReD repository, identically in the observer env and the MCP env. The dry attestation the
   handoff script runs in `prove` (before any dump) refused at 12:17 UTC; the check now
   requires the `brain-v42 → 1337360966:hawkixs/brain-v42` pair and byte equality of the
   parsed registry between the two private files — the real invariant of the 09-11 runbook —
   and passed at 12:18. Two empty files and one empty directory were the only residue.
2. **The canary post-check used `min(uuid)`.** PostgreSQL defines no `min`/`max` aggregate on
   `uuid`; the runbook's `(min(id) = max(id))` errors out. `count(DISTINCT id) = 1` carries
   the same proof and is what the row-count evidence records.
3. **The bearer is not a fixed JSON.** The runbook's canary names
   `~/.config/brain-v42/delivery-canary.private.json`, which has never existed. The reference
   client holds its bearer in the raw file `delivery-canary-mcp-token-<SOURCE_SHA>` (derived
   from `MCP_HTTP_TOKEN` without ever printing it), and reads its path from the `mcp_token_file`
   key of a **per-invocation** private JSON — the form `scripts/verify_delivery_canary.py`
   and the 09-07 runbook already prescribe.

## Pin for red-rail

Three things, sent to the `red-rail` session at 14:41 CEST (cross-session message
`86bc2623`) and copied to the thread of ticket `04bc1f4a` for the durable trace:

- `SOURCE_SHA` `9bdb38122fb7603927f18d1aaec8544ae4b1300d`, the merge of PR #151 and the
  immutable release running on the eight writers.
- Tag **`delivery-attestations-v1.0`**, annotated (`81dfab87`), on that exact commit, pushed
  to `hawkixs/brain-v42`. Operator's decision of 14:38 CEST between a `pyproject` bump to
  `0.7.0` with `v0.7.0` (the release rail refuses a tag that does not name the built
  version, and the tag would not have pointed at the deployed commit) and a contract tag
  outside the `v*` pattern: the contract tag, on the convention `headless-agents-v0.2.0`
  set on 2026-09-15. It names the contract's version (`contract_version` 1, tools 1.0),
  never the package's (`0.6.0` unchanged), and moves only on a breaking change. No release
  run was triggered.
- Contract path at that tag: `docs/contracts/delivery_attestations.json`, SHA-256
  `b654b9a3479f02c3d80baf1d49777fb93356b5d34a7cd17b70e5e2205fd9a3fe`, verified identical
  through the GitHub API at `?ref=delivery-attestations-v1.0` — readable without Brain
  network access, which is what red-rail's boundary test needs.

And how the reference client holds its bearer: a private file whose absolute path is the
`mcp_token_file` key of its JSON configuration, read raw and trimmed (`0600`, owner-only,
printable ASCII, no trailing newline), never an environment variable, never in a repository,
never a command argument; headers `Authorization: Bearer`, `X-Brain-Tool-Profile: native` and
`X-Brain-Agent` (refused `invalid_issuer` without it; it becomes `issuer_identity`, while
`issuer_project` is the participant `actor_project`). red-rail may write against the API
from this message on.

## Rollback

Compatible forward rollback, 054 in place: `rollback` mode of the same handoff script with the
same window republishes the backed-up drop-ins of `e11e3660` and restarts; the previous
release runs unchanged on a database carrying one more table with one canary row. Never
`alembic downgrade`: with the canary row present the downgrade refuses without its named
opt-in, and that refusal is the design.

## Limits

The `dr-current` declaration of `docs/PLAN_INDEX_REPAIR_RUNBOOK.md` still announces head
`052` measured on 2026-09-03; it was left stale by the 053 apply of 09-08 and is not touched
here — a live replay of v11 against production and a redated row are a separate gesture.
Delivery persistence uses `READ COMMITTED`; these observations do not prove the absence of
unmeasured writers. Caller identity is established at the MCP `X-Brain-Agent` boundary. The
dump and every private log stay owner-only under the evidence directory
`~/.local/state/brain-v42-delivery/9bdb3812…/20260918T121649Z/`.
