#!/usr/bin/env bash
# Isolated PROMOTE-phase smoke test — cheap iteration for prompt debugging.
#
# Runs ONLY the PROMOTE phase against a fixture candidate pool, skipping
# scan/clean/connect/synth/reorg. Codex via ChatGPT is the default; Claude is
# retained only for an explicit rollback comparison.
#
# Usage:
#   scripts/dream/_promote_smoke.sh           # runs once, prints result
#   scripts/dream/_promote_smoke.sh 3         # runs 3 iterations
#
# Exit 0 = markers present with valid JSON body. Exit 1 = empty markers
# or malformed JSON. Stdout shows the last agent message; exit code
# tells the automation pass/fail.

set -euo pipefail

ITERATIONS="${1:-1}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

BRAIN_DREAM_AGENT_PROVIDER="${BRAIN_DREAM_AGENT_PROVIDER:-codex}"
BRAIN_DREAM_CAPABILITY_ENFORCEMENT="${BRAIN_DREAM_CAPABILITY_ENFORCEMENT-false}"
BRAIN_DREAM_CODEX_DEEP_MODEL="${BRAIN_DREAM_CODEX_DEEP_MODEL:-gpt-5.6-sol}"
BRAIN_DREAM_CODEX_DEEP_REASONING="${BRAIN_DREAM_CODEX_DEEP_REASONING:-high}"
BRAIN_DREAM_CODEX_BIN="${BRAIN_DREAM_CODEX_BIN:-codex}"
PROJECT_KEY="${BRAIN_DREAM_PROJECT_KEY:-brain-v42}"
case "$PROJECT_KEY" in
  brain|brain_v42) PROJECT_KEY="brain-v42" ;;
esac
case "$BRAIN_DREAM_AGENT_PROVIDER" in
  codex|claude) ;;
  *)
    echo "Unsupported BRAIN_DREAM_AGENT_PROVIDER: $BRAIN_DREAM_AGENT_PROVIDER" >&2
    exit 2
    ;;
esac
case "$BRAIN_DREAM_CAPABILITY_ENFORCEMENT" in
  true|false) ;;
  *)
    echo "Invalid BRAIN_DREAM_CAPABILITY_ENFORCEMENT value" >&2
    exit 2
    ;;
esac
# The refusal of claude under enforcement was lifted here as in dream.sh, and
# for the same reason: the rail now carries a bearer per (project, phase). The
# preflight follows the provider — both runners read the same registry, but
# having the check carried by the rail that will actually run avoids a claude
# smoke being validated by codex's preflight.
_PREFLIGHT_RUNNER="scripts.dream.codex_runner"
if [[ "$BRAIN_DREAM_AGENT_PROVIDER" == "claude" ]]; then
  _PREFLIGHT_RUNNER="scripts.dream.claude_runner"
fi
if [[ "$BRAIN_DREAM_CAPABILITY_ENFORCEMENT" == "true" ]] \
  && ! uv run python -m "$_PREFLIGHT_RUNNER" \
    --preflight-capabilities --project-key "$PROJECT_KEY"; then
  echo "Dream capability configuration is invalid" >&2
  exit 1
fi

# Block on MCP connection before turn 1 so the stdio brain-v42 server is always
# present (default claude -p is non-blocking → brain_* races in; see dream.sh
# and regression 27430ae1). NONBLOCKING=false flips claude to the blocking branch.
export MCP_CONNECTION_NONBLOCKING=false
export MCP_CONNECT_TIMEOUT_MS=10000

# Fixture: real candidate pool from last dream run (10 learnings).
FIXTURE="${FIXTURE:-logs/dream/2026-04-20_promote_candidates.json}"
if [[ ! -f "$FIXTURE" ]]; then
  echo "FIXTURE not found: $FIXTURE" >&2
  echo "Set FIXTURE=<path> or run a full dream.sh once to generate it." >&2
  exit 2
fi

POOL_JSON=$(cat "$FIXTURE")
DATE=$(date +%Y-%m-%d)
OUT_DIR="${OUT_DIR:-/tmp/promote_smoke_${DATE}}"
mkdir -p "$OUT_DIR"

