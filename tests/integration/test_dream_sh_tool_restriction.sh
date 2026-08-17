#!/usr/bin/env bash
# Integration test for scripts/dream.sh — built-in tool restriction.
#
# Validates that the claude rail runs with `--tools ""` so no built-in
# Claude Code tools (Bash, Read, Edit, Write, Agent, Task, ToolSearch,
# WebFetch, WebSearch, NotebookEdit, etc.) can be called by dream phases,
# and that the ONLY reachable MCP tools are those of the phase being run.
#
# The tool wildcard `mcp__brain-v42__*` was that second contract until
# 2026-08-11 and is now a FAILURE: a scoped bearer served with an unrestricted
# tool list is only half a firewall. Since that date the rail goes through
# scripts/dream/claude_runner.py, so the mock `uv` below delegates to the real
# runner — otherwise this file would measure its own mock.
#
# Background: on 2026-04-09 the synth phase timed out because Claude Opus
# dispatched a runaway subagent via the Agent tool (1 Agent call → 3 Read +
# 14 Bash in the child agent → 10m wallclock consumed). The script passed
# only `--permission-mode bypassPermissions --allowedTools "mcp__brain-v42__*"`
# which did NOT restrict built-ins — bypassPermissions lets every tool
# through without prompting. The fix is `--tools ""` which explicitly
# disables every built-in tool at CLI parse time.
#
# The test uses a mock `claude` that records every CLI arg it receives,
# then asserts the recorded args contain `--tools` followed by an empty
# string literal.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DREAM_SH_SRC="$REPO_ROOT/scripts/dream.sh"
DREAM_PROMPTS_SRC="$REPO_ROOT/scripts/dream"
FAIL_COUNT=0

pass() { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; FAIL_COUNT=$((FAIL_COUNT + 1)); }

echo "=== test_dream_sh_tool_restriction.sh ==="

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/scripts"
cp "$DREAM_SH_SRC" "$TMP/scripts/dream.sh"
cp -r "$DREAM_PROMPTS_SRC" "$TMP/scripts/dream"
chmod +x "$TMP/scripts/dream.sh"

MOCK_BIN="$TMP/bin"
mkdir -p "$MOCK_BIN"

# Mock `claude` that records every arg to a log file (one invocation per
# line: each arg is written as its own entry for robust parsing).
cat > "$MOCK_BIN/claude" <<'MOCK_CLAUDE'
#!/usr/bin/env bash
cat >/dev/null 2>&1 || true
{
  echo "---BEGIN CLAUDE CALL---"
  for arg in "$@"; do
    printf 'ARG:%s\n' "$arg"
  done
  echo "---END CLAUDE CALL---"
} >> "$CLAUDE_ARGS_LOG"
echo "[mock claude] phase output"
exit 0
MOCK_CLAUDE
chmod +x "$MOCK_BIN/claude"

cat > "$MOCK_BIN/uv" <<'MOCK_UV'
#!/usr/bin/env bash
if [[ -n "${UV_ARGS_LOG:-}" ]]; then
  printf '%s\n' "$*" >> "$UV_ARGS_LOG"
fi
# Le rail claude est passé derrière scripts.dream.claude_runner : c'est lui, et
# non plus dream.sh, qui construit la ligne de commande. Le mock DÉLÈGUE donc au
# vrai runner (REPO_PYTHON, injecté par le test) — sinon ce test mesurerait le
# mock. Le runner exécute ensuite le `claude` bouchonné trouvé dans le PATH, si
# bien que les arguments enregistrés sont ceux réellement produits par le code.
if [[ "${1:-} ${2:-} ${3:-} ${4:-}" == \
  "run python -m scripts.dream.claude_runner" ]]; then
  shift 4
  exec "$REPO_PYTHON" -m scripts.dream.claude_runner "$@"
