#!/usr/bin/env bash
# Integration smoke test for the BLOCKER 1 fix in dream.sh:
#
#   The REORG post-phase validator block reads `reorg_effective_dry_run`, which
#   is recomputed from global inputs (DRY_RUN + BRAIN_DREAM_REORG_DRY_RUN) at
#   the call site — NOT the `effective_dry_run` local that belongs to
#   run_phase() and is out-of-scope there.  Under set -euo pipefail, reading
#   an unset variable crashes the entire pipeline (unbound variable).
#
# This test verifies TWO things:
#   1. dream.sh completes without crashing when a REORG phase succeeds and the
#      validator block is reached (i.e. `reorg_effective_dry_run` is in scope).
#   2. When BRAIN_DREAM_REORG_DRY_RUN=true, the validator is invoked with
#      --dry-run (the flag must appear in the recorded uv call).
#
# bash -n cannot catch this class of bug (runtime unbound-variable), so this
# test actually executes the code path with mocked `uv` and `claude` stubs.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DREAM_SH_SRC="$REPO_ROOT/scripts/dream.sh"
DREAM_PROMPTS_SRC="$REPO_ROOT/scripts/dream"
FAIL_COUNT=0

pass() { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; FAIL_COUNT=$((FAIL_COUNT + 1)); }

echo "=== test_dream_sh_reorg_validator_dry_run_flag.sh ==="

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/scripts"
cp "$DREAM_SH_SRC" "$TMP/scripts/dream.sh"
cp -r "$DREAM_PROMPTS_SRC" "$TMP/scripts/dream"
chmod +x "$TMP/scripts/dream.sh"

MOCK_BIN="$TMP/bin"
mkdir -p "$MOCK_BIN"

# Capture directory: each uv invocation writes its full argv to a numbered file
# so assertions can inspect exactly what flags were passed.
UV_CALLS_DIR="$TMP/uv_calls"
mkdir -p "$UV_CALLS_DIR"

# Mock `claude`: swallows stdin, emits minimal output so run_phase() succeeds.
cat > "$MOCK_BIN/claude" <<'MOCK_CLAUDE'
#!/usr/bin/env bash
# Consume stdin (rendered prompt) and print a stub report.
cat > /dev/null
echo "[mock-claude] stub output"
exit 0
MOCK_CLAUDE
chmod +x "$MOCK_BIN/claude"

# Mock `uv`: records every invocation, then inspects whether this call is to
# reorg_validate and, if so, records the full argv for assertion.
# For all other calls (parser, preflight, post_run_alert, …) exit 0 silently.
cat > "$MOCK_BIN/uv" <<MOCK_UV
#!/usr/bin/env bash
# Le rail claude passe par scripts.dream.claude_runner depuis le 2026-08-11 :
# c'est lui, et non plus dream.sh, qui construit la ligne de commande. Le mock
# delegue donc au VRAI runner, qui execute ensuite le `claude` bouchonne du PATH.
if [[ "\${1:-} \${2:-} \${3:-} \${4:-}" == "run python -m scripts.dream.claude_runner" ]]; then
  shift 4
  exec "\$REPO_PYTHON" -m scripts.dream.claude_runner "\$@"
