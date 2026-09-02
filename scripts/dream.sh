#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DREAM_DIR="$SCRIPT_DIR/dream"
LOG_DIR="$SCRIPT_DIR/../logs/dream"
# Pinned MCP config for the explicit Claude rollback path — see run_phase.
# Codex ignores ambient user configuration and declares only brain-v42 through
# scripts.dream.codex_runner.
MCP_CONFIG="$SCRIPT_DIR/../.mcp.json"

# Arg parsing — flag-style args come first, then the positional project_key.
# Env-var forms (DRY_RUN=true, BRAIN_DREAM_PROMOTE_ENABLED=false) also work.
DRY_RUN="${DRY_RUN:-false}"
# Agent provider for SCAN/CLEAN/CONNECT/SYNTH/PROMOTE/REORG. Codex is the
# subscription-backed default; Claude remains an explicit operator rollback.
# There is deliberately no automatic fallback after a phase starts because a
# WET MCP call may already have committed a mutation.
BRAIN_DREAM_AGENT_PROVIDER="${BRAIN_DREAM_AGENT_PROVIDER:-codex}"
# THE CHAIN. An ORDERED list of providers, comma-separated — the same
# transport constraint as the pool, and for the same reason: systemd's
# `Environment=` splits on unquoted whitespace.
#
# By default it equals the single provider, so a night that does not configure
# it behaves EXACTLY as before: one provider, no switchover. That is
# deliberate — the chain must be an option you arm, not a change of behaviour
# you are subjected to.
BRAIN_DREAM_AGENT_PROVIDERS="${BRAIN_DREAM_AGENT_PROVIDERS:-$BRAIN_DREAM_AGENT_PROVIDER}"
# The code a runner returns when it has failed AND can PROVE that no Brain
# tool call succeeded. It lives in scripts/dream/_agent_capability.py;
# tests/unit/test_dream_provider_chain.py keeps the two in agreement.
#
# It alone advances the chain. Widening to `!= 0` would make the night able to
# replay a phase that had already written — doubling its writes, without a word.
PROVIDER_FALLBACK_EXIT_CODE=3
BRAIN_DREAM_CAPABILITY_ENFORCEMENT="${BRAIN_DREAM_CAPABILITY_ENFORCEMENT-false}"
BRAIN_DREAM_CODEX_FAST_MODEL="${BRAIN_DREAM_CODEX_FAST_MODEL:-gpt-5.6-terra}"
BRAIN_DREAM_CODEX_DEEP_MODEL="${BRAIN_DREAM_CODEX_DEEP_MODEL:-gpt-5.6-sol}"
BRAIN_DREAM_CODEX_FAST_REASONING="${BRAIN_DREAM_CODEX_FAST_REASONING:-medium}"
BRAIN_DREAM_CODEX_DEEP_REASONING="${BRAIN_DREAM_CODEX_DEEP_REASONING:-high}"
BRAIN_DREAM_CODEX_BIN="${BRAIN_DREAM_CODEX_BIN:-codex}"
BRAIN_DREAM_CLAUDE_BIN="${BRAIN_DREAM_CLAUDE_BIN:-claude}"
BRAIN_DREAM_AGY_BIN="${BRAIN_DREAM_AGY_BIN:-agy}"
# Models of the Google link. GEMINI, deliberately: agy also exposes
# claude-sonnet-4-6 and claude-opus-4-6-thinking, and taking those would defeat
# the point of the link — if Anthropic falls, those models fall with it, and the
# chain would have two correlated links disguised as three.
BRAIN_DREAM_AGY_FAST_MODEL="${BRAIN_DREAM_AGY_FAST_MODEL:-gemini-3.6-flash-medium}"
BRAIN_DREAM_AGY_DEEP_MODEL="${BRAIN_DREAM_AGY_DEEP_MODEL:-gemini-3.1-pro-high}"
# Ship the PROMOTE killswitch CLOSED (false) by default. Flip to true once
# §8 step 5 of the spec (first live rollout) has been cleared.
BRAIN_DREAM_PROMOTE_ENABLED="${BRAIN_DREAM_PROMOTE_ENABLED:-false}"
# REORG killswitch — independent of PROMOTE. Kept CLOSED until the two
# tool-shape blockers surfaced by Night 3 REORG (2026-04-23) are fixed:
# brain_list token ceiling + brain_get missing access_count/freshness_status.
# Both shipped in commit 0f521cd (2026-04-28). Re-arming proceeds via the
# REORG-only DRY_RUN sub-flag below before flipping WET.
BRAIN_DREAM_REORG_ENABLED="${BRAIN_DREAM_REORG_ENABLED:-false}"
# REORG-only dry-run override. When true AND BRAIN_DREAM_REORG_ENABLED=true,
# the REORG phase renders its prompt with Dry run: true while every other
# phase (notably PROMOTE) keeps the global DRY_RUN value. Lets REORG soak
# safely against live data without rolling back PROMOTE's WET status —
# applies the killswitch-as-cadence-decoupling pattern (learning 9a677c1a)
# at the dry-run dimension.
BRAIN_DREAM_REORG_DRY_RUN="${BRAIN_DREAM_REORG_DRY_RUN:-false}"
# EXTRACT killswitch — ticket knowledge extraction (proposer-only, spec
# 2026-07-04). Ship CLOSED; once enabled it starts in DRY (propose-only,
# human review through ticket_extraction_proposals) — the same soak
# trajectory as REORG before any WET flip.
BRAIN_DREAM_EXTRACT_ENABLED="${BRAIN_DREAM_EXTRACT_ENABLED:-false}"
BRAIN_DREAM_EXTRACT_DRY_RUN="${BRAIN_DREAM_EXTRACT_DRY_RUN:-true}"
# ROADMAP killswitch — nightly roadmap curation (spec 2026-07-04). Ship
# CLOSED; DRY by default. Aggressive regime since the evening of 2026-07-04
# (Armand's decision): in WET the CLI applies ALL FOUR ops (merge/rename
# included, WET_APPLYABLE_OPS = VALID_OPS) and the prompt consolidates granular
# features into broad subjects — Claude validates the applies at the morning check.
BRAIN_DREAM_ROADMAP_ENABLED="${BRAIN_DREAM_ROADMAP_ENABLED:-false}"
BRAIN_DREAM_ROADMAP_DRY_RUN="${BRAIN_DREAM_ROADMAP_DRY_RUN:-true}"
# SWEEP killswitch — draining the ghost sessions (spec 2026-08-07). Shipped
# CLOSED and DRY. A deterministic phase, with no model and no network: the
# threshold lives in brain_v42.models.brain_session.AUTO_STALE_AFTER, never here.
BRAIN_DREAM_SWEEP_ENABLED="${BRAIN_DREAM_SWEEP_ENABLED:-false}"
BRAIN_DREAM_SWEEP_DRY_RUN="${BRAIN_DREAM_SWEEP_DRY_RUN:-true}"
# The retry allocation for the WHOLE NIGHT, all projects together (§10). Two,
# because the most expensive phase (synth) is worth 15 min: 2 × 15 = 30 min of
# maximum extension, against 43 min PER PROJECT if retries stayed per phase.
BRAIN_DREAM_RETRY_BUDGET="${BRAIN_DREAM_RETRY_BUDGET:-2}"
RETRY_BUDGET_LEFT="$BRAIN_DREAM_RETRY_BUDGET"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --) shift; break ;;
    -*)
      echo "Unknown flag: $1" >&2; exit 2 ;;
    *) break ;;
  esac
done