fi
if [[ "${1:-} ${2:-} ${3:-} ${4:-}" == \
  "run python -m scripts.dream.codex_runner" ]]; then
  report_log=""
  while (($#)); do
    if [[ "$1" == "--report-log" ]]; then
      report_log="$2"
      shift 2
    else
      shift
    fi
  done
  cat >/dev/null 2>&1 || true
  if [[ -n "$report_log" ]]; then
    printf '%s\n' \
      '=== PROMOTE REPORT ===' \
      '{"dry_run":true,"target_type":"none","reason":"test"}' \
      '=== END ===' > "$report_log"
  fi
fi
exit 0
MOCK_UV
chmod +x "$MOCK_BIN/uv"

ARGS_LOG="$TMP/claude_args.log"
: > "$ARGS_LOG"

set +e
env -i HOME="$HOME" PATH="$MOCK_BIN:/usr/bin:/bin" \
  REPO_PYTHON="$REPO_ROOT/.venv/bin/python" PYTHONPATH="$REPO_ROOT" \
  MCP_HTTP_TOKEN=test-only-token \
  BRAIN_DREAM_AGENT_PROVIDER=claude \
  CLAUDE_ARGS_LOG="$ARGS_LOG" \
  "$TMP/scripts/dream.sh" test-project >"$TMP/run.out" 2>&1
rc=$?
set -e

if [[ "$rc" -eq 0 ]]; then
  pass "dream.sh completes successfully with mock claude"
else
  fail "dream.sh exited $rc (expected 0 — mock claude returns success)"
  sed 's/^/    /' "$TMP/run.out" >&2 || true
fi

# Count how many times claude was invoked. The test runs dream.sh under
# `env -i` which strips outer killswitches, so dream.sh defaults apply:
# BRAIN_DREAM_PROMOTE_ENABLED=false and BRAIN_DREAM_REORG_ENABLED=false.
# Only scan + clean + connect + synth call claude (4 phases). When a
# killswitch is flipped on in production, update this constant — the test
# isn't run from CI with overrides.
EXPECTED_CALLS=4
call_count=$(grep -c '^---BEGIN CLAUDE CALL---' "$ARGS_LOG" || true)
if [[ "$call_count" -eq "$EXPECTED_CALLS" ]]; then
  pass "claude invoked $EXPECTED_CALLS times (one per active phase)"
else
  fail "expected $EXPECTED_CALLS claude calls, got $call_count"
fi

# --- Assertion: every claude call includes --tools "" ---
#
# We expect to see an argument sequence where `--tools` is followed by an
# ARG: line with empty content (the empty-string literal). We count how
# many invocations have this pattern.
with_tools_restriction=0
while IFS= read -r line; do
  case "$line" in
    '---BEGIN CLAUDE CALL---')
      prev_was_tools=0
      has_restriction=0
      ;;
    '---END CLAUDE CALL---')
      (( has_restriction )) && with_tools_restriction=$((with_tools_restriction + 1))
      ;;
    'ARG:--tools')
      prev_was_tools=1
      ;;
    'ARG:')
      if (( prev_was_tools )); then
        has_restriction=1
      fi
      prev_was_tools=0
      ;;
    *)
      prev_was_tools=0
      ;;
  esac
done < "$ARGS_LOG"

if [[ "$with_tools_restriction" -eq "$call_count" ]]; then
  pass "every claude invocation includes --tools \"\" (restricts built-ins)"
else
  fail "only $with_tools_restriction/$call_count invocations include --tools \"\""
  echo "    (first call args follow)" >&2
  awk '/^---BEGIN CLAUDE CALL---/,/^---END CLAUDE CALL---/' "$ARGS_LOG" | head -25 | sed 's/^/    /' >&2 || true
fi

# --- Assertion: le joker a disparu au profit de l'allowlist EXACTE ---
#
# `mcp__brain-v42__*` a été le contrat de ce rail jusqu'au 2026-08-11. Il ne
# l'est plus : le joker et le jeton admin étaient les deux moitiés du même trou,
# et un bearer scopé servi avec une liste d'outils illimitée n'est qu'un demi
# pare-feu. Le contrat est désormais : brain reste joignable, mais UNIQUEMENT
# par les outils de la phase.
if grep -q '^ARG:mcp__brain-v42__\*$' "$ARGS_LOG"; then
  fail "le joker mcp__brain-v42__* est de retour — le pare-feu de capacité est contourné"
else
  pass "aucun joker d'outils dans les appels claude"
fi

# scan est la première phase : son allowlist exacte doit apparaître, et un outil
# d'une AUTRE phase (brain_update, propre à reorg) doit rester absent.
if grep -q '^ARG:mcp__brain-v42__brain_decay_status,' "$ARGS_LOG"; then
  pass "chaque phase reçoit son allowlist exacte (brain MCP joignable)"
else
  fail "allowlist de phase absente des appels claude"
  grep '^ARG:mcp__brain-v42__' "$ARGS_LOG" | head -5 | sed 's/^/    /' >&2 || true
fi
if grep -q '^ARG:[^ ]*brain_update' "$ARGS_LOG"; then
  fail "un outil hors périmètre (brain_update) a fuité dans une phase de lecture"
else
  pass "aucun outil d'une autre phase ne fuite dans scan/clean/connect/synth"
fi

# --- Assertion: permission-mode remains bypassPermissions ---
if grep -q '^ARG:bypassPermissions$' "$ARGS_LOG"; then
  pass "permission-mode bypassPermissions still present"
else
  fail "permission-mode bypassPermissions missing"
fi

