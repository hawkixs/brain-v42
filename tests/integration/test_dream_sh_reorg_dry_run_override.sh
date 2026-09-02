#!/usr/bin/env bash
# Integration test for scripts/dream.sh — per-phase REORG DRY_RUN override.
#
# Validates the BRAIN_DREAM_REORG_DRY_RUN sub-flag introduced to soak the
# REORG phase in dry-run mode while the rest of the pipeline (notably
# PROMOTE) continues to run WET. This is the killswitch-as-cadence-decoupling
# pattern from learning 9a677c1a applied at the dry-run dimension.
#
# When BRAIN_DREAM_REORG_ENABLED=true and BRAIN_DREAM_REORG_DRY_RUN=true
# (with global DRY_RUN=false), the rendered REORG prompt MUST carry
# `Dry run: true` while every other phase prompt MUST carry `Dry run: false`.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DREAM_SH_SRC="$REPO_ROOT/scripts/dream.sh"
DREAM_PROMPTS_SRC="$REPO_ROOT/scripts/dream"
FAIL_COUNT=0

pass() { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; FAIL_COUNT=$((FAIL_COUNT + 1)); }

echo "=== test_dream_sh_reorg_dry_run_override.sh ==="

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/scripts"
cp "$DREAM_SH_SRC" "$TMP/scripts/dream.sh"
cp -r "$DREAM_PROMPTS_SRC" "$TMP/scripts/dream"
chmod +x "$TMP/scripts/dream.sh"

MOCK_BIN="$TMP/bin"
mkdir -p "$MOCK_BIN"
STDIN_DIR="$TMP/stdin"
mkdir -p "$STDIN_DIR"

# Mock `claude` that records the rendered prompt (stdin) for each call,
# tagged by phase. The phase name is recovered from the temporary `--model`
# arg sequence: scan/clean/connect use sonnet, synth/promote/reorg use opus.
# To distinguish, we use a per-call counter file.
cat > "$MOCK_BIN/claude" <<'MOCK_CLAUDE'
#!/usr/bin/env bash
COUNTER_FILE="$STDIN_DIR/.counter"
n=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$COUNTER_FILE"
cat > "$STDIN_DIR/call_${n}.prompt"
echo "[mock claude] phase output (call $n)"
exit 0
MOCK_CLAUDE
chmod +x "$MOCK_BIN/claude"

cat > "$MOCK_BIN/uv" <<'MOCK_UV'
#!/usr/bin/env bash
# The claude rail has gone through scripts.dream.claude_runner since 2026-08-11:
# it, and no longer dream.sh, builds the command line. The mock therefore delegates
# to the REAL runner, which then executes the stubbed `claude` from the PATH.
if [[ "${1:-} ${2:-} ${3:-} ${4:-}" == "run python -m scripts.dream.claude_runner" ]]; then
  shift 4
  exec "$REPO_PYTHON" -m scripts.dream.claude_runner "$@"
fi
exit 0
MOCK_UV
chmod +x "$MOCK_BIN/uv"

# Run dream.sh with REORG enabled + REORG dry-run override + global DRY_RUN false.
# PROMOTE stays disabled (default) so the candidate-pool path doesn't kick in.
set +e
env -i HOME="$HOME" PATH="$MOCK_BIN:/usr/bin:/bin" \
  REPO_PYTHON="$REPO_ROOT/.venv/bin/python" PYTHONPATH="$REPO_ROOT" \
  MCP_HTTP_TOKEN=test-only-token \
  BRAIN_DREAM_AGENT_PROVIDER=claude \
  STDIN_DIR="$STDIN_DIR" \
  DRY_RUN=false \
  BRAIN_DREAM_PROMOTE_ENABLED=false \
  BRAIN_DREAM_REORG_ENABLED=true \
  BRAIN_DREAM_REORG_DRY_RUN=true \
  "$TMP/scripts/dream.sh" test-project >"$TMP/run.out" 2>&1
rc=$?
set -e

if [[ "$rc" -eq 0 ]]; then
  pass "dream.sh completes successfully with mock claude"
else
  fail "dream.sh exited $rc (expected 0)"
  sed 's/^/    /' "$TMP/run.out" >&2 || true
fi

# Active phases when PROMOTE skipped + REORG enabled = scan, clean, connect, synth, reorg = 5
EXPECTED_CALLS=5
call_count=$(cat "$STDIN_DIR/.counter" 2>/dev/null || echo 0)
if [[ "$call_count" -eq "$EXPECTED_CALLS" ]]; then
  pass "claude invoked $EXPECTED_CALLS times (scan, clean, connect, synth, reorg)"
else
  fail "expected $EXPECTED_CALLS claude calls, got $call_count"
  ls -la "$STDIN_DIR/" >&2 || true
fi

# Phases run in order defined by the PHASES array: scan, clean, connect, synth,
# promote, reorg. With PROMOTE skipped, the call indices map to:
#   call_1=scan, call_2=clean, call_3=connect, call_4=synth, call_5=reorg
REORG_PROMPT="$STDIN_DIR/call_5.prompt"
SCAN_PROMPT="$STDIN_DIR/call_1.prompt"

# REORG prompt must carry Dry run: true (the override fired)
if [[ -f "$REORG_PROMPT" ]] && grep -q '^- Dry run: true$' "$REORG_PROMPT"; then
  pass "REORG prompt rendered with 'Dry run: true' (sub-flag honored)"
else
  fail "REORG prompt does NOT carry 'Dry run: true' (sub-flag did not override)"
  if [[ -f "$REORG_PROMPT" ]]; then
    grep -E '^- (Dry run|Project scope|Date):' "$REORG_PROMPT" | head -5 | sed 's/^/    /' >&2 || true
  fi
fi

# Scan prompt must keep Dry run: false (the override is REORG-only)
if [[ -f "$SCAN_PROMPT" ]] && grep -q '^- Dry run: false$' "$SCAN_PROMPT"; then
  pass "SCAN prompt kept 'Dry run: false' (override is REORG-scoped)"
else
  fail "SCAN prompt has wrong Dry run value (override leaked across phases)"
  if [[ -f "$SCAN_PROMPT" ]]; then
    grep -E '^- (Dry run|Project scope|Date):' "$SCAN_PROMPT" | head -5 | sed 's/^/    /' >&2 || true
  fi
fi

echo "==="
if (( FAIL_COUNT > 0 )); then
  echo "FAILED ($FAIL_COUNT assertion(s))" >&2
  exit 1
fi
echo "PASSED"
