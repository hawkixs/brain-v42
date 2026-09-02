# Handoff — 2026-08-10, evening

Written because the MCP client of the emitting session died during the cutover (see
§1). The brain, itself, did receive everything: this file does not replace memory, it
gives the identifiers to go read it and the state of what was in progress.

---

## 0. To do first, in order

1. **Reconnect the brain MCP** (`/mcp`, or new session). A fresh connection
   works — measured: `tools/list` → 9 tools.
2. **Reread the brain** rather than this file for detail: the entries below
   are more complete and carry their measurements.
3. **The brain session `8f2dc148-73ac-403c-bb6d-fb61aa6a83fc` remained OPEN.**
   `client_key = brain-v42-session-2026-08-10`, `started_focus_revision = 200`.
   It lives in the database, the cutover did not touch it. Closing or resuming it is
   an explicit command from the user — do nothing without it.

---

## 1. What was delivered, and is running

**Commit `8ed57969`** — `feat(provenance): transport identity`. Deployed and verified
in production on 2026-08-10 around 21:15.

The problem solved: the brain panel aggregated by actor, but the actor is the
basename of `cwd` (`X-Brain-Agent: ${PWD}`). The four Claude engines measured that
day run in the same directory and collapsed into **a single line**.

What changes: the server mints an `Mcp-Session-Id` (`stateless_http=False`), the
middleware reads it, it travels across the wire in a field **distinct** from `session`, and
the registry turns it into a `kind="transport"` line.

Checks passed on live production:

| Check | Result |
|---|---|
| The server mints a sid | 32 hex (`uuid4().hex`) |
| Made-up sid | **404** |
| `tools/list` without sid | **400** |
| Without bearer | **401** |
| Inactivity deadline | `session_idle_timeout seconds=900.0` |
| `Terminating session: None` | **1732 before → 0 after** |
| Dream token | 200, sid minted |
| Two connections from one actor | **2 lines** `transport-…`, 2 and 4 calls |

Full suite: 7416 passed, mypy Success, ruff clean.
`test_container_image_pins` fails — **pre-existing**, verified by stash.

### Rollback, just in case

```bash
mkdir -p ~/.config/systemd/user/brain-mcp-http.service.d
printf '[Service]\nEnvironment=MCP_HTTP_STATELESS=true\n' \
  > ~/.config/systemd/user/brain-mcp-http.service.d/stateless.conf
systemctl --user daemon-reload && systemctl --user restart brain-mcp-http
```

The commit stays; only the setting decides. No migration, no persistent write
is engaged by this piece of work.

---

## 2. Dream token rotation — done

60 profiles re-minted after a leak in transcript (5 files, all `0600`, all
under `wf_6370847b-70e`). Registry `0d8b13378932` → `738d1e8ff170`, zero token
recycled. Proven by four probes including **two negative** (old token → 401).

- Backup: `~/.config/brain-v42/mcp-token.env.bak-20260810-203428`
- Full runbook: brain `da84204f`
- `MCP_HTTP_TOKEN` (admin) **was not rotated** — see ticket `842d1bb4`.

---

## 3. What comes next — the OTLP join

**The design is settled and proven. It remains to be written.**

Chain established live on 2026-08-10: Codex's OTLP attribute `conversation.id`
**is** its `session_id`, identical to the rollout's `session_meta.session_id` and to the
file name. Measured on `019fecfb-2ecc-7a71-b26d-0aeefb5230b8`.

And 37 brain session `client_key` already carry the UUID of a real Codex session.
The join key therefore exists on **both sides**; nobody had linked them.

### What needs to be written

An **explicit parameter** `agent_conversation_id` on `brain_session_start`,
validated as a canonical UUID, persisted (migration 045), and reported to the sidecar so
that `_session_key()` computes the same agent-neutral key as the OTLP side.

### What must ABSOLUTELY NOT be done

Scraping the UUID out of `client_key` with a regex. The number that settles it: **114**
`client_key` carry a canonical UUID, **only 37** correspond to a Codex session. The
**other 78** are `red-mission`, `red-worker`, etc. UUIDs. Scraping would produce
78 false and silent joins.

### Two properties to respect in the design

- The relation is **N brain sessions → 1 conversation** (measured: 3 distinct
  `client_key` on `019fec5a-…`). Do not assume it is injective.
- A single `codex exec` emits **TWO** `conversation.id`, of which only one persists. There
  will remain unjoinable ghost OTLP lines — that's the regime, not a bug.

