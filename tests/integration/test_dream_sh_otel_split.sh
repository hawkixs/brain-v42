#!/usr/bin/env bash
# Integration test for scripts/dream.sh — OTEL / report log separation.
#
# Validates that dream.sh post-processes each phase's combined claude
# stdout into two distinct files:
#   • ${TIMESTAMP}_${phase}.log       → Claude's report text only
#   • ${TIMESTAMP}_${phase}.otel.log  → OTEL JS-object blobs only
#
# Background: the real 2026-04-09 synth run produced a 156 KB phase log
# that contained ZERO lines of actual report text because OTEL events
# flooded stdout and drowned the report. Humans diagnosing a failure
# could not tell what Claude had been doing. This fix moves OTEL into a
# sibling .otel.log file and keeps .log readable.
#
# The test uses a mock `claude` that emits a realistic interleaved stream
# (report text + OTEL blob + more report + OTEL blob) and a "smart" mock
# `uv` that:
#   • dispatches `run python -m brain_v42.metrics.otel_split` to the real
#     venv Python so the splitter behaviour is exercised end-to-end;
#   • stubs every other `uv run ...` call (notably dream_parser) as a
#     no-op so the test does not require a running database.
#
# Assertions:
#   1. dream.sh exits 0 (all phases succeed under mock)
#   2. for every phase, .log file exists and contains the report text
#      but NOT the OTEL body marker
#   3. for every phase, .otel.log file exists and contains the OTEL body
#      marker but NOT the report-only sentinel
#   4. the raw combined log is cleaned up (no *.raw.log leftover)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DREAM_SH_SRC="$REPO_ROOT/scripts/dream.sh"
DREAM_PROMPTS_SRC="$REPO_ROOT/scripts/dream"
VENV_PY="$REPO_ROOT/.venv/bin/python"
FAIL_COUNT=0

pass() { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; FAIL_COUNT=$((FAIL_COUNT + 1)); }

echo "=== test_dream_sh_otel_split.sh ==="

# --- 0. Prerequisites ---
if [[ ! -x "$VENV_PY" ]]; then
  echo "SKIP: venv python not found at $VENV_PY (run 'pip install -e .[dev]')" >&2
  exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/scripts"
cp "$DREAM_SH_SRC" "$TMP/scripts/dream.sh"
cp -r "$DREAM_PROMPTS_SRC" "$TMP/scripts/dream"
chmod +x "$TMP/scripts/dream.sh"

MOCK_BIN="$TMP/bin"
mkdir -p "$MOCK_BIN"

# Mock claude: emit realistic interleaved report + OTEL.
cat > "$MOCK_BIN/claude" <<'MOCK_CLAUDE'
#!/usr/bin/env bash
cat >/dev/null 2>&1 || true
cat <<'OUT'
REPORT_SENTINEL_LINE_1
{
  resource: {
    "service.name": "claude-code",
  },
  body: "claude_code.api_request",
  attributes: {
    input_tokens: "3",
    output_tokens: "42",
  },
}
REPORT_SENTINEL_LINE_2
{
  body: "claude_code.tool_result",
  attributes: {
    tool_name: "mcp_tool",
  },
}
REPORT_SENTINEL_LINE_3
OUT
exit 0
MOCK_CLAUDE
chmod +x "$MOCK_BIN/claude"

# Smart mock `uv`: route otel_split to real python, stub everything else.
cat > "$MOCK_BIN/uv" <<MOCK_UV
#!/usr/bin/env bash
# Le rail claude passe par scripts.dream.claude_runner depuis le 2026-08-11 :
# c'est lui, et non plus dream.sh, qui construit la ligne de commande. Le mock
# delegue donc au VRAI runner, qui execute ensuite le `claude` bouchonne du PATH.
if [[ "\${1:-} \${2:-} \${3:-} \${4:-}" == "run python -m scripts.dream.claude_runner" ]]; then
  shift 4
  exec "\$REPO_PYTHON" -m scripts.dream.claude_runner "\$@"