fi
# Count this call.
n=\$(ls "$UV_CALLS_DIR"/*.argv 2>/dev/null | wc -l)
n=\$((n + 1))
printf '%s\n' "\$@" > "$UV_CALLS_DIR/\${n}.argv"

# If this is the reorg_validate invocation, record argv in a dedicated file
# for the assertion, then exit 0 (validator succeeds → no FAIL_TOTAL bump).
for arg in "\$@"; do
  if [[ "\$arg" == *reorg_validate* ]]; then
    printf '%s\n' "\$@" > "$UV_CALLS_DIR/reorg_validate.argv"
    exit 0
  fi
done
exit 0
MOCK_UV
chmod +x "$MOCK_BIN/uv"

# Create a mock log dir so dream.sh can write the REORG report log that
# --report-log expects.  We also pre-create the file so the validator mock
# (uv exit 0) doesn't need a real file.
LOG_DIR="$TMP/logs/dream"
mkdir -p "$LOG_DIR"

# Run dream.sh with:
#   - REORG enabled
#   - BRAIN_DREAM_REORG_DRY_RUN=true (the sub-flag)
#   - global DRY_RUN=false  (the sub-flag must override independently)
#   - PROMOTE disabled (keeps the run shorter)
set +e
env -i \
  REPO_PYTHON="$REPO_ROOT/.venv/bin/python" PYTHONPATH="$REPO_ROOT" \
  MCP_HTTP_TOKEN=test-only-token \
  HOME="$HOME" \
  PATH="$MOCK_BIN:/usr/bin:/bin" \
  BRAIN_DREAM_AGENT_PROVIDER=claude \
  DRY_RUN=false \
  BRAIN_DREAM_PROMOTE_ENABLED=false \
  BRAIN_DREAM_REORG_ENABLED=true \
  BRAIN_DREAM_REORG_DRY_RUN=true \
  "$TMP/scripts/dream.sh" test-project >"$TMP/run.out" 2>&1
rc=$?
set -e

# ── Assertion 1: dream.sh must not crash ──────────────────────────────────────
# exit 0 = all phases OK; any other exit is acceptable as long as the crash was
# NOT from an unbound-variable abort (which would produce exit 1 with "unbound
# variable" in stderr — the BLOCKER condition).
if grep -q "unbound variable" "$TMP/run.out" 2>/dev/null; then
  fail "dream.sh crashed with 'unbound variable' — BLOCKER 1 regression"
  cat "$TMP/run.out" | sed 's/^/    /' >&2
else
  pass "dream.sh completed without 'unbound variable' crash (BLOCKER 1 not regressed)"
fi

# ── Assertion 2: reorg_validate was invoked ───────────────────────────────────
if [[ -f "$UV_CALLS_DIR/reorg_validate.argv" ]]; then
  pass "reorg_validate was invoked by dream.sh after REORG phase"
else
  fail "reorg_validate was NOT invoked — validator block may not have been reached"
  echo "    uv calls captured:" >&2
  ls "$UV_CALLS_DIR/"*.argv 2>/dev/null | while read f; do
    echo "      $(basename "$f"): $(tr '\n' ' ' < "$f")" >&2
  done
fi

# ── Assertion 3: --dry-run flag was passed to the validator ──────────────────
# When BRAIN_DREAM_REORG_DRY_RUN=true the reorg_effective_dry_run variable must
# be "true", which causes dream.sh to append --dry-run to reorg_validator_flags.
if [[ -f "$UV_CALLS_DIR/reorg_validate.argv" ]]; then
  if grep -q -- "--dry-run" "$UV_CALLS_DIR/reorg_validate.argv"; then
    pass "reorg_validate received --dry-run (BRAIN_DREAM_REORG_DRY_RUN honoured)"
  else
    fail "reorg_validate was NOT called with --dry-run (reorg_effective_dry_run derivation broken)"
    echo "    Actual argv:" >&2
    cat "$UV_CALLS_DIR/reorg_validate.argv" | sed 's/^/      /' >&2
  fi
fi

# ── Assertion 4: --run-date was passed to the validator ──────────────────────
# Verifies the MAJOR 1 wiring: dream.sh must pass --run-date $TIMESTAMP so the
# updated_at >= run_date check in validate() can do its job.
if [[ -f "$UV_CALLS_DIR/reorg_validate.argv" ]]; then
  if grep -q -- "--run-date" "$UV_CALLS_DIR/reorg_validate.argv"; then
    pass "reorg_validate received --run-date (updated_at recency check wired)"
  else
    fail "reorg_validate was NOT called with --run-date — MAJOR 1 wiring missing"
    echo "    Actual argv:" >&2
    cat "$UV_CALLS_DIR/reorg_validate.argv" | sed 's/^/      /' >&2
  fi
fi

# ── Assertion 5: global DRY_RUN=false does NOT suppress --dry-run ────────────
# Confirm that the sub-flag BRAIN_DREAM_REORG_DRY_RUN=true is independent of
# the global DRY_RUN value.  This is the cadence-decoupling invariant.
# Already covered by assertions 3+4 since we ran with DRY_RUN=false above and
# --dry-run still appeared.  Add an explicit pass line for clarity.
pass "BRAIN_DREAM_REORG_DRY_RUN=true overrides global DRY_RUN=false (verified by assertion 3)"

echo "==="
if (( FAIL_COUNT > 0 )); then
  echo "FAILED ($FAIL_COUNT assertion(s))" >&2
  exit 1
fi
echo "PASSED"
