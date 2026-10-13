# `programmes/v1` — publisher contract

Status: frozen 2026-10-13 with red-monitor (relay, ticket `1f99fce3`) and red-cockpit
(renderer, ticket `2fa85286`). Answers ticket `24c1f172`. Ticket `8a063256` asked for
the block. The machine-readable shape is in `programmes_v1_block.json`, and sample
blocks are in `programmes_v1/`. Tests pin all three against the publisher.

Owners: brain-v42 owns the block, its fields, its vocabulary and who may write what.
red-monitor owns the HTTP route `GET /api/v1/programmes`, publisher selection and the
transport metadata. red-cockpit owns parsing and rendering.

## 1. Where the block lives

- **Path**: `GET /programmes` on the brain metrics sidecar (the `metrics` service of a
  brain-v42 deployment). It shares the listener and the exposure of `GET /metrics`.
  The body is `{"programmes": <block>}` while brain publishes, and `{}` while it does
  not. The block is never nested deeper.
- **Not in `/metrics`.** The block does not appear in the body of `GET /metrics`, so it
  adds nothing to the payload that red-monitor's agent stores on every probe of the
  brain card. Ticket `8a063256` asked for a key of `/metrics`. It moved to its own
  route at the freeze, and the member name and the shape stayed the same.
- **Publishers**: one per brain-v42 deployment that runs a release carrying this
  contract. As of the freeze there is exactly one: red-base. The workstation stack is
  stopped and frozen as a rollback. It runs a release older than this contract, so it
  has no such route.
- **Service identity**: the allowlist pair and its `source` label belong to
  red-monitor's configuration. At the freeze the pair is the workstation agent that
  probes red-base, with the label `red-base`. The route returns the label, never the
  agent name. Selecting a publisher by key presence alone is not allowed.

## 2. Block shape

```json
{
  "version": "programmes/v1",
  "generated_at": "2026-10-13T09:00:00.123456+00:00",
  "programmes": [{
    "key": "brain-move-red-base",
    "title": "Brain to red-base",
    "coordinator": "brain-v42",
    "state": "active",
    "last_change": {"gate": "G12", "from": "todo", "to": "in_progress",
                    "at": "2026-10-13T08:58:10.004211+00:00", "by": "brain-v42"},
    "gates": [{
      "key": "G7",
      "title": "red-backup deployed on red-base",
      "owner": "red-backup",
      "state": "done",
      "depends_on": ["G6"],
      "weight": 4,
      "waiting_on_operator": null,
      "progress": null,
      "note": null,
      "updated_at": "2026-10-13T08:40:00+00:00",
      "updated_by": "red-backup"
    }]
  }]
}
```

| Field | Type | Meaning |
|---|---|---|
| `version` | string | Exactly `programmes/v1` for every v1 block |
| `generated_at` | RFC 3339 | When the sidecar computed the block from the database (section 4) |
| `programmes` | array | Published programmes, in brain's order (section 5) |
| `programme.key` | string | Stable, unique, `[A-Za-z0-9][A-Za-z0-9._-]*`, at most 64 characters |
| `programme.title` | string | One line, at most 120 characters |
| `programme.coordinator` | string | Project key of the coordinating project, derived by the server from the creator's credential. It is never a free label |
| `programme.state` | `active` \| `paused` \| `done` | Declared by the coordinator |
| `programme.last_change` | object \| null | The latest gate **state transition** in this programme. `null` until the first one |
| `last_change.gate`, `from`, `to` | string | Gate key, previous state, new state (`from` differs from `to`) |
| `last_change.at`, `by` | RFC 3339, string | When the transition was written, and the identity of the writer |
| `gate.key` | string | Unique in its programme, `[A-Za-z0-9][A-Za-z0-9._-]*`, at most 32 characters |
| `gate.title` | string | One line, at most 120 characters |
| `gate.owner` | string | Project key that owns the gate and may report on it |
| `gate.state` | `todo` \| `in_progress` \| `blocked` \| `done` | Declared by the owner or the coordinator, never measured |
| `gate.depends_on` | array of strings | Keys of other gates of the same programme. It may be empty, and is never `null`. brain refuses at write a self-edge, an unknown key and a cycle |
| `gate.weight` | integer, 1 to 100 | Relative remaining effort, default 1. The cockpit's critical path is its only use |
| `gate.waiting_on_operator` | object \| null | `{ask, since}`. `ask` is one line, 1 to 200 characters. `since` is when this ask was raised |
| `gate.progress` | object \| null | `{done, total, unit}`, with `0 <= done <= total`, `1 <= total <= 100000` and `unit` of 1 to 24 characters |
| `gate.note` | string \| null | One line, 1 to 200 characters, or `null` |
| `gate.updated_at`, `updated_by` | RFC 3339, string | When the gate was last written, and who wrote it (section 6) |