### What the join brings that transport doesn't

Transport **separates** the lines; it does not **name** them. A `transport-0ae…f9e5`
cannot be tied back to any pid, any task, any tab. The brain session, on the other hand,
carries a project, a `client_key` and a focus. The two are complementary.

---

## 4. Brain entries to reread

| Type | Subject | id |
|---|---|---|
| Decision | Join path: explicit parameter, not scraping | `4890a475` |
| Decision | Dream token rotation, admin kept | `ac75678e` |
| Learning | OTLP `conversation.id` == Codex session_id (proven chain) | `3747bb5e` |
| Learning | The `SessionStart` hook cannot declare the session (refuted) | `06332ea4` |
| Learning | `access_log` is a ~5 min queue, not a log | `1de79d26` |
| Learning | Metrics 60 s window: 2 callers out of 3 invisible | `5bd39821` |
| Learning | Two systemd units + cutover breaks live connections | `896d1e35` |
| Runbook | Rotating the dream registry without breaking the night (10 steps) | `da84204f` |

### Open tickets

- **`40dbfeb1` → red-monitor**: split the two sources into three tables, re-sort
  globally, rename `brain_calls` to "attempts", label the buckets.
  **The user launches this session himself** — give him the context, do not
  start without him.
- `842d1bb4` → brain-v42: `MCP_HTTP_TOKEN` in clear text in 13 transcripts, 4 projects.
- `d2a669c6` → brain-v42: `collector_db.py:137`, 60 s window against 1 h purge.
  **One constant**, and the panel goes from 1 agent to 3-4.

---

## 5. Pitfalls that cost time tonight

1. **The chain crosses TWO systemd units.** The wire format and the registry live
   in `brain-metrics.service`, not in `brain-mcp-http.service`. Restarting the MCP
   alone leaves the sidecar decoding with the old schema — and since its decoder ignores
   unknown keys **by design**, it silently drops the field. Symptom: one
   `unattributed calls=6` line instead of two `transport` lines at 2 and 4. Reflex:
   compare the age of **both** processes to that of the commit.

2. **Switching to stateful mode breaks already established connections**, and the client
   does not recover from it. A client connected beforehand receives `400 Missing session ID`
   then `-32602`. The study had measured a recovery on **404**; this path is a
   **400**, and it does not trigger the same recovery. Plan for the reconnection.

3. **A display pseudonym is not a join key.** I compared
   `codex-8f05…` (38 char., HMAC pseudonym) to `normalize_session`'s cap of 36 and
   drew a false conclusion from it. The real key is the HMAC of the UUID.

4. **The capability registry has FLAT keys** `"project:phase"`, and some projects
   themselves contain a `:` (`red-shrik:agent`). A naive `split(':')` renders a false
   "incomplete matrix" verdict. Use `rpartition(':')`.

5. **Substituting a symbol in a third-party module leaks between tests.** My injection
   of `session_idle_timeout` made five tests fail that passed in isolation. Hence
   `tests/unit/mcp/conftest.py` and installation idempotence.

---

## 6. What remains UNMEASURED

1. **A real end-to-end dream phase under stateful mode.** I proved that a
   dream bearer obtains a sid; not that a full phase goes all the way. It was already
   unknown #1 of the study, and it remains so. The user accepted the risk
   ("worst case we replay it manually").
2. The number of sessions per Claude Code invocation — 1 or 2? Two lenses
   contradict each other. Factor of 2 on the panel's line count.
3. The behavior of **interactive** engines over several hours: how many
   reconnections per day? Decides whether the panel shows 4 lines or 12.
4. The memory cost per session on the real production catalog (~142 kB estimated,
   measured on a bare FastMCP at 55 kB). To watch: prod was already growing by
   ~16 MB/h **before** this work, in stateless mode.
5. `len(_server_instances)` is **not** exposed in `/metrics`: the effectiveness of the 900 s
   TTL is therefore not observable in production. To ship before trusting
   the deadline.

---

## 7. Context of the night of 2026-08-11

First night with **ten projects** with the server scope armed, start **06:01**. The
registry was re-minted tonight, so phases will pick up the new tokens at
startup (they read the `EnvironmentFile`). REORG is in **DRY**.

The morning check is the **number of insights per project**, not the unit's
color — a phase can "succeed" on emptiness if the scope hides everything from it. Read the
report content, grouped by project.