fi
# Expected call shape: uv run python -m <module> [args...]
if [[ "\$1" == "run" && "\$2" == "python" && "\$3" == "-m" ]]; then
  mod="\$4"
  shift 4
  case "\$mod" in
    brain_v42.metrics.otel_split)
      exec "$VENV_PY" -m brain_v42.metrics.otel_split "\$@"
      ;;
    brain_v42.metrics.dream_parser)
      # Stub: dream_parser needs a live database. For this test we only
      # care that it is invoked AFTER the split.
      echo "[dream_parser mock] called for \$*"
      exit 0
      ;;
  esac
fi
exit 0
MOCK_UV
chmod +x "$MOCK_BIN/uv"

set +e
env -i HOME="$HOME" PATH="$MOCK_BIN:/usr/bin:/bin" \
  REPO_PYTHON="$REPO_ROOT/.venv/bin/python" PYTHONPATH="$REPO_ROOT" \
  MCP_HTTP_TOKEN=test-only-token \
  BRAIN_DREAM_AGENT_PROVIDER=claude \
  "$TMP/scripts/dream.sh" test-project >"$TMP/run.out" 2>&1
rc=$?
set -e

if [[ "$rc" -eq 0 ]]; then
  pass "dream.sh exits 0 with interleaved mock stdout"
else
  fail "dream.sh exited $rc (expected 0)"
  sed 's/^/    /' "$TMP/run.out" >&2 || true
fi

LOG_DIR="$TMP/logs/dream"

# --- Per-phase assertions ---
# reorg + promote excluded: their killswitches default OFF (BRAIN_DREAM_REORG_ENABLED,
# BRAIN_DREAM_PROMOTE_ENABLED) and the test runs dream.sh under `env -i` which strips
# any outer overrides. Both phases skip without producing log files. When a killswitch
# is opened in production, add the phase here and update the assertion count.
for phase in scan clean connect synth; do
  report_log=$(find "$LOG_DIR" -maxdepth 1 -name "*_${phase}.log" ! -name "*.otel.log" 2>/dev/null | head -1)
  otel_log=$(find "$LOG_DIR" -maxdepth 1 -name "*_${phase}.otel.log" 2>/dev/null | head -1)

  if [[ -n "$report_log" && -f "$report_log" ]]; then
    if grep -q REPORT_SENTINEL_LINE_ "$report_log"; then
      pass "$phase: report log contains report text"
    else
      fail "$phase: report log missing report text ($report_log)"
    fi

    if grep -q 'body: "claude_code' "$report_log"; then
      fail "$phase: report log still contains OTEL body"
    else
      pass "$phase: report log stripped of OTEL body"
    fi
  else
    fail "$phase: report log not found"
  fi

  if [[ -n "$otel_log" && -f "$otel_log" ]]; then
    if grep -q 'body: "claude_code' "$otel_log"; then
      pass "$phase: otel log contains OTEL body"
    else
      fail "$phase: otel log missing OTEL body ($otel_log)"
    fi

    if grep -q REPORT_SENTINEL_LINE_ "$otel_log"; then
      fail "$phase: otel log leaks report text"
    else
      pass "$phase: otel log isolated from report text"
    fi
  else
    fail "$phase: otel log not found"
  fi
done

# --- Raw log cleanup ---
leftover_raw=$(find "$LOG_DIR" -maxdepth 1 -name "*.raw.log" 2>/dev/null || true)
if [[ -z "$leftover_raw" ]]; then
  pass "no *.raw.log leftover after split"
else
  fail "raw combined log not cleaned up: $leftover_raw"
fi

echo "==="
if (( FAIL_COUNT > 0 )); then
  echo "FAILED ($FAIL_COUNT assertion(s))" >&2
  exit 1
fi
echo "PASSED"