**Every key is always emitted.** No optional key is omitted. "None" is `null` for
`last_change`, `waiting_on_operator`, `progress` and `note`, and `[]` for `depends_on`.
`weight` is always an integer: 1 when the writer gave none.

Text fields are checked **at write**: a write that breaks a limit, or that carries a
control character, a line break or a bidirectional override, is refused. Nothing is
silently cut. The cockpit's own 200-character cut and character stripping stay as a
consumer safeguard.

## 3. Absent, empty and null

| What `GET /programmes` answers | Meaning |
|---|---|
| `200 {}` (no `programmes` key) | Not published this cycle: the block's database read failed. It is retried within about 5 s (`error_ttl_seconds`). The block is never replaced by an older copy |
| `200 {"programmes": {"version": "programmes/v1", "generated_at": "…", "programmes": []}}` | Published, and nothing to show |
| `404` | The release predates this contract. red-monitor's probe fails and the route reads `no_publisher` |
| `"programmes": null` | Never emitted by brain. A relay may treat it as absent |

## 4. Timestamps

- **Format**: RFC 3339 date-time, always in UTC. brain writes the offset as `+00:00`
  and fractional seconds as 0 or 6 digits. Consumers parse with any RFC 3339 parser and
  compare instants, never strings.
- **`generated_at`**: the sidecar's wall clock when it computed the block. The block is
  memoised with a single flight (decision `1669d429`): one computation every 30 s at
  most, shared by every concurrent poll. At the moment the sidecar serves it,
  `generated_at` is therefore at most about 30 s old. It is not the time of the
  request.
- **`updated_at`, `since` and `last_change.at`**: the database server time at which the
  write ran. They are never taken from a tool argument. A seeded gate therefore carries
  its seed time, and no historical date is invented.
- **Order guarantees**: the sidecar and the database share the red-base host clock, and
  a row is read only after it is committed. So `updated_at <= generated_at` holds in
  practice. A consumer still clamps a negative age to 0 and never rejects a block for
  it.
- **No future, no missing**: brain emits no timestamp that is missing, invalid, or
  ahead of its own `generated_at` by design.

## 5. Order, counts and size

- **Order**: programmes and gates come in brain's order: the declared position, then
  creation time, then the key. Consumers sort nothing, except the cockpit's "for you"
  list.
- **Published programmes**: every `active` or `paused` programme, plus the `done`
  programmes updated in the last 7 days, at most the 4 most recent. A `done` programme
  past that window stays readable through `brain_programme_list`.
- **Write-time caps**, enforced by refusal: at most 8 programmes `active` or `paused`;
  at most 32 gates per programme; at most 12 keys in `depends_on`. One programme
  serialised as published must stay within **16 KiB**.
- **Block size**, by construction: at most 12 programmes of 16 KiB each, which is
  192 KiB plus the envelope. red-monitor's transport ceilings (256 KiB for the block,
  272 KiB for the whole response, `502 payload_too_large`) are accepted, and a
  conforming brain never reaches them.

## 6. Who writes, and who is named

The MCP tools are `brain_programme_upsert`, `brain_programme_gate_upsert`,
`brain_programme_gate_report`, `brain_programme_request` and `brain_programme_list`.
Their details are in `docs/MCP_TOOLS.md`.