# Optional: SYNTH log to prepend (mimic dream.sh dep injection).
SYNTH_LOG="${SYNTH_LOG:-logs/dream/2026-04-20_synth.log}"
SYNTH_PREAMBLE=""
if [[ -f "$SYNTH_LOG" && -s "$SYNTH_LOG" ]]; then
  SYNTH_PREAMBLE="## Previous Phase Reports (reference context — do not mimic style)
The orchestrator has injected the output from dependency phases below.

### SYNTH phase output
$(cat "$SYNTH_LOG")

---

"
fi

pass=0
fail=0

for i in $(seq 1 "$ITERATIONS"); do
  echo "=== iteration $i/$ITERATIONS ==="
  log="$OUT_DIR/iter_${i}.log"

  # Same rendering path as dream.sh line 81.
  base_prompt=$(python3 -m scripts.dream._render_prompt \
    scripts/dream/phase_promote.md "$PROJECT_KEY" "$DATE" true "$POOL_JSON" '[]')
  prompt="${SYNTH_PREAMBLE}${base_prompt}"

  runner_rc=0
  if [[ "$BRAIN_DREAM_AGENT_PROVIDER" == "codex" ]]; then
    events_log="$OUT_DIR/iter_${i}.events.jsonl"
    stderr_log="$OUT_DIR/iter_${i}.stderr.log"
    if printf '%s' "$prompt" | uv run python -m scripts.dream.codex_runner \
      --phase promote \
      --project-key "$PROJECT_KEY" \
      --model "$BRAIN_DREAM_CODEX_DEEP_MODEL" \
      --reasoning-effort "$BRAIN_DREAM_CODEX_DEEP_REASONING" \
      --timeout-seconds 480 \
      --report-log "$log" \
      --events-log "$events_log" \
      --stderr-log "$stderr_log" \
      --codex-executable "$BRAIN_DREAM_CODEX_BIN"; then
      :
    else
      runner_rc=$?
    fi
  else
    # The claude rail, scoped like dream.sh's since 2026-08-11. It no longer
    # goes through the repository's MCP config nor the tool wildcard: keeping
    # the old call here would make a "comparison" smoke run with the admin
    # token and the whole tool surface, hence compare nothing of the night it
    # is supposed to represent.
    if printf '%s' "$prompt" | uv run python -m scripts.dream.claude_runner \
      --phase promote \
      --project-key "$PROJECT_KEY" \
      --model opus \
      --max-turns 50 \
      --timeout-seconds 480 \
      --raw-log "$log" \
      --claude-executable "${BRAIN_DREAM_CLAUDE_BIN:-claude}"; then
      :
    else
      runner_rc=$?
    fi
  fi

  if (( runner_rc != 0 )); then
    fail=$((fail + 1))
    echo "FAIL: provider runner exited $runner_rc"
    [[ -f "${stderr_log:-}" ]] && tail -20 "$stderr_log" | sed 's/^/    /'
    continue
  fi

  # Validator regex from scripts/dream/promote_validate.py:_REPORT_RE.
  if python3 -c "
import re, sys
raw = open('$log').read()
m = re.search(r'===\s*PROMOTE\s+REPORT\s*===\s*(\{.*?\})\s*===\s*END\s*===', raw, re.DOTALL)
if m is None:
    print('FAIL: no JSON between markers')
    if '=== PROMOTE REPORT ===' in raw and '=== END ===' in raw:
        print('  markers present but body empty (the bug we are hunting)')
    sys.exit(1)
import json
try:
    d = json.loads(m.group(1))
    print('PASS: target_type=' + str(d.get('target_type')) + ', dry_run=' + str(d.get('dry_run')))
    print('  draft_title=' + str(d.get('draft_title', ''))[:80])
except json.JSONDecodeError as e:
    print('FAIL: malformed JSON: ' + str(e))
    sys.exit(1)
"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "  log: $log"
    echo "  tail:"
    tail -20 "$log" | sed 's/^/    /'
  fi
done

echo
echo "=== summary: $pass pass, $fail fail ($ITERATIONS total) ==="
if (( fail > 0 )); then
  exit 1
fi