# --- Capability provider boundary: strict flag parsing happens before phases ---
: > "$ARGS_LOG"
set +e
env -i HOME="$HOME" PATH="$MOCK_BIN:/usr/bin:/bin" \
  REPO_PYTHON="$REPO_ROOT/.venv/bin/python" PYTHONPATH="$REPO_ROOT" \
  MCP_HTTP_TOKEN=test-only-token \
  BRAIN_DREAM_AGENT_PROVIDER=claude \
  BRAIN_DREAM_CAPABILITY_ENFORCEMENT= \
  CLAUDE_ARGS_LOG="$ARGS_LOG" \
  "$TMP/scripts/dream.sh" test-project >"$TMP/invalid-flag.out" 2>&1
invalid_flag_rc=$?
set -e
if [[ "$invalid_flag_rc" -eq 2 ]] \
  && grep -q 'Invalid BRAIN_DREAM_CAPABILITY_ENFORCEMENT value' "$TMP/invalid-flag.out"; then
  pass "an explicitly empty capability flag fails closed"
else
  fail "empty capability flag returned $invalid_flag_rc instead of a strict parse error"
fi
if [[ ! -s "$ARGS_LOG" ]]; then
  pass "invalid capability flag is rejected before every Claude phase"
else
  fail "Claude ran despite an invalid capability flag"
fi

: > "$ARGS_LOG"
set +e
env -i HOME="$HOME" PATH="$MOCK_BIN:/usr/bin:/bin" \
  REPO_PYTHON="$REPO_ROOT/.venv/bin/python" PYTHONPATH="$REPO_ROOT" \
  MCP_HTTP_TOKEN=test-only-token \
  BRAIN_DREAM_AGENT_PROVIDER=claude \
  BRAIN_DREAM_CAPABILITY_ENFORCEMENT=true \
  CLAUDE_ARGS_LOG="$ARGS_LOG" \
  "$TMP/scripts/dream.sh" test-project >"$TMP/claude-enabled.out" 2>&1
claude_enabled_rc=$?
set -e
# Sous enforcement, claude n'est plus REFUSÉ — il est SCOPÉ. Mais l'ancienne
# propriété qui comptait vraiment survit intacte : rien ne tourne quand la
# configuration de capacité est incomplète. Ici le registre est absent, donc le
# préflight doit tuer la nuit AVANT la première phase, exactement comme le refus
# le faisait. Un fail-closed remplacé par un autre fail-closed.
if [[ "$claude_enabled_rc" -ne 0 ]] \
  && grep -q 'FAIL Claude preflight' "$TMP/claude-enabled.out"; then
  pass "un registre de capacité absent tue la nuit au préflight claude"
else
  fail "claude a démarré sans registre de capacité (rc=$claude_enabled_rc)"
  sed 's/^/    /' "$TMP/claude-enabled.out" >&2 || true
fi
if [[ ! -s "$ARGS_LOG" ]]; then
  pass "le préflight échoue avant toute phase"
else
  fail "claude a tourné malgré une configuration de capacité invalide"
fi

# --- Direct PROMOTE entry uses the same uv/project-key runner boundary ---
UV_ARGS_LOG="$TMP/uv_args.log"
: > "$UV_ARGS_LOG"
printf '[]\n' > "$TMP/promote-fixture.json"
set +e
env -i HOME="$HOME" PATH="$MOCK_BIN:/usr/bin:/bin" \
  REPO_PYTHON="$REPO_ROOT/.venv/bin/python" PYTHONPATH="$REPO_ROOT" \
  MCP_HTTP_TOKEN=test-only-token \
  BRAIN_DREAM_AGENT_PROVIDER=codex \
  BRAIN_DREAM_CAPABILITY_ENFORCEMENT=false \
  BRAIN_DREAM_PROJECT_KEY=smoke-project \
  FIXTURE="$TMP/promote-fixture.json" \
  SYNTH_LOG="$TMP/missing-synth.log" \
  UV_ARGS_LOG="$UV_ARGS_LOG" \
  "$TMP/scripts/dream/_promote_smoke.sh" >"$TMP/promote-smoke.out" 2>&1
promote_smoke_rc=$?
set -e
if [[ "$promote_smoke_rc" -eq 0 ]]; then
  pass "direct PROMOTE smoke completes through the mocked uv runner"
else
  fail "direct PROMOTE smoke exited $promote_smoke_rc"
  sed 's/^/    /' "$TMP/promote-smoke.out" >&2 || true
fi
if grep -q '^run python -m scripts.dream.codex_runner .*--project-key smoke-project' \
  "$UV_ARGS_LOG"; then
  pass "direct PROMOTE smoke passes its project key through uv run python"
else
  fail "direct PROMOTE smoke did not use uv run python with its project key"
fi

echo "==="
if (( FAIL_COUNT > 0 )); then
  echo "FAILED ($FAIL_COUNT assertion(s))" >&2
  exit 1
fi
echo "PASSED"
