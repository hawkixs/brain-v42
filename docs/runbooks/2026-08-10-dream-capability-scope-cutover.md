# Arming the dream server scope — cutover, rotation, rollback

Step 8 of `docs/superpowers/specs/2026-08-08-dream-v2-design.md`. Cut over to
production on 2026-08-10.

## What this changes, and why it's the batch that matters

As long as `BRAIN_DREAM_CAPABILITY_ENFORCEMENT` is absent, a phase's principal is
`unscoped` and `on_call_tool` lets it through with no scope. A night
launched for `red` then reads the corpus of the 54 projects and can mutate it. With ten
projects, that's ten times the same global work — the spec puts a figure of 8 × 15 min of
work for the value of 15.

Armed, each phase receives a bearer specific to `(project, phase)`. The tools
read the scope via `get_dream_project_scope()` and filter their requests.
**Measured on 2026-08-10**: a SCAN scoped to `red` sees 751 learnings out of 2,760,
i.e. 27% of the corpus.

Second effect, which was not the goal: the prompts already FORBADE some
tools in prose ("Do NOT call brain_learn", `## Forbidden tools`). These
prohibitions are now backed by a server-side refusal.
`tests/unit/test_dream_prompts_match_phase_allowlists.py` keeps the two
in agreement.

## The failure mode, which is green

On 2026-07-03, a missing bearer made every phase run into a 401 — zero
brain tools — and the night reported "6/6 OK". The `token.conf` drop-in exists
because of that. An incomplete registry, malformed, or present on only one side produces
exactly the same night. **Every step below ends with a
positive proof, never with the absence of an error.**

## Cutover

The registry requires a **complete matrix**: the six phases for each project,
otherwise `parse_dream_capability_registry` raises at MCP server startup and
production does not come back up. With ten projects, sixty profiles.

```bash
# 1. Mint. The tool validates its output with the SERVER's parser before
#    writing, so it cannot produce a registry refused at startup.
#    The admin bearer comes in via the environment, never via an argument.
#    --from-drop-in reuses exactly the pool of the live unit.
MCP_HTTP_TOKEN="$(sed -n 's/^MCP_HTTP_TOKEN=//p' ~/.config/brain-v42/mcp-token.env)" \
  uv run python -m scripts.mint_dream_capability_registry \
    --output ~/.config/brain-v42/dream-registry.staged --from-drop-in

# 2. Back up the private file BEFORE touching it. The rollback is this file.
cp -p ~/.config/brain-v42/mcp-token.env \
      ~/.config/brain-v42/mcp-token.env.bak-$(date -u +%Y%m%d-%H%M%S)
```

Then compose the new private file with a local editor — never via
`echo`, whose argument remains in the shell history. It carries exactly
three assignments, and the preflight rejects any other key:

```
MCP_HTTP_TOKEN=<unchanged>
MCP_HTTP_DREAM_TOKENS=<the line minted at step 1, without its key prefix>
BRAIN_DREAM_CAPABILITY_ENFORCEMENT=true
```

```bash
# 3. Validate BEFORE restarting, under systemd conditions — the preflight
#    compares the file to the EFFECTIVE environment, so it must be loaded.
env $(cat ~/.config/brain-v42/mcp-token.env | xargs -d '\n') \
  .venv/bin/python scripts/check_mcp_http_port.py \
    --shared .env --expected 8765 --expected-host 127.0.0.1 \
    --token-file ~/.config/brain-v42/mcp-token.env \
    --require-effective-runtime-settings

# 4. Restart the single MCP HTTP service.
systemctl --user restart brain-mcp-http.service
curl -fsS -m 3 http://127.0.0.1:8765/health
```

## Proofs to require, in this order

1. **The admin bearer still passes.** A `brain_*` call from an existing
   client must respond. An argument validation error is sufficient
   proof — it comes from AFTER authentication. A 401 is not.
2. **A scoped bearer passes.** `POST /mcp` with the token of a profile must return
   200. That is the proof of non-blindness, and it's the one missing on 2026-07-03.
3. **A made-up token is refused.** 401. Without this third probe, the first two
   don't prove that a guard exists.
4. **The matrix covers the pool, and nothing more.**
   `codex_runner --preflight-capabilities --project-key X` for each project of the
   pool, then for a project OUTSIDE the pool — the second must FAIL. Without this
   reverse guard, the matrix proves nothing.

`dream.sh` replays the preflight for each project of the pool before any mutation,
so a hole in the matrix stops the night before it starts.

## Widening the pool

The registry is minted for a given pool. **Adding a project to the drop-in without
re-minting the registry makes that project's preflight fail, hence the entire
night.** This is fail-closed and intentional. The sequence is: re-mint for the
new pool, replace the private file, restart MCP HTTP, re-verify.

## Rotation

`accepted` exists for overlap: place the old token there while
clients pick up the new one, then remove it. `verify_token` honors
`active` **and** every `accepted`. The initial minting leaves `accepted` empty.

## Rollback

A single gesture, and it is complete:

```bash
cp -p ~/.config/brain-v42/mcp-token.env.bak-<timestamp> \
      ~/.config/brain-v42/mcp-token.env
systemctl --user restart brain-mcp-http.service
```

Without `BRAIN_DREAM_CAPABILITY_ENFORCEMENT`, `_configure_http_security` puts back the
historical `BearerTokenGuard` on `MCP_HTTP_TOKEN`: existing clients see
no difference. The dream side falls back to `unscoped` via the same
file — both units read it as `EnvironmentFile`, so the cutover and the
rollback are one file, not two to keep in sync.
