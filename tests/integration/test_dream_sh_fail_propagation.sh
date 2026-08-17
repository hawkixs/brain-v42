#!/usr/bin/env bash
# Integration test for scripts/dream.sh — phase failure propagation.
#
# Validates that dream.sh exits non-zero when any phase times out or fails,
# so systemd marks the service as failed instead of green-lighting a partial
# run. Prior to this fix, run_phase absorbed TIMEOUT/FAIL inside an if/else
# and the service was silently marked SUCCESS (see 2026-04-09 production run
# where the synth phase timed out but systemd reported the service finished
# successfully).
#
# The test uses a mock `claude` binary (configurable exit code via
# CLAUDE_MOCK_EXIT) and a mock `uv` no-op, runs dream.sh against a temp copy
# of scripts/ so real logs are untouched, and asserts:
#   1. exit == 0 when every phase succeeds (claude exits 0)
#   2. exit != 0 when every phase times out (claude exits 124)
#   3. exit != 0 when every phase hard-fails (claude exits 1)
#   4. main log contains a TIMEOUT marker when phases timed out
#   5. main log contains a summary line listing how many phases failed

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DREAM_SH_SRC="$REPO_ROOT/scripts/dream.sh"
DREAM_PROMPTS_SRC="$REPO_ROOT/scripts/dream"
FAIL_COUNT=0

pass() { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; FAIL_COUNT=$((FAIL_COUNT + 1)); }

echo "=== test_dream_sh_fail_propagation.sh ==="

# --- 0. Prerequisites ---
if [[ ! -f "$DREAM_SH_SRC" ]]; then
  fail "dream.sh missing at $DREAM_SH_SRC"
  exit 1
fi
if [[ ! -d "$DREAM_PROMPTS_SRC" ]]; then
  fail "dream prompts dir missing at $DREAM_PROMPTS_SRC"
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- 1. Isolated copy of scripts/ so LOG_DIR resolves into the tmp tree ---
mkdir -p "$TMP/scripts"
cp "$DREAM_SH_SRC" "$TMP/scripts/dream.sh"
cp -r "$DREAM_PROMPTS_SRC" "$TMP/scripts/dream"
chmod +x "$TMP/scripts/dream.sh"

# --- 2. Mock `claude` and `uv` in a dedicated bin dir prepended to PATH ---
MOCK_BIN="$TMP/bin"
mkdir -p "$MOCK_BIN"

cat > "$MOCK_BIN/claude" <<'MOCK_CLAUDE'
#!/usr/bin/env bash
# Drain stdin so upstream `echo "$prompt" |` does not SIGPIPE.
cat >/dev/null 2>&1 || true
echo "[mock claude] phase output"
exit "${CLAUDE_MOCK_EXIT:-0}"
MOCK_CLAUDE
chmod +x "$MOCK_BIN/claude"

cat > "$MOCK_BIN/uv" <<'MOCK_UV'
#!/usr/bin/env bash
# Le rail claude passe par scripts.dream.claude_runner depuis le 2026-08-11 :
# c'est lui, et non plus dream.sh, qui construit la ligne de commande. Le mock
# delegue donc au VRAI runner, qui execute ensuite le `claude` bouchonne du PATH.
if [[ "${1:-} ${2:-} ${3:-} ${4:-}" == "run python -m scripts.dream.claude_runner" ]]; then
  shift 4
  exec "$REPO_PYTHON" -m scripts.dream.claude_runner "$@"
fi
# No-op stub so dream.sh's dream_parser invocation does not require the
# real Python environment.
exit 0
MOCK_UV
chmod +x "$MOCK_BIN/uv"

run_dream() {
  # Usage: run_dream <CLAUDE_MOCK_EXIT>
  local mock_exit="$1"
  rm -rf "$TMP/logs"
  set +e
  env -i HOME="$HOME" PATH="$MOCK_BIN:/usr/bin:/bin" \
    REPO_PYTHON="$REPO_ROOT/.venv/bin/python" PYTHONPATH="$REPO_ROOT" \
    MCP_HTTP_TOKEN=test-only-token \
    BRAIN_DREAM_AGENT_PROVIDER=claude \
    CLAUDE_MOCK_EXIT="$mock_exit" \
    "$TMP/scripts/dream.sh" test-project >"$TMP/run.out" 2>&1
  local rc=$?
  set -e
  echo "$rc"
}

# --- 3. Scenario: all phases succeed → exit 0 ---
rc=$(run_dream 0)
if [[ "$rc" -eq 0 ]]; then
  pass "dream.sh exits 0 when all phases succeed"
else
  fail "dream.sh exited $rc when all phases succeeded (expected 0)"
  sed 's/^/    /' "$TMP/run.out" >&2 || true
fi

# --- 4. Scenario: all phases time out → exit != 0 ---
rc=$(run_dream 124)
if [[ "$rc" -ne 0 ]]; then
  pass "dream.sh exits non-zero when phases time out (exit=$rc)"
else
  fail "dream.sh exited 0 when phases timed out — failure not propagated"
fi

# Verify log records TIMEOUT (at least one phase marker)
main_log="$(find "$TMP/logs/dream" -maxdepth 1 -name '*.log' -not -name '*_*.log' 2>/dev/null | head -1)"
if [[ -n "$main_log" ]] && grep -q "TIMEOUT" "$main_log"; then
  pass "main log records TIMEOUT marker"
else
  fail "main log does not record TIMEOUT marker"
  [[ -n "$main_log" ]] && sed 's/^/    /' "$main_log" >&2 || true
fi

# Verify there is a summary line that mentions a failure count
if [[ -n "$main_log" ]] && grep -qiE "(failed|timed out|[0-9]+/[0-9]+ phases?)" "$main_log"; then
  pass "main log contains a phase summary"
else
  fail "main log does not contain a phase summary"
fi

# --- 5. Scenario: all phases hard-fail → exit != 0 ---
rc=$(run_dream 1)
if [[ "$rc" -ne 0 ]]; then
  pass "dream.sh exits non-zero when phases FAIL (exit=$rc)"
else
  fail "dream.sh exited 0 when phases hard-failed — failure not propagated"
fi

echo "==="
if (( FAIL_COUNT > 0 )); then
  echo "FAILED ($FAIL_COUNT assertion(s))" >&2
  exit 1
fi
echo "PASSED"