- **Identity**: the caller's project, as resolved by the credential layer from the
  verified bearer and its accepted actor. A call that has no resolved project is
  refused (`caller_unidentified`). No argument names the writer.
- **`updated_by`, `last_change.by` and `coordinator`** carry that project key. No
  declared session label is published.
- **Authority**:
  - The project that creates a programme becomes its coordinator.
  - Only the coordinator may change the programme (title, state, order) and the
    structure of its gates (create, title, owner, `depends_on`, weight, order).
  - The gate's owner and the coordinator may report its state, progress and note, and
    may raise or lift its request to the operator.
- **A request leaves** the operator's list only when the owner or the coordinator
  lifts it (`waiting_on_operator` back to `null`). Raising the same ask again keeps its
  `since`. A different ask gets a new `since`.
- **`last_change`** moves only on a state change of an existing gate. Creating a gate,
  or reporting the same state again, does not move it. Any accepted write to a gate
  refreshes that gate's `updated_at` and `updated_by`.
- The cockpit and red-monitor never write.

## 7. Versioning

- `version` stays `programmes/v1` while changes are **additive**: a new optional key, a
  new value in a vocabulary that consumers already render as unknown.
- Removing, renaming or retyping a key, or changing what one means, is a new major
  (`programmes/v2`). It goes through a coordinated change in red-monitor and
  red-cockpit before brain publishes it.
- **Reserved names**: brain never emits `served_at`, `source_age_s`, `source`,
  `available`, `reason` or `payload` at the top level of the block, in v1 or any later
  major. This keeps the relay's **flat** envelope free of collisions (section 8).

## 8. Relay envelope (red-monitor's, recorded here for both sides)

`GET /api/v1/programmes` answers `200` with the selected block's top-level keys
unchanged, plus `served_at`, `source_age_s` and `source` beside them. This is the
envelope that red-cockpit's parser already reads. The nested `payload` variant is
dropped. When nothing can be served, the answer is
`{"available": false, "reason": "<code>"}`. red-monitor owns the codes:

| Code | Status | When |
|---|---|---|
| `no_publisher` | `200` | No publisher is configured, or none has a live snapshot (a snapshot reloaded from storage does not count) |
| `not_published` | `200` | A live snapshot carries no `programmes` member |
| `unsupported_version` | `502` | The selected block has an unsupported major |
| `payload_too_large` | `502` | The block is over 256 KiB, or the response over 272 KiB |
| `invalid_payload` | `502` | The block emits a reserved top-level name. It is refused, never overwritten |

Selection between publishers, as agreed with red-monitor:

- **Freshest wins.** The freshest is the greatest parsed `generated_at`.
- **Ties** go to the publisher listed first in the allowlist.
- **An undated block** (missing or unparseable `generated_at`) ranks below every dated
  one.
- **Mixed majors**: when the freshest block has an unsupported major, the answer is
  `502`, with no fallback to an older block, the same as for an oversize block.

`source` is the label of the allowlist entry (`red-base` at the freeze). It is never
the name of the agent that pulled. `source_age_s` is red-monitor's own clock: whole
seconds since its last successful pull, never negative.

## 9. Fixtures

- **brain owns the block samples** in `programmes_v1/`. They come from brain's own
  serializer and are pinned by brain's tests.
- **red-monitor's `contracts/programmes-v1/`** is canonical for the route. It holds
  copies of brain's samples, pinned to the brain commit they came from, and the
  envelope cases: the whole sidecar payload with the block absent, a malformed gate
  beside valid ones passed through unchanged, and an unknown major.
- **red-cockpit pins red-monitor's fixtures.**
- `brain_move_g1_g12.json` is **illustrative**. It uses the structure confirmed by the
  coordination, with `depends_on` from `8a063256` and G7 at weight 4. Its states and
  times are examples. The live states are declared at seed time, each against its
  evidence.

## 10. Exclusions

The block carries no secret, prompt, log line, credential identifier, session id or
client key. Its text comes from declared one-line fields, checked at write.