# No default, deliberately. A default here would satisfy the parsers'
# required --project-key with `brain-v42` and label another project's whole
# night, one layer above where that guard was placed. The failure mode is
# silent by construction: the rows are valid, nothing in the corpus says they
# lie, and there is no backfill. The systemd unit passes the key explicitly.
if [[ $# -eq 0 || -z "${1:-}" ]]; then
  echo "Usage: dream.sh [--dry-run] <project_key>[,<project_key>...]" >&2
  echo "Refusing to guess the project: a mislabelled night cannot be measured after the fact." >&2
  exit 2
fi

# --- The night's project pool ----------------------------------------------
#
# The positional argument stays REQUIRED. The guard above is not diluted just
# because a pool exists: refusing to guess is still worth it.
#
# BRAIN_DREAM_PROJECT_POOL, when set, REPLACES the positional. The direction of
# precedence is not a matter of taste. `ExecStart=` lives in the versioned
# template, which deploy/systemd/install.sh REGENERATES; the drop-in, for its
# part, survives the regeneration — that is the whole point of killswitches.conf
# and of the 2026-06-30 incident it documents. If the positional won, widening
# the pool in the drop-in would change NOTHING: the night would stay on one
# project, green and mute. That is the failure mode spec §3.1 refuses by name.
#
# A COMMA separator, and it is a transport constraint, not an aesthetic one:
# systemd's `Environment=` splits on UNQUOTED whitespace and treats each piece
# as a separate assignment. `Environment=BRAIN_DREAM_PROJECT_POOL=a b` sets the
# variable to `a` and throws `b` away, without an error at startup. An INTERNAL
# blank is therefore the signature of a broken transport, and it exits 2 below
# rather than manufacturing a key that canonicalize_project_key will reject deep
# inside a best-effort function that swallows its exception.
declare -a PROJECT_POOL=()
POOL_SOURCE="positional argument"
_pool_raw="$1"
if [[ -n "${BRAIN_DREAM_PROJECT_POOL:-}" ]]; then
  _pool_raw="$BRAIN_DREAM_PROJECT_POOL"
  POOL_SOURCE="BRAIN_DREAM_PROJECT_POOL"
fi

IFS=',' read -r -a _pool_parts <<< "$_pool_raw"
for _entry in "${_pool_parts[@]}"; do
  # Trim edge whitespace only: `a, b` is a natural, unambiguous human
  # spelling.
  _entry="${_entry#"${_entry%%[![:space:]]*}"}"
  _entry="${_entry%"${_entry##*[![:space:]]}"}"
  if [[ -z "$_entry" ]]; then
    echo "Empty project key in $POOL_SOURCE: '$_pool_raw'" >&2
    exit 2
  fi
  if [[ "$_entry" == *[[:space:]]* ]]; then
    echo "Whitespace inside a project key from $POOL_SOURCE: '$_entry'" >&2
    echo "Pool entries are comma-separated: BRAIN_DREAM_PROJECT_POOL=brain-v42,red,red-lab" >&2
    exit 2
  fi
  if [[ "$_entry" == */* ]]; then
    # A project key goes into a log file name (§3.2). A slash there would dig
    # a directory, or make the open fail.
    echo "Slash inside a project key from $POOL_SOURCE: '$_entry'" >&2
    exit 2
  fi
  case "$_entry" in
    brain|brain_v42) _entry="brain-v42" ;;
  esac
  for _seen in ${PROJECT_POOL[@]+"${PROJECT_POOL[@]}"}; do
    if [[ "$_seen" == "$_entry" ]]; then
      # Serving the same project twice in one night is exactly the waste §7
      # measures for the global phases. A repetition is a typo, not an
      # intention.
      echo "Duplicate project key in $POOL_SOURCE: '$_entry'" >&2
      exit 2
    fi
  done
  PROJECT_POOL+=("$_entry")
done

# §10: the retry allocation is a NIGHT-wide resource, so the project at the
# head of the pool is better served than the one at the tail. Without rotation
# it is always the same one sacrificed. Same idiom as roadmap_curate.rotate_keys,
# in service since 2026-07-04. `10#` forces base 10: `date +%j` returns 001-366
# and bash would read 008 as an invalid octal.
if (( ${#PROJECT_POOL[@]} > 1 )); then
  _rotation=$(( 10#$(date +%j) % ${#PROJECT_POOL[@]} ))
  PROJECT_POOL=("${PROJECT_POOL[@]:$_rotation}" "${PROJECT_POOL[@]:0:$_rotation}")
fi

# The project served at a given instant. Reassigned on every iteration of the
# project loop: run_phase, the three validators and _render_prompt all read it
# comme un global.
PROJECT_KEY="${PROJECT_POOL[0]}"
TIMESTAMP=$(date +%Y-%m-%d)

case "$BRAIN_DREAM_AGENT_PROVIDER" in
  codex|claude|agy) ;;
  *)
    echo "Unsupported BRAIN_DREAM_AGENT_PROVIDER: $BRAIN_DREAM_AGENT_PROVIDER" >&2
    exit 2
    ;;
esac

# --- The provider chain -----------------------------------------------------
#
# Validated IN FULL here, before the first phase. An unknown link in the middle
# of the chain must not be discovered at 4 a.m., on the one night the first link
# dies: that would be precisely the day the standby matters, and it would not
# be there.
declare -a PROVIDER_CHAIN=()
IFS=',' read -r -a _provider_parts <<< "$BRAIN_DREAM_AGENT_PROVIDERS"
for _provider in "${_provider_parts[@]}"; do
  _provider="${_provider#"${_provider%%[![:space:]]*}"}"
  _provider="${_provider%"${_provider##*[![:space:]]}"}"
  if [[ -z "$_provider" ]]; then
    echo "Empty entry in BRAIN_DREAM_AGENT_PROVIDERS: $BRAIN_DREAM_AGENT_PROVIDERS" >&2
    exit 2
  fi
  case "$_provider" in
    codex|claude|agy) ;;
    *)
      echo "Unsupported provider in BRAIN_DREAM_AGENT_PROVIDERS: $_provider" >&2
      exit 2
      ;;
  esac
  for _seen in "${PROVIDER_CHAIN[@]}"; do
    if [[ "$_seen" == "$_provider" ]]; then
      echo "Duplicate provider in BRAIN_DREAM_AGENT_PROVIDERS: $_provider" >&2
      exit 2
    fi
  done
  PROVIDER_CHAIN+=("$_provider")
done
if (( ${#PROVIDER_CHAIN[@]} == 0 )); then
  echo "BRAIN_DREAM_AGENT_PROVIDERS is empty" >&2
  exit 2
fi
# The first link is the nominal provider: the preflights, the logs and the
# per-tier model choice all read it as a global.
BRAIN_DREAM_AGENT_PROVIDER="${PROVIDER_CHAIN[0]}"

case "$BRAIN_DREAM_CAPABILITY_ENFORCEMENT" in
  true|false) ;;
  *)
    echo "Invalid BRAIN_DREAM_CAPABILITY_ENFORCEMENT value" >&2
    exit 2
    ;;
esac
# The historical refusal of `claude` under enforcement was LIFTED on
# 2026-08-11, and only because its cause disappeared. That rail went through the
# repository's .mcp.json, whose Authorization interpolates ${MCP_HTTP_TOKEN} —
# the ADMIN token — and through the `mcp__brain-v42__*` wildcard. Under
# enforcement, that was exactly the combination the firewall exists to forbid:
# six unscoped phases, green logs, and nothing to say so. Refusing was the only
# fail-closed available while the rail could not carry a per-phase bearer.
#
# scripts/dream/claude_runner.py now can: it renders a per-phase MCP
# configuration (dedicated agent header, exact allowlist, no wildcard) and gives
# the child process the (project, phase) token under the name that configuration
# references. So the guard has nothing left to protect.
#
# Do not remove it without that counterpart: deleting it alone would hand the
# admin token back to the rail, in silence and with the same green night.

mkdir -p "$LOG_DIR"

# Phase definitions: name:model_tier:timeout_minutes:legacy_claude_max_turns
# max_turns raised after 2026-04-14 scan fail + 2026-04-10 synth fail.
# synth bumped 10→15 on 2026-05-03 after 3 timeouts in 16 nights post-04-24
# (brain_graph_* tool surface expansion + corpus mega-cluster +27% growth);
# pre-04-24 SYNTH ran 2-5 min, post-04-24 it ranges 4-10+ min, leaving zero
# headroom against the original 10m budget. Pinned by
# tests/unit/test_dream_sh_phase_timeouts.py.
PHASES=(
  "scan:fast:5:30"
  "clean:fast:5:25"
  "connect:fast:8:40"
  "synth:deep:15:50"
  "promote:deep:10:50"
  "reorg:deep:10:50"
)

# Phase dependencies: which previous phase logs to inject
declare -A PHASE_DEPS=(
  [scan]=""
  [clean]="scan"
  [connect]="clean"
  [synth]="connect"
  [promote]="synth"
  [reorg]="scan synth"
)

# The three GLOBAL phases, named for the `planned_phases` computation alone.
# The three blocks stay hand-written OUTSIDE the loop (pinned by
# tests/unit/test_dream_sh_global_phases_outside_loop.py): this array does not
# drive them, it counts them.
DREAM_GLOBAL_PHASES=(extract roadmap sweep)

# OTEL env vars for Claude Code telemetry
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_LOGS_EXPORTER=console
export OTEL_METRICS_EXPORTER=console

# Force the headless `claude -p` subagents to BLOCK on MCP connection before
# the first turn (default is non-blocking). Without this, claude snapshots the
# turn's tool list ~450ms after init while the stdio brain-v42 server needs
# ~1.2s to finish importing + register tools — so brain_* is intermittently
# absent (regression 27430ae1: scan/reorg win the race, promote loses).
# Counter-intuitive flag semantics (decompiled from claude 2.1.186): setting
# NONBLOCKING=false flips connect to the BLOCKING branch, which awaits all MCP
# servers up to MCP_CONNECT_TIMEOUT_MS. --strict-mcp-config alone only removed
# the fast HTTP gitlab competitor; it did NOT make brain-v42 win the race.
export MCP_CONNECTION_NONBLOCKING=false
export MCP_CONNECT_TIMEOUT_MS=10000

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_DIR/$TIMESTAMP.log"; }

#
# Exit codes from run_phase:
#   0 → phase succeeded (DONE)
#   1 → phase hard-failed (non-zero exit, not a timeout)
#   2 → phase timed out (exit 124 from `timeout`)
run_phase() {
  local name="$1" model_tier="$2" timeout="$3" max_turns="$4"
  local prompt_file="$DREAM_DIR/phase_${name}.md"
  # §3.2: these paths carry the project. codex_runner opens `events` and
  # `stderr` in "w" and calls `report_log.write_text("")` — TRUNCATION, not
  # append. Without a project component, only the LAST project's logs would
  # survive till morning, and §3.3's dependency injection would re-read the
  # previous project's report.
  local raw_log="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.raw.log"
  local report_log="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.log"
  local otel_log="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.otel.log"
  local events_log="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.events.jsonl"
  local stderr_log="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.stderr.log"

  local model reasoning_effort=""
  case "$BRAIN_DREAM_AGENT_PROVIDER:$model_tier" in
    codex:fast)
      model="$BRAIN_DREAM_CODEX_FAST_MODEL"
      reasoning_effort="$BRAIN_DREAM_CODEX_FAST_REASONING"
      ;;
    codex:deep)
      model="$BRAIN_DREAM_CODEX_DEEP_MODEL"
      reasoning_effort="$BRAIN_DREAM_CODEX_DEEP_REASONING"
      ;;
    claude:fast) model="sonnet" ;;
    claude:deep) model="opus" ;;
    agy:fast) model="$BRAIN_DREAM_AGY_FAST_MODEL" ;;
    agy:deep) model="$BRAIN_DREAM_AGY_DEEP_MODEL" ;;
    *)
      log "FAIL  $name — unsupported provider/model tier: $BRAIN_DREAM_AGENT_PROVIDER/$model_tier"
      return 1
      ;;
  esac

  if [[ ! -f "$prompt_file" ]]; then
    log "SKIP $name — prompt file missing: $prompt_file"
    return 0
  fi

  local prompt
  # Per-phase DRY_RUN override: REORG soaks dry while PROMOTE runs WET.
  # The override only fires when REORG is enabled — disabled phases never
  # reach the renderer (they `continue` above).
  local effective_dry_run="$DRY_RUN"
  if [[ "$name" == "reorg" && "$BRAIN_DREAM_REORG_DRY_RUN" == "true" ]]; then
    effective_dry_run="true"
  fi
  # Delegate to scripts/dream/_render_prompt.py — sed used to break here
  # when the candidate pool JSON contained `|`, `&`, or `\` (all have
  # special meaning in sed's s/// replacement). On 2026-04-19 a `|` in a
  # topic string produced an empty prompt and the validator flagged a
  # missing PROMOTE REPORT.
  prompt=$(python3 -m scripts.dream._render_prompt \
    "$prompt_file" "$PROJECT_KEY" "$TIMESTAMP" "$effective_dry_run" \
    "${PROMOTE_CANDIDATE_POOL_JSON:-[]}" \
    "${PROMOTE_RECENT_PROMOTIONS_JSON:-[]}")

  # Inject dependency phase outputs into the prompt.
  # PREPENDED (not appended) so the phase prompt's own output-format
  # instructions are the LAST thing the model sees. When deps were
  # appended, the prose style of the SYNTH log (markdown candidate
  # reports) primed PROMOTE to emit prose + empty markers — the
  # "last example sticks" failure mode caught on 2026-04-20 runs.
  local deps="${PHASE_DEPS[$name]:-}"
  if [[ -n "$deps" ]]; then
    local dep_section=""
    for dep in $deps; do
      local dep_log="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${dep}.log"
      if [[ -f "$dep_log" && -s "$dep_log" ]]; then
        dep_section+="
### ${dep^^} phase output
$(cat "$dep_log")
"
      fi
    done
    if [[ -n "$dep_section" ]]; then
      prompt="## Previous Phase Reports (reference context — do not mimic style)
The orchestrator has injected the output from dependency phases below.
$dep_section

---

$prompt"
    fi
  fi

  if [[ "$BRAIN_DREAM_AGENT_PROVIDER" == "agy" ]]; then
    log "START $name (provider=agy, model=$model, timeout=${timeout}m)"
  elif [[ "$BRAIN_DREAM_AGENT_PROVIDER" == "codex" ]]; then
    log "START $name (provider=codex, model=$model, reasoning=$reasoning_effort, timeout=${timeout}m)"
  else
    log "START $name (provider=claude, model=$model, timeout=${timeout}m, max_turns=$max_turns)"
  fi

  local phase_start=$SECONDS
  local status="done"
  local phase_rc=0

  # The prompt always travels through stdin to avoid ARG_MAX when dependency
  # reports are large. Codex writes final report, JSONL events, and stderr to
  # separate files. The Claude rollback keeps its historical mixed OTEL path.
  local code=0
  if [[ "$BRAIN_DREAM_AGENT_PROVIDER" == "agy" ]]; then
    # agy takes no configuration on the command line: its runner composes an
    # ephemeral HOME for it, carrying the scoped bearer AND the tool guard. See
    # scripts/dream/agy_runner.py — the only rail whose bearer touches a file,
    # confined to a tmpfs and destroyed with the HOME.
    if printf '%s' "$prompt" | uv run python -m scripts.dream.agy_runner \
      --phase "$name" \
      --project-key "$PROJECT_KEY" \
      --model "$model" \
      --timeout-seconds "$(( timeout * 60 ))" \
      --events-log "$events_log" \
      --report-log "$report_log" \
      --stderr-log "$stderr_log" \
      --agy-executable "$BRAIN_DREAM_AGY_BIN"; then
      code=0
    else
      code=$?
    fi
  elif [[ "$BRAIN_DREAM_AGENT_PROVIDER" == "codex" ]]; then
    if printf '%s' "$prompt" | uv run python -m scripts.dream.codex_runner \
      --phase "$name" \
      --project-key "$PROJECT_KEY" \
      --model "$model" \
      --reasoning-effort "$reasoning_effort" \
      --timeout-seconds "$(( timeout * 60 ))" \
      --report-log "$report_log" \
      --events-log "$events_log" \
      --stderr-log "$stderr_log" \
      --codex-executable "$BRAIN_DREAM_CODEX_BIN"; then
      code=0
    else
      code=$?
    fi
  else
    # The claude rail has gone through its isolated runner since 2026-08-11.
    # It replaces the `mcp__brain-v42__*` wildcard with the phase's exact
    # allowlist and substitutes the (project, phase) bearer for the admin token.
    # The mixed stream still lands in $raw_log: otel_split reads it right after.
    if printf '%s' "$prompt" | uv run python -m scripts.dream.claude_runner \
      --phase "$name" \
      --project-key "$PROJECT_KEY" \
      --model "$model" \
      --max-turns "$max_turns" \
      --timeout-seconds "$(( timeout * 60 ))" \
      --raw-log "$raw_log" \
      --claude-executable "$BRAIN_DREAM_CLAUDE_BIN"; then
      code=0
    else
      code=$?
    fi
  fi

  if [[ $code -eq 0 ]]; then
    log "DONE  $name"
  else
    if [[ $code -eq 124 ]]; then
      status="timeout"
      phase_rc=2
      log "TIMEOUT $name (>${timeout}m)"
    elif [[ $code -eq $PROVIDER_FALLBACK_EXIT_CODE ]]; then
      # A failure PROVEN to have written nothing. It is a failure for the
      # metrics as for the unit — `status` stays `fail` — but the code must
      # REACH run_phase_chain unchanged, the only place that knows whether a
      # next link exists. Squashing it to 1 here, as the original version did,
      # made the chain inert: it never saw its
      # condition de bascule.
      status="fail"
      phase_rc=$PROVIDER_FALLBACK_EXIT_CODE
      log "FAIL  $name (exit=$code — aucun appel d'outil Brain abouti)"
    else
      status="fail"
      phase_rc=1
      log "FAIL  $name (exit=$code)"
    fi
  fi

  local duration=$(( SECONDS - phase_start ))

  local err_log=""
  if [[ "$BRAIN_DREAM_AGENT_PROVIDER" == "claude" ]]; then
    # Preserve the failed mixed stream before otel_split removes it.
    if [[ "$status" != "done" && -f "$raw_log" ]]; then
      err_log="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.err.log"
      cp "$raw_log" "$err_log"
    fi

    if uv run python -m brain_v42.metrics.otel_split \
        "$raw_log" --report "$report_log" --otel "$otel_log" \
        >> "$LOG_DIR/$TIMESTAMP.log" 2>&1; then
      rm -f "$raw_log"
    else
      log "WARN  otel_split failed for $name — leaving raw log in place"
      [[ -f "$raw_log" ]] && cp "$raw_log" "$report_log"
      : > "$otel_log"
    fi
  else
    # The runner already separated the streams. Always leave a readable report
    # path for dependency injection and validators, including failed phases.
    [[ -f "$report_log" ]] || : > "$report_log"
    err_log="$stderr_log"
  fi

  # --project-key lands in the SHARED array, before the codex/claude fork, so
  # both rails receive it from one edit. Teaching it to a single parser would
  # hand the other an unknown argument: argparse exits 2, pipefail propagates,
  # and the WARN below swallows it — the night silently loses its six
  # per-project rows while the test suite stays green.
  local parser_args=(--phase "$name" --model "$model" --date "$TIMESTAMP"
                     --status "$status" --duration "$duration"
                     --project-key "$PROJECT_KEY")
  parser_args+=(--phase-dry-run "$effective_dry_run")
  local scan_log="$err_log"
  if [[ -z "$scan_log" && -f "$report_log" ]]; then
    scan_log="$report_log"
  fi
  if [[ -n "$scan_log" ]]; then
    parser_args+=(--raw-log "$scan_log")
  fi
  if [[ "$BRAIN_DREAM_AGENT_PROVIDER" == "agy" ]]; then
    if uv run python -m brain_v42.metrics.agy_dream_parser \
      "${parser_args[@]}" --report-log "$report_log" "$events_log" \
      2>&1 | tee -a "$LOG_DIR/$TIMESTAMP.log"; then
      :
    else
      log "WARN  agy_dream_parser failed for $name (non-fatal)"
    fi
  elif [[ "$BRAIN_DREAM_AGENT_PROVIDER" == "codex" ]]; then
    if uv run python -m brain_v42.metrics.codex_dream_parser \
      "${parser_args[@]}" --report-log "$report_log" "$events_log" \
      2>&1 | tee -a "$LOG_DIR/$TIMESTAMP.log"; then
      :
    else
      log "WARN  codex_dream_parser failed for $name (non-fatal)"
    fi
  else
    if uv run python -m brain_v42.metrics.dream_parser \
      "${parser_args[@]}" "$otel_log" 2>&1 | tee -a "$LOG_DIR/$TIMESTAMP.log"; then
      :
    else
      log "WARN  dream_parser failed for $name (non-fatal)"
    fi
  fi

  return "$phase_rc"
}

#
# Runs one phase across the provider CHAIN.
#
# It advances to the next link on the single code PROVIDER_FALLBACK_EXIT_CODE,
# which means "failed, and I can prove no Brain tool call succeeded". An
# ordinary failure (1) and a timeout (2) stop where they fell: neither proves
# nothing was written, and replaying a phase that mutated would make it write
# twice.
#
# The return codes are identical to run_phase (0 / 1 / 2), so that every caller
# — the night's retry, the validators, the counters — stays unchanged. The chain
# is a layer ABOVE the existing contract, not a change to that contract.
run_phase_chain() {
  local name="$1" model_tier="$2" timeout="$3" max_turns="$4"
  local nominal_provider="$BRAIN_DREAM_AGENT_PROVIDER"
  local rc=0
  local index=0

  for provider in "${PROVIDER_CHAIN[@]}"; do
    index=$(( index + 1 ))
    BRAIN_DREAM_AGENT_PROVIDER="$provider"
    # Above all NO `set +e` / `set -e` here: a `set -e` placed inside a
    # function survives its `return`, and the errexit thus restored would exit
    # the script on the final `return $rc` — the night would stop at the first
    # failed phase, before even its summary. `|| rc=$?` captures the code without
    # toucher au mode du shell.
    rc=0
    run_phase "$name" "$model_tier" "$timeout" "$max_turns" || rc=$?

    if (( rc != PROVIDER_FALLBACK_EXIT_CODE )); then
      break
    fi

    if (( index < ${#PROVIDER_CHAIN[@]} )); then
      # Name the abandoned link AND the proof that authorises it. A mute
      # switchover would make "codex worked" and "codex died, claude saved the
      # night" indistinguishable — yet that is exactly what has to be visible in
      # the morning to know whether a subscription is still alive.
      log "FALLBACK $PROJECT_KEY/$name — $provider a échoué sans aucun appel d'outil Brain abouti, bascule vers ${PROVIDER_CHAIN[$index]}"
      # Aggregated for the closing summary. Recorded at the SWITCHOVER, not at
      # the next link's success: what we want to measure is the primary's death,
      # whether or not a standby catches it. If nobody catches it, the phase
      # joins FAILED_PHASES below and will be counted on both grounds.
      FALLBACK_PHASES+=("$PROJECT_KEY/$name")
    else
      # Last link: nobody left behind it. The phase becomes an ordinary
      # failure again, so the unit turns red as before.
      log "FALLBACK-END $PROJECT_KEY/$name — $provider était le dernier maillon de la chaîne"
      rc=1
    fi
  done

  BRAIN_DREAM_AGENT_PROVIDER="$nominal_provider"
  return "$rc"
}

# --- Main ---

# Advisory lock: prevent concurrent dream cycles (cron overlap, manual
# re-trigger during an active run). File descriptor 9 is held for the
# full lifetime of this process; flock -n returns immediately if another
# process already owns it.
LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/brain-v42-dream.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "dream cycle already running (lock=$LOCK_FILE), skipping"
  exit 0
fi

# --- The night's manifest ---------------------------------------------------
#
# MIND THE ORDER: this block TRUNCATES, so it lives BEHIND the lock, never in
# front. Placed higher, a night's second invocation — a cron overlap, a manual
# re-trigger: the very case the lock exists to absorb — would empty the LIVE
# night's manifest then exit 0. The healthy night would end `consistent=false`,
# escalate to rc 2 and write a lying `coverage` row, and its pairs from before
# the truncation would be reclassified as `extra`, which never escalates: a real
# hole among them would become invisible. Pinned by
# test_dream_sh_run_manifest.py.
#
# What the night DECLARES — expectations, skips and their reason — written AT
# THE SITE OF EVERY DECISION, read back by scripts.dream.post_run_alert.
#
# Ticket 0a9c067e: the coverage comparator exists and it fired three nights
# running, but its expectation comes from the systemd drop-in, which has keys
# only for promote and reorg. The night of 2026-08-16 therefore announced 20
# missing phases when 60 were missing. Widening it from the drop-in would be a
# no-op; the night is the only witness that knows what it actually attempted.
#
# INCREMENTAL, never flushed at the end of the night: a night killed by
# TimeoutStartSec, an OOM or an unguarded `set -e` would reach no final flush,
# and the morning replay would fall back on the drop-in's expectation — that is,
# on the very hole this file closes. The absence of the closing block therefore
# becomes the interruption marker, and forbids any green verdict.
#
# WHOLLY BEST-EFFORT: `set -euo pipefail` has been active since line 2, and a
# read-only `logs/` must never kill a night. Telemetry that fails does not bring
# down the phase it observes.
#
# No escaping is needed: a project key carrying a blank or a slash is already
# refused above, and the phase names as well as the skip reasons are literals
# from a closed set.
MANIFEST_FILE="$LOG_DIR/${TIMESTAMP}_manifest.tsv"
manifest_put() {
  printf '%s\t%s\t%s\t%s\n' "$1" "${2-}" "${3-}" "${4-}" >> "$MANIFEST_FILE" 2>/dev/null || true
}
: > "$MANIFEST_FILE" 2>/dev/null || true
manifest_put meta run_date "$TIMESTAMP"
manifest_put meta pool_source "$POOL_SOURCE"
manifest_put meta pool "$(IFS=,; echo "${PROJECT_POOL[*]}")"
manifest_put meta planned_phases \
  "$(( ${#PHASES[@]} * ${#PROJECT_POOL[@]} + ${#DREAM_GLOBAL_PHASES[@]} ))"
manifest_put meta started "$(date -Iseconds)"

log "=== Dream started (project=$PROJECT_KEY, provider=$BRAIN_DREAM_AGENT_PROVIDER, dry_run=$DRY_RUN, promote_enabled=$BRAIN_DREAM_PROMOTE_ENABLED, reorg_enabled=$BRAIN_DREAM_REORG_ENABLED, reorg_dry_run=$BRAIN_DREAM_REORG_DRY_RUN) ==="
# The pool on its own line, and its SOURCE with it. Without the source, a
# single-project night does not say whether the drop-in was read or systemd ate
# the variable — both render the same line.
log "=== Pool (${#PROJECT_POOL[@]}) from $POOL_SOURCE: ${PROJECT_POOL[*]} ==="

# Preflights, run across the WHOLE chain — not on its first link alone.
#
# This is what decides whether a chain is worth anything. The preflight detects
# exactly what kills a rail: missing binary, missing token, expired
# subscription. Having it fail the NIGHT rather than the LINK would amount to
# killing the night on precisely the day the standby was meant to serve — the
# failure the chain exists to absorb.
#
# A link that does not pass its preflight is therefore REMOVED from the chain,
# and named. The night stops only if ALL of them fail: the fail-closed is kept,
# it simply bears on the set instead of on the first one.
#
# None of this is a silent fallback: every removal is logged with its reason,
# and the final PROVIDERS line says what the night sets off with.
preflight_provider() {
  local provider="$1"
  local binary runner label
  case "$provider" in
    codex)  binary="$BRAIN_DREAM_CODEX_BIN";  runner="scripts.dream.codex_runner";  label="Codex" ;;
    claude) binary="$BRAIN_DREAM_CLAUDE_BIN"; runner="scripts.dream.claude_runner"; label="Claude" ;;
    agy)    binary="$BRAIN_DREAM_AGY_BIN";    runner="scripts.dream.agy_runner";    label="Agy" ;;
    *)
      log "FAIL $provider preflight — unsupported provider"
      return 1
      ;;
  esac

  if ! command -v "$binary" >/dev/null 2>&1; then
    log "FAIL $label preflight — executable not found: $binary"
    return 1
  fi
  if [[ -z "${MCP_HTTP_TOKEN:-}" ]]; then
    log "FAIL $label preflight — MCP_HTTP_TOKEN is not set"
    return 1
  fi
  # The capability preflight is per PROJECT: the tool policy depends on the
  # key. Checking it on the first project alone would let a night start and then
  # fail at the third, after two projects had already been mutated.
  if [[ "$BRAIN_DREAM_CAPABILITY_ENFORCEMENT" == "true" ]]; then
    for _preflight_project in "${PROJECT_POOL[@]}"; do
      if ! uv run python -m "$runner" \
        --preflight-capabilities --project-key "$_preflight_project"; then
        log "FAIL $label preflight — Dream capability configuration is invalid for $_preflight_project"
        return 1
      fi
    done
  fi

  # The agy rail REQUIRES enforcement. Its tool guard is probed by its runner
  # inside --preflight-capabilities above; without enforcement that path is
  # never taken, and the phase would set off with a free shell and an admin
  # bearer. The link is therefore removed rather than run without a net.
  if [[ "$provider" == "agy" && "$BRAIN_DREAM_CAPABILITY_ENFORCEMENT" != "true" ]]; then
    log "FAIL $label preflight — le rail agy exige BRAIN_DREAM_CAPABILITY_ENFORCEMENT=true"
    return 1
  fi

  # Codex alone exposes a non-interactive authentication probe. It also pins
  # forced_login_method=chatgpt, so that an ambient API key cannot move these
  # phases onto usage billing without saying so. `claude` has no equivalent, and
  # inventing one that fails open would prove nothing — the absence is therefore
  # accepted, not papered over.
  if [[ "$provider" == "codex" ]]; then
    # No `set +e` / `set -e` here: a `set -e` placed INSIDE a function
    # survives its `return`, and the errexit thus restored made the script exit
    # on the `return 1` instead of letting the chain fall back on the next
    # link. The `|| true` captures the rc without touching the shell's mode.
    local codex_login_status codex_login_rc=0
    codex_login_status="$("$binary" login status 2>&1)" || codex_login_rc=$?
    if (( codex_login_rc != 0 )) || [[ "$codex_login_status" != *"Logged in using ChatGPT"* ]]; then
      log "FAIL $label preflight — ChatGPT login is not active"
      return 1
    fi
  fi

  log "PREFLIGHT $label — ready"
  return 0
}

declare -a READY_PROVIDERS=()
for _provider in "${PROVIDER_CHAIN[@]}"; do
  set +e
  preflight_provider "$_provider"
  _preflight_rc=$?
  set -e
  if (( _preflight_rc == 0 )); then
    READY_PROVIDERS+=("$_provider")
  else
    log "DROP $_provider — préflight échoué, retiré de la chaîne de cette nuit"
  fi
done

if (( ${#READY_PROVIDERS[@]} == 0 )); then
  log "FAIL — aucun provider de la chaîne ne passe son préflight (${PROVIDER_CHAIN[*]})"
  exit 1
fi

PROVIDER_CHAIN=("${READY_PROVIDERS[@]}")
BRAIN_DREAM_AGENT_PROVIDER="${PROVIDER_CHAIN[0]}"
log "=== Providers (${#PROVIDER_CHAIN[@]}) prêts, dans l'ordre : ${PROVIDER_CHAIN[*]} ==="

# Defense-in-depth: scrub any new XML tool-call leaks that landed in
# learnings/decisions since the last run. Pure deterministic cleanup,
# non-LLM, quiet single-line summary. Root cause of the leak is upstream
# in Claude Code MCP client tool-call serialization (2026-04-22 decision
# 509783da) — safe to re-run daily because it's idempotent and protected
# against meta-reference false positives by regex-level tests.
# Failures here MUST NOT abort the dream run; we capture and continue.
set +e
uv run python -m scripts.scrub_xml_tool_call_leak --live --quiet \
  2>&1 | tee -a "$LOG_DIR/$TIMESTAMP.log"
scrub_rc=${PIPESTATUS[0]}
set -e
if (( scrub_rc != 0 )); then
  log "WARN  pre-phase XML scrub failed (rc=$scrub_rc); continuing dream run"
fi

declare -a FAILED_PHASES=()
declare -a TIMED_OUT_PHASES=()
# A subset of TIMED_OUT_PHASES: the BOUNDED deadlines a phase imposes on itself
# AFTER recording its terminal dream_run. That is not a breakdown, it is a
# normal night carried to its time limit. They alert (the FAIL_TOTAL counter)
# but no longer redden the systemd unit. Fail-closed by construction: a timeout
# one forgets to classify here stays counted as a failure, hence noisy. An
# EXTERNAL guard-rail timeout (`timeout` killed the process) NEVER enters here:
# the phase's state is
# unknown.
declare -a CONTROLLED_TIMEOUT_PHASES=()
declare -a SKIPPED_PHASES=()
# Skips WITHOUT a dream_runs row — the only count the reconciliation must
# subtract: a skip that DOES write (empty promote pool, record-empty-pool) is
# already in pairs_written, and subtracting it too would give gap=-1 on a
# healthy night (2nd fix from the PR 47 review, wolf-cry by the other path).
SKIPPED_UNWRITTEN=0
# Phases a standby link caught. Deliberately OUTSIDE FAIL_TOTAL and outside the
# exit guard: they succeeded. They exist only so the summary can tell "codex
# worked" from "codex died, agy saved the night" — the distinction
# `run_phase_chain` already logs per phase but that nothing aggregated, for six
# nights running.
declare -a FALLBACK_PHASES=()
TOTAL_PHASES=0

# --- Pre-flight gate: skip the costly deep phases (synth/promote/reorg) when
# the brain corpus is provably unchanged since the previous run. ~40% of nights
# re-process a static corpus, decide nothing (0 tool_calls) and burn ~$1
# (2026-06-22 audit). The verdict is computed once; the decision logic lives in
# Python (scripts.dream.dream_preflight) and is fail-safe — any error or
# uncertainty prints RUN, so a wrong skip is impossible.
OPUS_SKIP=false
preflight_verdict="$(
  uv run python -m scripts.dream.dream_preflight --date "$TIMESTAMP" \
    2>> "$LOG_DIR/$TIMESTAMP.log" || echo "RUN"
)"
if [[ "$preflight_verdict" == SKIP* ]]; then
  OPUS_SKIP=true
  log "PREFLIGHT $preflight_verdict"
  log "PREFLIGHT → skipping synth/promote/reorg (deep phases) this run"
else
  log "PREFLIGHT RUN — corpus changed since last run (or check inconclusive)"
fi

# Serves ONE project: its six agent phases, in order, with their killswitches
# and their validators. Extracting this block into a function is not cosmetic.
#
# §9 lists five `continue` statements that belong to the PHASE loop. A project
# loop placed AROUND them would not have broken them — bash always binds them to
# the innermost loop — but it would have made the reading ambiguous enough that
# the next rework breaks it. A function boundary removes the question: this body
# has exactly one loop.
#
# The function writes into the counter arrays, which stay global: they aggregate
# the whole night, all projects together.
run_project_phases() {
  # A GLOBAL assignment, deliberately not `local`: run_phase, the three
  # validators and _render_prompt all read $PROJECT_KEY as a global.
  PROJECT_KEY="$1"

  # §3.4 — these two exports survive across iterations. If a project skips
  # PROMOTE (killswitch, empty pool, or a promote_prepare failure), the PREVIOUS
  # project's value stays loaded and the current project promotes on someone
  # else's pool. The reset sits at the HEAD of the iteration, not at the tail:
  # the body's five `continue` statements would skip a cleanup placed at the end.
  export PROMOTE_CANDIDATE_POOL_JSON='[]'
  export PROMOTE_RECENT_PROMOTIONS_JSON='[]'
  CANDIDATES_JSON=""
  DREAM_RUN_ID=""
  REORG_RUN_ID=""
  REORG_TAGS_BEFORE=""

  log "--- Projet $PROJECT_KEY ---"

  for phase_spec in "${PHASES[@]}"; do
    IFS=':' read -r name model_tier timeout max_turns <<< "$phase_spec"
    TOTAL_PHASES=$(( TOTAL_PHASES + 1 ))
    # Same site, same instant as the counter: a SEVENTH phase added to PHASES
    # extends the expectation on its own, with no guard to maintain elsewhere.
    manifest_put expected "$name" "$PROJECT_KEY"

    # --- Pre-flight: skip the costly Opus phases on a provably-static corpus ---
    if [[ "$OPUS_SKIP" == true ]] \
       && { [[ "$name" == "synth" ]] || [[ "$name" == "promote" ]] || [[ "$name" == "reorg" ]]; }; then
      log "SKIP $name (pre-flight: corpus unchanged since last run)"
      SKIPPED_PHASES+=("$PROJECT_KEY/$name")
      SKIPPED_UNWRITTEN=$(( SKIPPED_UNWRITTEN + 1 ))
      manifest_put skipped "$name" "$PROJECT_KEY" preflight
      continue
    fi

    # --- PROMOTE: killswitch + candidate-pool pre-compute ------------------
    if [[ "$name" == "promote" ]]; then
      if [[ "$BRAIN_DREAM_PROMOTE_ENABLED" != "true" ]]; then
        log "SKIP promote (killswitch BRAIN_DREAM_PROMOTE_ENABLED=$BRAIN_DREAM_PROMOTE_ENABLED)"
        SKIPPED_PHASES+=("$PROJECT_KEY/promote")
        SKIPPED_UNWRITTEN=$(( SKIPPED_UNWRITTEN + 1 ))
        manifest_put skipped promote "$PROJECT_KEY" killswitch
        continue
      fi

      CANDIDATES_JSON="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_promote_candidates.json"
      promote_prep_start=$SECONDS
      set +e
      uv run python -m scripts.dream.promote_prepare \
        --project-key "$PROJECT_KEY" --limit 10 \
        > "$CANDIDATES_JSON" 2>> "$LOG_DIR/$TIMESTAMP.log"
      prep_rc=$?
      set -e
      if (( prep_rc != 0 )); then
        log "FAIL promote — candidate pool fetch failed (rc=$prep_rc)"
        FAILED_PHASES+=("$PROJECT_KEY/promote")
        manifest_put failed promote "$PROJECT_KEY"
        continue
      fi

      pool_size=$(jq 'length' "$CANDIDATES_JSON" 2>/dev/null || echo 0)
      if [[ "$pool_size" -eq 0 ]]; then
        log "SKIP promote — empty candidate pool"
        # Synthesize a no_candidates report so downstream tooling has
        # something to read (otel_split + dream_parser rely on the file).
        {
          echo '=== PROMOTE REPORT ==='
          echo '{"dry_run":false,"target_type":"none","reason":"no_candidates"}'
          echo '=== END ==='
        } > "$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_promote.log"
        # Record a REAL dream_runs row for the phase. Promote stays an EXPECTED
        # phase (killswitch open), so with no row at all post_run_alert
        # manufactures a synthetic `partial` every single night — a false alarm
        # that pushes the operator to "repair" the migration-041 maturity filter
        # that legitimately emptied the pool. Writing the row makes the phase
        # OBSERVED; a promote that really crashes still writes nothing and still
        # rings. Non-fatal: a missing row only brings the alarm back.
        set +e
        uv run python -m scripts.dream._promote_helpers record-empty-pool \
          --date "$TIMESTAMP" --duration-seconds "$(( SECONDS - promote_prep_start ))" \
          --project-key "$PROJECT_KEY" \
          >> "$LOG_DIR/$TIMESTAMP.log" 2>&1
        record_rc=$?
        set -e
        # The declaration lives INSIDE each of the two branches, never after
        # the `if`: the `SKIPPED_PHASES+=` push below is common to both, so
        # "skipped" and "its row is written" are two INDEPENDENT facts. A single
        # reason would declare "no row owed" while dream.sh has just printed
        # that the write FAILED, and the hole would be mute.
        if (( record_rc == 0 )); then
          log "promote — empty-pool dream_runs row recorded (phase observed, not failed)"
          manifest_put skipped promote "$PROJECT_KEY" empty-pool-recorded
        else
          log "WARN  promote — empty-pool dream_runs row NOT recorded (rc=$record_rc)"
          manifest_put skipped promote "$PROJECT_KEY" empty-pool-unrecorded
          SKIPPED_UNWRITTEN=$(( SKIPPED_UNWRITTEN + 1 ))
        fi
        SKIPPED_PHASES+=("$PROJECT_KEY/promote")
        continue
      fi

      export PROMOTE_CANDIDATE_POOL_JSON
      PROMOTE_CANDIDATE_POOL_JSON="$(cat "$CANDIDATES_JSON")"
      export PROMOTE_RECENT_PROMOTIONS_JSON
      PROMOTE_RECENT_PROMOTIONS_JSON="$(
        uv run python -m scripts.dream._promote_helpers recent-promotions --limit 10 \
          2>> "$LOG_DIR/$TIMESTAMP.log" || echo '[]'
      )"
    fi

    # --- REORG: killswitch --------------------------------------------------
    # Independent of PROMOTE. Night 3 DRY_RUN (2026-04-23) surfaced two
    # tool-shape blockers (brain_list token ceiling, brain_get missing
    # access_count / freshness_status) that make WET-mode guardrails
    # structurally unverifiable. Keep REORG gated until both ship.
    if [[ "$name" == "reorg" ]]; then
      if [[ "$BRAIN_DREAM_REORG_ENABLED" != "true" ]]; then
        log "SKIP reorg (killswitch BRAIN_DREAM_REORG_ENABLED=$BRAIN_DREAM_REORG_ENABLED)"
        SKIPPED_PHASES+=("$PROJECT_KEY/reorg")
        SKIPPED_UNWRITTEN=$(( SKIPPED_UNWRITTEN + 1 ))
        manifest_put skipped reorg "$PROJECT_KEY" killswitch
        continue
      fi
    fi

    # --- REORG: pre-phase tags snapshot ------------------------------------
    # The post-phase validator compares the tags of the entities declared
    # mutated against those from BEFORE. It is the only observed "before": the
    # check it replaces, `updated_at >= run_date`, was hollow — DecayFlusher
    # refreshes the timestamp every 300 s through a trigger with no WHEN clause,
    # and it is REORG's own reads that feed it.
    #
    # Taken AFTER the killswitch (a phase cut off does not pay for the query)
    # and BEFORE run_phase_chain, hence once only for the two attempts the retry
    # budget allows: the "before" is the one from before the FIRST write, not
    # from before the last.
    if [[ "$name" == "reorg" ]]; then
      REORG_TAGS_BEFORE="$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_reorg_tags_before.json"
      set +e
      uv run python -m scripts.dream.reorg_snapshot --project-key "$PROJECT_KEY" \
        > "$REORG_TAGS_BEFORE" 2>> "$LOG_DIR/$TIMESTAMP.log"
      snapshot_rc=$?
      set -e
      if (( snapshot_rc != 0 )); then
        # The validator will refuse the report for want of a readable snapshot
        # — intended and fail-closed. This line is what allows tracing the
        # refusal back to its cause, instead of suspecting the agent's report.
        log "WARN  reorg — pre-phase tags snapshot failed (rc=$snapshot_rc); the validator will refuse the report"
      fi
    fi

    # `set -e` is active, so we must guard the call (run_phase's non-zero
    # return is expected on phase failure and MUST NOT abort the script — we
    # want every phase to run for diagnostic completeness).
    set +e
    run_phase_chain "$name" "$model_tier" "$timeout" "$max_turns"
    phase_rc=$?
    # Retry once on hard-fail (exit 1) — NOT on timeout (2), because timeouts
    # already burned the full budget and retrying would double the wall-clock
    # cost. Previously a single transient fail lost the whole phase (e.g.
    # 2026-04-14 scan hit max_turns=20). PROMOTE is NOT retried because a
    # partial materialization (atomic repo write committed, something
    # downstream hiccuped) would surface as a duplicate-source IntegrityError
    # on retry — ambiguous signal, safer to leave the validator handle it.
    #
    # §10 — the retry is the ONLY budget that is genuinely a night-wide
    # resource, and it is the one that multiplies by the number of projects: +43
    # eligible minutes PER PROJECT, that is +344 minutes of ceiling at eight. A
    # night-wide allocation brings the configured worst case from 803 min to ~489.
    if (( phase_rc == 1 )) && [[ "$name" != "promote" ]]; then
      if (( RETRY_BUDGET_LEFT > 0 )); then
        RETRY_BUDGET_LEFT=$(( RETRY_BUDGET_LEFT - 1 ))
        log "RETRY $PROJECT_KEY/$name (first attempt failed, re-running once; night budget left=$RETRY_BUDGET_LEFT)"
        run_phase_chain "$name" "$model_tier" "$timeout" "$max_turns"
        phase_rc=$?
      else
        # Not a silent failure: the phase keeps its rc=1 and reddens the unit
        # as before. Only the SECOND chance disappears, and the log says so.
        log "NO-RETRY $PROJECT_KEY/$name — night retry budget exhausted (BRAIN_DREAM_RETRY_BUDGET=$BRAIN_DREAM_RETRY_BUDGET)"
      fi
    fi
    set -e

    # --- CONNECT: post-phase validator ------------------------------------
    # A zero agent exit is insufficient: the exact report must also contain
    # zero tool-level errors. Validation runs after the final retry so only the
    # retained report determines the phase outcome.
    if [[ "$name" == "connect" && "$phase_rc" == "0" ]]; then
      set +e
      uv run python -m scripts.dream.connect_validate \
        --report-log "$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.log" \
        --run-date "$TIMESTAMP" \
        --project-key "$PROJECT_KEY" \
        >> "$LOG_DIR/$TIMESTAMP.log" 2>&1
      validator_rc=$?
      set -e
      if (( validator_rc != 0 )); then
        log "FAIL connect — validator rejected CONNECT report; see validation detail"
        phase_rc=1
      fi
    fi

    # --- PROMOTE: post-phase validator ------------------------------------
    if [[ "$name" == "promote" && "$phase_rc" == "0" ]]; then
      DREAM_RUN_ID=$(
        uv run python -m scripts.dream._promote_helpers dream-run-id --date "$TIMESTAMP" \
          --project-key "$PROJECT_KEY" \
          2>> "$LOG_DIR/$TIMESTAMP.log" | tr -d '\n'
      )
      set +e
      uv run python -m scripts.dream.promote_validate \
        --report-log "$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.log" \
        --candidates-json "$CANDIDATES_JSON" \
        --project-key "$PROJECT_KEY" \
        ${DREAM_RUN_ID:+--dream-run-id "$DREAM_RUN_ID"} \
        >> "$LOG_DIR/$TIMESTAMP.log" 2>&1
      validator_rc=$?
      set -e
      if (( validator_rc != 0 )); then
        log "FAIL promote — validator rejected PROMOTE report; see validation detail"
        phase_rc=1
      fi
    fi

    # --- REORG: post-phase validator --------------------------------------
    # Symmetric to PROMOTE's validator. Runs on EVERY reorg outcome, green or
    # not. It used to be gated on `phase_rc == 0`, which removed the check from
    # exactly the case it serves: a phase that dies or times out has already had
    # its tool calls land, and those partial writes are the ones nobody re-reads.
    # A green phase at least emitted its report and followed its prompt to the end.
    # The validator never fails the pipeline — it marks the dream_runs row partial
    # and exits 1, which we translate to phase_rc=1 so the FAIL_TOTAL counter
    # captures it, but the pipeline continues.
    # In dry-run mode the validator detects this from the JSON trailer and
    # skips all DB checks (nothing should have mutated).
    #
    # NOTE: effective_dry_run is local to run_phase() and is out of scope
    # here.  Recompute the same logic from the global inputs — this is the
    # exact same derivation run_phase uses for the REORG phase.
    if [[ "$name" == "reorg" ]]; then
      reorg_effective_dry_run="$DRY_RUN"
      [[ "$BRAIN_DREAM_REORG_DRY_RUN" == "true" ]] && reorg_effective_dry_run="true"

      # Fetch the dream_runs.id for this reorg run (same helper, different phase filter).
      REORG_RUN_ID=$(
        uv run python -c "
import asyncio, sys, datetime as dt, sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from brain_v42.config import Settings
from brain_v42.db.tables import dream_runs

async def _get() -> None:
    eng = create_async_engine(Settings().postgres_url, pool_pre_ping=True)
    sf = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with sf() as s:
        r = (await s.execute(
            sa.select(dream_runs.c.id)
            .where(dream_runs.c.phase == 'reorg')
            .where(dream_runs.c.run_date == dt.date.fromisoformat('$TIMESTAMP'))
            .where(dream_runs.c.project_key == '$PROJECT_KEY')
            .order_by(dream_runs.c.id.desc())
            .limit(1)
        )).scalar_one_or_none()
    sys.stdout.write(str(r) if r is not None else '')

asyncio.run(_get())
" 2>> "$LOG_DIR/$TIMESTAMP.log" | tr -d '\n'
      )
      reorg_validator_flags=()
      reorg_validator_flags+=(--report-log "$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.log")
      reorg_validator_flags+=(--tags-before-json "$REORG_TAGS_BEFORE")
      # The phase's event stream — what the agent ACTUALLY called, against what
      # its report DECLARES. Same name construction as run_phase (line ~328).
      # A warning-only check for now.
      reorg_validator_flags+=(--events-jsonl "$LOG_DIR/${TIMESTAMP}_${PROJECT_KEY}_${name}.events.jsonl")
      # The run's perimeter, as for promote and connect. The server already
      # bounds REORG to its project, but brain_list is the only CRUD tool with
      # no scope check OF ITS OWN — its bound lives in the middleware alone, and
      # enforcement defaults to false in the code. If that falls away, this is
      # the last place that can still say a pass crossed the border.
      reorg_validator_flags+=(--project-key "$PROJECT_KEY")
      [[ -n "$REORG_RUN_ID" ]] && reorg_validator_flags+=(--dream-run-id "$REORG_RUN_ID")
      [[ "$reorg_effective_dry_run" == "true" ]] && reorg_validator_flags+=(--dry-run)
      set +e
      uv run python -m scripts.dream.reorg_validate \
        "${reorg_validator_flags[@]}" \
        >> "$LOG_DIR/$TIMESTAMP.log" 2>&1
      validator_rc=$?
      set -e
      if (( validator_rc != 0 )); then
        if (( phase_rc == 0 )); then
          log "FAIL reorg — validator rejected REORG report; see validation detail"
          phase_rc=1
        else
          # The phase has ALREADY fallen: the `case` below files a 2 into
          # TIMED_OUT_PHASES and a 1 into FAILED_PHASES. Overwriting the 2 with
          # a 1 would report a hard failure in place of the budget overrun that
          # actually happened, and the operator would hunt the wrong breakdown.
          # So the verdict adds to the log, not to the classification.
          log "FAIL reorg — validator rejected REORG report; see validation detail (phase already rc=$phase_rc; classification unchanged)"
        fi
      fi
    fi

    case "$phase_rc" in
      0) ;;
      2) TIMED_OUT_PHASES+=("$PROJECT_KEY/$name")
         manifest_put timeout "$name" "$PROJECT_KEY" ;;
      *) FAILED_PHASES+=("$PROJECT_KEY/$name")
         manifest_put failed "$name" "$PROJECT_KEY" ;;
    esac
  done
}

for _project in "${PROJECT_POOL[@]}"; do
  run_project_phases "$_project"
done  # fin de la boucle de projets


# --- EXTRACT: ticket knowledge extraction (proposer-only) -----------------
# Not a claude -p phase: a direct Python CLI (the domain_backfill pattern,
# NVIDIA API strict JSON without tools). Inserts its own dream_runs row
# (phase='extract') for briefing visibility (killswitches + last failure).
TOTAL_PHASES=$(( TOTAL_PHASES + 1 ))
manifest_put expected extract '*'
if [[ "$BRAIN_DREAM_EXTRACT_ENABLED" != "true" ]]; then
  log "SKIP extract (killswitch BRAIN_DREAM_EXTRACT_ENABLED=$BRAIN_DREAM_EXTRACT_ENABLED)"
  SKIPPED_PHASES+=("*/extract")
  SKIPPED_UNWRITTEN=$(( SKIPPED_UNWRITTEN + 1 ))
  manifest_put skipped extract '*' killswitch
else
  # The CLI owns a 9m deadline and checkpoints each ticket before returning
  # rc=3. The outer 10m timeout remains only as a last-resort process guard.
  extract_args=(--limit 20 --run-budget-seconds 540 --ticket-budget-seconds 180)
  if [[ "$BRAIN_DREAM_EXTRACT_DRY_RUN" != "true" ]]; then
    extract_args+=(--wet)
  fi
  log "extract: ticket_extract starting (dry_run=$BRAIN_DREAM_EXTRACT_DRY_RUN)"
  set +e
  timeout 10m uv run python -m scripts.ticket_extract "${extract_args[@]}" \
    >> "$LOG_DIR/${TIMESTAMP}_extract.log" 2>&1
  extract_rc=$?
  set -e
  if (( extract_rc == 0 )); then
    log "DONE extract"
  elif (( extract_rc == 3 )); then
    log "TIMEOUT extract (controlled deadline; terminal dream_run recorded)"
    TIMED_OUT_PHASES+=("*/extract")
    CONTROLLED_TIMEOUT_PHASES+=("*/extract")
    manifest_put timeout extract '*'
  elif (( extract_rc == 4 )); then
    # Deferral, not failure: the run declined to START tickets it could not
    # finish inside the budget, so nothing was cut short. Deferred tickets keep
    # their row in ticket_extraction_attempts and are retried first next run,
    # oldest closed_at first. Marking the unit failed here fired the alarm every
    # single night on designed behaviour, which is how it stopped being read.
    log "DEFERRED extract (nominal — tickets owed, see ticket_extraction_attempts)"
  elif (( extract_rc == 124 )); then
    log "TIMEOUT extract (outer guard; inspect ${TIMESTAMP}_extract.log)"
    TIMED_OUT_PHASES+=("*/extract")
    manifest_put timeout extract '*'
  else
    log "FAIL extract (rc=$extract_rc) — see ${TIMESTAMP}_extract.log"
    FAILED_PHASES+=("*/extract")
    manifest_put failed extract '*'
  fi
fi

# --- ROADMAP: nightly roadmap curation (proposer-only) ---------------------
# Not a claude -p phase: a direct Python CLI (the extract pattern). Inserts its
# own dream_runs row (phase='roadmap') for briefing visibility.
TOTAL_PHASES=$(( TOTAL_PHASES + 1 ))
manifest_put expected roadmap '*'
if [[ "$BRAIN_DREAM_ROADMAP_ENABLED" != "true" ]]; then
  log "SKIP roadmap (killswitch BRAIN_DREAM_ROADMAP_ENABLED=$BRAIN_DREAM_ROADMAP_ENABLED)"
  SKIPPED_PHASES+=("*/roadmap")
  SKIPPED_UNWRITTEN=$(( SKIPPED_UNWRITTEN + 1 ))
  manifest_put skipped roadmap '*' killswitch
else
  roadmap_args=(--limit 10)
  if [[ "$BRAIN_DREAM_ROADMAP_DRY_RUN" != "true" ]]; then
    roadmap_args+=(--wet)
  fi
  log "roadmap: roadmap_curate starting (dry_run=$BRAIN_DREAM_ROADMAP_DRY_RUN)"
  set +e
  # 20m: the first real run (2026-07-04) hit 597s/600s — zero margin under 10m.
  # Pinned par tests/unit/test_dream_sh_roadmap.py.
  timeout 20m uv run python -m scripts.roadmap_curate "${roadmap_args[@]}" \
    >> "$LOG_DIR/${TIMESTAMP}_roadmap.log" 2>&1
  roadmap_rc=$?
  set -e
  if (( roadmap_rc == 0 )); then
    log "DONE roadmap"
  else
    log "FAIL roadmap (rc=$roadmap_rc) — see ${TIMESTAMP}_roadmap.log"
    FAILED_PHASES+=("*/roadmap")
    manifest_put failed roadmap '*'
  fi
fi

# --- SWEEP: draining the ghost sessions -------------------------------------
# Not an agent phase: a direct Python CLI (the extract/roadmap pattern). Inserts
# its own dream_runs row (phase='sweep', model NULL) for briefing visibility.
# The threshold is NOT passed as an argument: one constant only.
TOTAL_PHASES=$(( TOTAL_PHASES + 1 ))
manifest_put expected sweep '*'
if [[ "$BRAIN_DREAM_SWEEP_ENABLED" != "true" ]]; then
  log "SKIP sweep (killswitch BRAIN_DREAM_SWEEP_ENABLED=$BRAIN_DREAM_SWEEP_ENABLED)"
  SKIPPED_PHASES+=("*/sweep")
  SKIPPED_UNWRITTEN=$(( SKIPPED_UNWRITTEN + 1 ))
  manifest_put skipped sweep '*' killswitch
else
  sweep_args=()
  if [[ "$BRAIN_DREAM_SWEEP_DRY_RUN" != "true" ]]; then
    sweep_args+=(--wet)
  fi
  log "sweep: session_sweep starting (dry_run=$BRAIN_DREAM_SWEEP_DRY_RUN)"
  set +e
  # 5m: one indexed query, with no model call and no network. An overrun
  # signals a database in trouble, not a slow phase.
  timeout 5m uv run python -m brain_v42.maintenance.session_sweep "${sweep_args[@]}" \
    >> "$LOG_DIR/${TIMESTAMP}_sweep.log" 2>&1
  sweep_rc=$?
  set -e
  if (( sweep_rc == 0 )); then
    log "DONE sweep"
  else
    log "FAIL sweep (rc=$sweep_rc) — see ${TIMESTAMP}_sweep.log"
    FAILED_PHASES+=("*/sweep")
    manifest_put failed sweep '*'
  fi
fi

# Two distinct questions — above all not one.
#   FAIL_TOTAL: "should we ALERT?" Sensitivity unchanged since day one; it also
#               drives OK_TOTAL and the summary.
#   the exit guard below: "should the systemd unit turn red?"
# Merging them would restore the original defect: either the unit red every
# night (a state carrying no information), or the alert extinguished with it.
FAIL_TOTAL=$(( ${#FAILED_PHASES[@]} + ${#TIMED_OUT_PHASES[@]} ))
OK_TOTAL=$(( TOTAL_PHASES - FAIL_TOTAL ))

# The summary tells the whole truth, whatever exit code is chosen below: no
# timeout is hidden, we merely stop turning one into a unit failure when the
# deadline was bounded (see the 2026-04-09 postmortem: an undetected silent
# timeout — and 2026-08-07: a permanently red unit, become just as
# mute).
summary="${OK_TOTAL}/${TOTAL_PHASES} phases OK"
if (( ${#FAILED_PHASES[@]} > 0 )); then
  summary+=", ${#FAILED_PHASES[@]} failed (${FAILED_PHASES[*]})"
fi
if (( ${#TIMED_OUT_PHASES[@]} > 0 )); then
  summary+=", ${#TIMED_OUT_PHASES[@]} timed out (${TIMED_OUT_PHASES[*]})"
fi
if (( ${#SKIPPED_PHASES[@]} > 0 )); then
  summary+=", ${#SKIPPED_PHASES[@]} skipped (${SKIPPED_PHASES[*]})"
fi
# A phase that fell back is a SUCCESSFUL phase — so it enters neither
# FAIL_TOTAL nor the exit guard below, and that is intended: the standby did its
# job, reddening the unit for that would make it mute. But it must be SEEN. From
# 2026-08-11 to 2026-08-17 all 60 codex phases of every night failed, agy caught
# them all, and the night signed off "63/63 phases OK": six nights with no
# primary rail and not a single warning light. FAIL_TOTAL counts phases where
# dream_runs counts attempts, and only the summary is read in the morning.
if (( ${#FALLBACK_PHASES[@]} > 0 )); then
  summary+=", ${#FALLBACK_PHASES[@]} repliées sur un secours (${FALLBACK_PHASES[*]})"
fi
log "=== Dream finished: $summary ==="

# The CLOSING block, the manifest's only non-incremental part. Its absence is
# the marker of an interrupted night: the reader then refuses any green verdict.
# `total_phases` is dream.sh's own counter, to be set against the header's
# `planned_phases` and against the number of expectations actually reached —
# three numbers, three instants, three code paths.
manifest_put meta total_phases "$TOTAL_PHASES"
manifest_put meta ok_total "$OK_TOTAL"
manifest_put meta fail_total "$FAIL_TOTAL"
manifest_put meta finished "$(date -Iseconds)"

# Keep a bounded operational report in the dated Dream log. Session briefings
# read the same failures directly from dream_runs. The helper's failure must
# NOT mask the Dream failure exit code — it is added to it, never substituted.
set +e
# ONE alert, after the loop, grouped by project (§11). Not one per project: the
# report does not filter on the key, so N invocations would produce N IDENTICAL
# blocks listing every project's failures.
#
# The output is CAPTURED, no longer redirected blind. `log()` does a `tee` to
# stdout — hence to journald — where this redirection wrote only into the dated
# file: the body of the alert never reached `journalctl`, which is the physical
# half of "nobody reads it" (ticket 0a9c067e). The report is bounded upstream
# (MAX_FETCHED_FAILURES), so capturing it in a variable is bounded too.
alert_out="$(uv run python -m scripts.dream.post_run_alert \
  --date "$TIMESTAMP" --manifest "$MANIFEST_FILE" --phases-ok "$OK_TOTAL" \
  --phases-skipped "$SKIPPED_UNWRITTEN" 2>&1)"
alert_rc=$?
set -e
printf '%s\n' "$alert_out" >> "$LOG_DIR/$TIMESTAMP.log"
coverage_line="$(printf '%s\n' "$alert_out" | grep -m1 '^COVERAGE ' || true)"
if [[ -n "$coverage_line" ]]; then
  # The two numbers the ticket says nobody reconciles, side by side in
  # journald, EVERY night — green ones included, without which the line would
  # be read only on the days it is already too late.
  log "=== dream_runs $coverage_line ==="
fi
# The phases-to-rows reconciliation (b95c5742): OK_TOTAL has just been counted
# by THIS loop, pairs_written comes from the database. A gap>0 is the loss of
# 15-16/08 — 61 phases OK, 2 rows, 240 swallowed InvalidPasswordError — made
# readable in the morning without cross-checking the log. The INSERT stays
# best-effort: nothing here reddens the unit, the line IS the fix.
reconciliation_line="$(printf '%s\n' "$alert_out" | grep -m1 '^RECONCILIATION ' || true)"
if [[ -n "$reconciliation_line" ]]; then
  log "=== dream_runs $reconciliation_line ==="
  if [[ "$reconciliation_line" != *" gap=0"* ]]; then
    log "WARN  dream_runs réconciliation — écart phases OK ↔ paires écrites non nul (INSERT best-effort perdu ? rejeu ? voir b95c5742)"
  fi
fi
# The manifest reader's in-band fallback (e30a1cec): the reporter NEVER returns
# 2 without a manifest — undecidable pairs do not make a red — but HERE the
# manifest has just been written by this very night. If it is unreadable, the
# instrument is the thing that is broken, and that gets engraved (a `coverage`
# dream_runs row) and SAID, without touching the night's exit code.
if [[ "$coverage_line" == *"mode=fallback"* ]]; then
  log "FAIL  dream_runs coverage — mode=fallback in-band : la nuit vient d'écrire $MANIFEST_FILE et l'alerte n'a pas pu le lire"
  set +e
  uv run python -m scripts.dream.record_coverage_gap \
    --date "$TIMESTAMP" --summary "$coverage_line" \
    --detail "manifest in-band illisible ou perdu : $MANIFEST_FILE" \
    >> "$LOG_DIR/$TIMESTAMP.log" 2>&1
  record_fallback_rc=$?
  set -e
  if (( record_fallback_rc != 0 )); then
    log "WARN  coverage — repli in-band NON enregistré (rc=$record_fallback_rc)"
  fi
fi
# The 2 is BELIEVED only on positive proof: the verdict's machine line. That
# code is also argparse's usage error, which `main()` cannot intercept
# (`SystemExit` is not an `Exception`), and it is `uv`'s and the interpreter's
# on an invalid command line. The trigger is not theoretical: on a hard
# rollback of the reader, dream.sh keeps passing `--manifest` to a
# post_run_alert that no longer knows it. Without this guard, every morning
# would print a coverage FAIL and write a lying `coverage` dream_runs row — over
# an unknown flag, with not a single hole. Renumbering the escalation would not
# close the class, positive proof does. A mute reporter stays RED: `alert_rc` is
# not reset and the structural guard below exits 1 (rule 3, "the reporter did
# not set off").
if (( alert_rc == 2 )) && [[ -n "$coverage_line" ]]; then
  log "FAIL  dream_runs coverage — des lignes attendues manquent sans explication"
  coverage_silent="$(printf '%s\n' "$alert_out" | grep -m1 '^COVERAGE_SILENT ' || true)"
  # T2 — the verdict carried to a reader that exists. This line reaches the
  # session briefing's "### Last failure" and /metrics nightly.last_failure
  # WITHOUT a line of code on their side. T1 alone reaches journald only, and the
  # ticket's lesson is that a signal without a reader cannot be told from none.
  #
  # `set +e` is INDISPENSABLE: errexit is active here, the exit guard lives
  # some thirty lines below, and this writer returns 1 on failure — like
  # `record-empty-pool`, on which it is modelled. Without this bracketing,
  # dream.sh would exit BEFORE its structural guard, without even printing the WARN.
  set +e
  uv run python -m scripts.dream.record_coverage_gap \
    --date "$TIMESTAMP" --summary "$coverage_line" --detail "$coverage_silent" \
    >> "$LOG_DIR/$TIMESTAMP.log" 2>&1
  record_gap_rc=$?
  set -e
  if (( record_gap_rc != 0 )); then
    log "WARN  coverage — ligne dream_runs 'coverage' NON enregistrée (rc=$record_gap_rc)"
  fi
  # An emergency switch, read by dream.sh ALONE: it is not a phase, so it does
  # not belong in the killswitch table. Disarmed, it keeps printing the verdict
  # AND saying that it is disarmed — the detector cannot be switched off in
  # silence.
  if [[ "${BRAIN_DREAM_COVERAGE_STRICT:-true}" != "true" ]]; then
    log "WARN  escalade désarmée (BRAIN_DREAM_COVERAGE_STRICT=false) — unité laissée verte"
    alert_rc=0
  fi
elif (( alert_rc != 0 )); then
  log "WARN  post_run_alert failed (rc=$alert_rc)"
fi

# A STRUCTURAL guard, not an arithmetic one. The original form subtracted
# ${#CONTROLLED_TIMEOUT_PHASES[@]} from the total: it was fail-closed only while
# CONTROLLED ⊆ TIMED_OUT, an invariant no guard enforced — a phase entered by
# mistake into FAILED **and** CONTROLLED erased its own failure and the script
# exited 0 after printing "1 failed (synth)".
# Here FAILED_PHASES is queried for itself, hence unmaskable.
#   1. hard failure                   -> red
#   2. UNBOUNDED timeout (external guard rail, unknown state) -> red
#   3. mute reporter (the alert did NOT set off) -> red: since a night with a
#      controlled deadline exits 0, the log is the only witness left.
if (( ${#FAILED_PHASES[@]} > 0 )) \
  || (( ${#TIMED_OUT_PHASES[@]} > ${#CONTROLLED_TIMEOUT_PHASES[@]} )) \
  || (( alert_rc != 0 )); then
  exit 1
fi

# Controlled deadlines only: the night unfolded as planned right up to its time
# limit. The alert set off, the unit stays green — so that a `failed` means
# something again.
#
# Conditioned on FAIL_TOTAL ever since the clean case comes through here:
# without this guard, a night WITHOUT a single anomaly would sign off "bounded
# anomalies only", which is false and wears the line out exactly as a
# permanently red unit wears the colour out.
if (( FAIL_TOTAL > 0 )); then
  log "=== Dream exit 0 — anomalies bornées uniquement (échéances contrôlées) ==="
fi
exit 0
