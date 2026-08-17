#!/usr/bin/env bash
# Integration contract for generated user units and EnvironmentFile precedence.
# REQUIRE_USER_SYSTEMD=1 makes the real transient user-manager probe mandatory.
# REQUIRE_SYSTEMD_ANALYZE=1 makes the isolated unit verifier mandatory.

set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=tests/integration/resolve_test_python.sh
# SOURCE_ROOT is resolved from this script at runtime.
# shellcheck disable=SC1091
source "$SOURCE_ROOT/tests/integration/resolve_test_python.sh"
FAIL_COUNT=0
TMP_ROOT="$(mktemp -d)"
FIXTURE_REPO="$TMP_ROOT/repo"
TMP_HOME="$TMP_ROOT/home"
TMP_XDG="$TMP_ROOT/xdg"
PROBE_UNITS=()

pass() { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; FAIL_COUNT=$((FAIL_COUNT + 1)); }

cleanup() {
  local unit
  for unit in "${PROBE_UNITS[@]}"; do
    systemctl --user stop "$unit" >/dev/null 2>&1 || true
    systemctl --user reset-failed "$unit" >/dev/null 2>&1 || true
    systemctl --user --runtime disable "$unit" >/dev/null 2>&1 || true
  done
  if ((${#PROBE_UNITS[@]} > 0)); then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

echo "=== test_dream_systemd_install.sh ==="

if ! command -v systemd-analyze >/dev/null; then
  if [[ "${REQUIRE_SYSTEMD_ANALYZE:-0}" == "1" ]]; then
    echo "ERROR: systemd-analyze is required" >&2
    exit 1
  fi
  echo "SKIP: systemd-analyze not available" >&2
  exit 0
fi

# Isolate every install input as well as XDG output. In particular, the fixture
# gives systemd-analyze a real ExecStart executable without relying on a worktree
# .venv symlink.
mkdir -p "$FIXTURE_REPO/deploy" "$FIXTURE_REPO/scripts" \
  "$FIXTURE_REPO/.venv/bin" "$TMP_HOME/.config/brain-v42" "$TMP_XDG"
cp -a "$SOURCE_ROOT/deploy/systemd" "$FIXTURE_REPO/deploy/"
cp "$SOURCE_ROOT/scripts/dream.sh" "$FIXTURE_REPO/scripts/dream.sh"
cp "$SOURCE_ROOT/scripts/check_mcp_http_port.py" \
  "$FIXTURE_REPO/scripts/check_mcp_http_port.py"
cp "$SOURCE_ROOT/scripts/check_graph_projector_env.py" \
  "$FIXTURE_REPO/scripts/check_graph_projector_env.py"
cp -a "$SOURCE_ROOT/scripts/dream" "$FIXTURE_REPO/scripts/"
printf '#!/bin/sh\nexit 0\n' > "$FIXTURE_REPO/.venv/bin/python"
chmod 0755 "$FIXTURE_REPO/.venv/bin/python"
touch "$FIXTURE_REPO/.env"
printf 'MCP_HTTP_TOKEN=integration-fixture-token\n' \
  > "$TMP_HOME/.config/brain-v42/mcp-token.env"
chmod 0600 "$TMP_HOME/.config/brain-v42/mcp-token.env"

INSTALL_SCRIPT="$FIXTURE_REPO/deploy/systemd/install.sh"
while IFS= read -r environment_key; do
  case "${environment_key,,}" in
    brain_dream_capability_enforcement|graph_ledger_write_enabled|graph_projector_enabled|graph_projector_neo4j_password|graph_projector_neo4j_url|graph_projector_neo4j_user|mcp_http_dream_tokens|mcp_http_host|mcp_http_port|mcp_http_token|neo4j_password|neo4j_url|neo4j_user)
      unset "$environment_key"
      ;;
  esac
done < <(compgen -e)

if HOME="$TMP_HOME" XDG_CONFIG_HOME="$TMP_XDG" \
  MCP_HTTP_HOST="127.0.0.1" MCP_HTTP_PORT="8765" \
  "$INSTALL_SCRIPT" --check-only >/dev/null 2>&1; then
  pass "install.sh --check-only verifies all managed units without publication"
else
  fail "install.sh --check-only returned non-zero"
fi
if [[ ! -e "$TMP_XDG/systemd/user" ]]; then
  pass "install.sh --check-only leaves the user unit directory absent"
else
  fail "install.sh --check-only touched the user unit directory"
fi

if HOME="$TMP_HOME" XDG_CONFIG_HOME="$TMP_XDG" MCP_HTTP_HOST="127.0.0.1" MCP_HTTP_PORT="8765" "$INSTALL_SCRIPT" --dry-run >/dev/null 2>&1; then
  pass "install.sh --dry-run exits 0"
else
  fail "install.sh --dry-run returned non-zero"
fi

USER_UNIT_DIR="$TMP_XDG/systemd/user"
DREAM_SERVICE="$USER_UNIT_DIR/brain-v42-dream.service"
DREAM_TIMER="$USER_UNIT_DIR/brain-v42-dream.timer"
AUTOMATION_SERVICE="$USER_UNIT_DIR/brain-v42-automation.service"
MCP_HTTP_SERVICE="$USER_UNIT_DIR/brain-mcp-http.service"

for file in "$DREAM_SERVICE" "$DREAM_TIMER" "$AUTOMATION_SERVICE" "$MCP_HTTP_SERVICE"; do
  if [[ -f "$file" ]]; then
    pass "$(basename "$file") generated"
  else
    fail "$(basename "$file") was not generated"
  fi
done

for service in "$DREAM_SERVICE" "$AUTOMATION_SERVICE"; do
  [[ -f "$service" ]] || continue
  if grep -q "__REPO_ROOT__" "$service"; then
    fail "$(basename "$service") still contains __REPO_ROOT__"
  else
    pass "$(basename "$service") has no unresolved repo placeholder"
  fi
  if grep -qF "WorkingDirectory=$FIXTURE_REPO" "$service"; then
    pass "$(basename "$service") embeds the fixture root"
  else
    fail "$(basename "$service") embeds the wrong WorkingDirectory"
  fi
  if systemd-analyze --user verify "$service"; then
    pass "systemd-analyze verifies $(basename "$service")"
  else
    fail "systemd-analyze rejects $(basename "$service")"
  fi
done

if [[ -f "$DREAM_TIMER" ]] \
  && systemd-analyze --user verify "$DREAM_TIMER" \
  && grep -qE '^OnCalendar=\*-\*-\* 06:00:00$' "$DREAM_TIMER"; then
  pass "Dream timer verifies and schedules 06:00"
else
  fail "Dream timer contract failed"
fi

if [[ -f "$MCP_HTTP_SERVICE" ]]; then
  BASE_ENV_LINE="$(
    grep -nF "EnvironmentFile=$FIXTURE_REPO/.env" "$MCP_HTTP_SERVICE" | cut -d: -f1 || true
  )"
  TOKEN_ENV_LINE="$(
    grep -nF 'EnvironmentFile=%h/.config/brain-v42/mcp-token.env' \
      "$MCP_HTTP_SERVICE" | cut -d: -f1 || true
  )"
  if [[ -n "$BASE_ENV_LINE" && -n "$TOKEN_ENV_LINE" ]] \
    && ((BASE_ENV_LINE < TOKEN_ENV_LINE)); then
    pass "HTTP reads the shared token file after the repo environment"
  else
    fail "HTTP shared token EnvironmentFile is missing or ordered before repo .env"
  fi
fi

if [[ -f "$DREAM_SERVICE" ]]; then
  if grep -qF 'EnvironmentFile=%h/.config/brain-v42/mcp-token.env' "$DREAM_SERVICE"; then
    pass "Dream token EnvironmentFile is required"
  else
    fail "Dream token EnvironmentFile is missing"
  fi
  if grep -q '^TimeoutStartSec=10800$' "$DREAM_SERVICE"; then
    pass "Dream startup cap is 10800 seconds"
  else
    fail "Dream startup cap changed"
  fi
  if grep -q '^UMask=0077$' "$DREAM_SERVICE"; then
    pass "Dream logs retain private umask"
  else
    fail "Dream umask changed"
  fi
  # The doubled dollars must remain literal until systemd launches Bash.
  # shellcheck disable=SC2016
  if grep -q '^ExecStartPre=.*BRAIN_DREAM_MCP_URL.*\/health' "$DREAM_SERVICE" \
    && grep -qF '$${BRAIN_DREAM_MCP_URL:-http://127.0.0.1:8765/mcp}' "$DREAM_SERVICE" \
    && grep -qF '$${url%%/mcp}/health' "$DREAM_SERVICE" \
    && grep -qF 'curl -fsS -m 1 "$$health"' "$DREAM_SERVICE" \
    && grep -qF 'Brain MCP readiness failed: $$health' "$DREAM_SERVICE"; then
    pass "Dream readiness expansions remain deferred to Bash"
  else
    fail "Dream readiness expansions are consumed by systemd"
  fi
  if grep -qF \
    "ExecStart=/bin/bash -lc '$FIXTURE_REPO/scripts/dream.sh brain-v42'" \
    "$DREAM_SERVICE"; then
    pass "Dream login-shell ExecStart is preserved"
  else
    fail "Dream login-shell ExecStart changed"
  fi
fi

# Reproduce the generated unit's login-shell path with a minimal environment.
# The uv shim delegates only the capability preflight and phase runner to the
# real project Python; every DB/network/maintenance module is a deterministic
# no-op. The Codex shim emits a valid local JSONL turn and records its scrubbed
# child environment.
PROJECT_PYTHON="$(resolve_test_python "$SOURCE_ROOT")"
SMOKE_BIN="$TMP_ROOT/smoke-bin"
SMOKE_HOME="$TMP_ROOT/smoke-home"
SMOKE_RUNTIME="$TMP_ROOT/smoke-runtime"
SMOKE_STATE="$TMP_ROOT/smoke-state"
SMOKE_UV_LOG="$TMP_ROOT/smoke-uv.log"
mkdir -p "$SMOKE_BIN" "$SMOKE_HOME" "$SMOKE_RUNTIME" "$SMOKE_STATE"

cat > "$SMOKE_BIN/uv" <<'MOCK_UV'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$BRAIN_TEST_UV_LOG"
if [[ "${1:-} ${2:-} ${3:-} ${4:-}" == \
  "run python -m scripts.dream.codex_runner" ]]; then
  shift 2
  PYTHONPATH="$BRAIN_TEST_FIXTURE_REPO:$BRAIN_TEST_SOURCE_ROOT/src" \
    exec "$BRAIN_TEST_PYTHON" "$@"
fi
if [[ "$*" == *"scripts.dream.dream_preflight"* ]]; then
  echo "SKIP systemd-like capability smoke"
fi
exit 0
MOCK_UV
chmod 0755 "$SMOKE_BIN/uv"

cat > "$SMOKE_BIN/codex" <<'MOCK_CODEX'
#!/usr/bin/env bash
if [[ "${1:-}" == "login" && "${2:-}" == "status" ]]; then
  echo "Logged in using ChatGPT"
  exit 0
fi
printf '%s|%s|%s\n' \
  "$MCP_HTTP_TOKEN" \
  "${MCP_HTTP_DREAM_TOKENS-unset}" \
  "${TOP_SECRET-unset}" >> "$XDG_STATE_HOME/codex-child-env.log"
report_log=""
while (($#)); do
  if [[ "$1" == "--output-last-message" ]]; then
    report_log="$2"
    shift 2
  else
    shift
  fi
done
cat > "$XDG_STATE_HOME/last-codex-prompt.log"
printf '%s\n' \
  '=== PROMOTE REPORT ===' \
  '{"dry_run":true,"target_type":"none","reason":"local capability smoke"}' \
  '=== END ===' > "$report_log"
printf '%s\n' \
  '{"type":"item.completed","item":{"type":"mcp_tool_call","server":"brain-v42","status":"completed","error":null}}' \
  '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":2,"output_tokens":3}}'
MOCK_CODEX
chmod 0755 "$SMOKE_BIN/codex"

printf 'export PATH=%q\n' "$SMOKE_BIN:/usr/bin:/bin" > "$SMOKE_HOME/bash-env"
: > "$SMOKE_UV_LOG"
CAPABILITY_REGISTRY='{"brain-v42:scan":{"active":"scan-active-token","accepted":["scan-accepted-token"]},"brain-v42:clean":{"active":"clean-active-token","accepted":[]},"brain-v42:connect":{"active":"connect-active-token","accepted":[]},"brain-v42:synth":{"active":"synth-active-token","accepted":[]},"brain-v42:promote":{"active":"promote-active-token","accepted":[]},"brain-v42:reorg":{"active":"reorg-active-token","accepted":[]}}'

set +e
(
  cd "$FIXTURE_REPO"
  env -i \
    HOME="$SMOKE_HOME" \
    PATH="$SMOKE_BIN:/usr/bin:/bin" \
    BASH_ENV="$SMOKE_HOME/bash-env" \
    LANG=C.UTF-8 \
    XDG_RUNTIME_DIR="$SMOKE_RUNTIME" \
    XDG_STATE_HOME="$SMOKE_STATE" \
    BRAIN_DREAM_AGENT_PROVIDER=codex \
    BRAIN_DREAM_CAPABILITY_ENFORCEMENT=true \
    BRAIN_DREAM_CODEX_BIN="$SMOKE_BIN/codex" \
    BRAIN_DREAM_PROMOTE_ENABLED=false \
    BRAIN_DREAM_REORG_ENABLED=false \
    BRAIN_TEST_PYTHON="$PROJECT_PYTHON" \
    BRAIN_TEST_FIXTURE_REPO="$FIXTURE_REPO" \
    BRAIN_TEST_SOURCE_ROOT="$SOURCE_ROOT" \
    BRAIN_TEST_UV_LOG="$SMOKE_UV_LOG" \
    MCP_HTTP_TOKEN=admin-token \
    MCP_HTTP_DREAM_TOKENS="$CAPABILITY_REGISTRY" \
    TOP_SECRET=must-not-reach-codex \
    /bin/bash -lc "'$FIXTURE_REPO/scripts/dream.sh' brain-v42"
) > "$TMP_ROOT/systemd-like-smoke.out" 2>&1
systemd_like_rc=$?
set -e

if [[ "$systemd_like_rc" -eq 0 ]]; then
  pass "minimal login-shell Dream path completes with local runner mocks"
else
  fail "minimal login-shell Dream path exited $systemd_like_rc"
  sed 's/^/    /' "$TMP_ROOT/systemd-like-smoke.out" >&2 || true
fi
if grep -q '^run python -m scripts.dream.codex_runner --preflight-capabilities --project-key brain-v42$' \
  "$SMOKE_UV_LOG" \
  && [[ "$(grep -c '^run python -m scripts.dream.codex_runner --phase .*--project-key brain-v42 ' "$SMOKE_UV_LOG" || true)" -eq 3 ]]; then
  pass "timer-like path imports the runner through uv and scopes every active phase"
else
  fail "timer-like path did not preflight and run three scoped phases through uv"
fi

CHILD_ENV_LOG="$SMOKE_STATE/codex-child-env.log"
if [[ -f "$CHILD_ENV_LOG" ]] \
  && grep -q '^scan-active-token|unset|unset$' "$CHILD_ENV_LOG" \
  && grep -q '^clean-active-token|unset|unset$' "$CHILD_ENV_LOG" \
  && grep -q '^connect-active-token|unset|unset$' "$CHILD_ENV_LOG" \
  && ! grep -Eq 'admin-token|accepted-token|must-not-reach-codex|MCP_HTTP_DREAM_TOKENS' \
    "$CHILD_ENV_LOG"; then
  pass "runner children receive only active tokens and no parent secrets"
else
  fail "runner child environment was not capability-isolated"
  [[ -f "$CHILD_ENV_LOG" ]] && sed 's/^/    /' "$CHILD_ENV_LOG" >&2
fi

# Exercise the second direct runner entry with real capability parsing and the
# same fake Codex process. Use a known alias at the shell boundary: both the
# prompt and runner CLI must see only its canonical project key.
: > "$SMOKE_UV_LOG"
printf '[]\n' > "$TMP_ROOT/promote-fixture.json"
PROMOTE_OUT_DIR="$TMP_ROOT/promote-out"
set +e
(
  cd "$FIXTURE_REPO"
  env -i \
    HOME="$SMOKE_HOME" \
    PATH="$SMOKE_BIN:/usr/bin:/bin" \
    BASH_ENV="$SMOKE_HOME/bash-env" \
    LANG=C.UTF-8 \
    XDG_STATE_HOME="$SMOKE_STATE" \
    BRAIN_DREAM_AGENT_PROVIDER=codex \
    BRAIN_DREAM_CAPABILITY_ENFORCEMENT=true \
    BRAIN_DREAM_CODEX_BIN="$SMOKE_BIN/codex" \
    BRAIN_DREAM_PROJECT_KEY=brain_v42 \
    BRAIN_TEST_PYTHON="$PROJECT_PYTHON" \
    BRAIN_TEST_FIXTURE_REPO="$FIXTURE_REPO" \
    BRAIN_TEST_SOURCE_ROOT="$SOURCE_ROOT" \
    BRAIN_TEST_UV_LOG="$SMOKE_UV_LOG" \
    FIXTURE="$TMP_ROOT/promote-fixture.json" \
    SYNTH_LOG="$TMP_ROOT/missing-synth.log" \
    OUT_DIR="$PROMOTE_OUT_DIR" \
    MCP_HTTP_TOKEN=admin-token \
    MCP_HTTP_DREAM_TOKENS="$CAPABILITY_REGISTRY" \
    TOP_SECRET=must-not-reach-codex \
    "$FIXTURE_REPO/scripts/dream/_promote_smoke.sh"
) > "$TMP_ROOT/promote-real-runner-smoke.out" 2>&1
promote_real_rc=$?
set -e

if [[ "$promote_real_rc" -eq 0 ]] \
  && [[ -s "$PROMOTE_OUT_DIR/iter_1.log" ]]; then
  pass "direct PROMOTE smoke completes through the real capability runner"
else
  fail "direct PROMOTE real-runner smoke exited $promote_real_rc"
  sed 's/^/    /' "$TMP_ROOT/promote-real-runner-smoke.out" >&2 || true
fi
if grep -q '^run python -m scripts.dream.codex_runner --preflight-capabilities --project-key brain-v42$' \
  "$SMOKE_UV_LOG" \
  && grep -q '^run python -m scripts.dream.codex_runner --phase promote --project-key brain-v42 ' \
    "$SMOKE_UV_LOG"; then
  pass "direct PROMOTE canonicalizes its project before preflight and phase execution"
else
  fail "direct PROMOTE passed a non-canonical project to the capability runner"
fi
if grep -q 'Project scope: brain-v42 ' "$SMOKE_STATE/last-codex-prompt.log" \
  && ! grep -q 'Project scope: brain_v42 ' "$SMOKE_STATE/last-codex-prompt.log"; then
  pass "direct PROMOTE renders only the canonical project key"
else
  fail "direct PROMOTE rendered a non-canonical project key"
fi
if grep -q '^promote-active-token|unset|unset$' "$CHILD_ENV_LOG" \
  && ! grep -Eq 'admin-token|accepted-token|must-not-reach-codex|MCP_HTTP_DREAM_TOKENS' \
    "$CHILD_ENV_LOG"; then
  pass "direct PROMOTE child receives only its active capability token"
else
  fail "direct PROMOTE child environment was not capability-isolated"
fi

if [[ -f "$AUTOMATION_SERVICE" ]]; then
  if grep -qF \
    "ExecStart=$FIXTURE_REPO/.venv/bin/python -m brain_v42.automation" \
    "$AUTOMATION_SERVICE"; then
    pass "automation uses the direct Python entrypoint"
  else
    fail "automation ExecStart is not the direct Python entrypoint"
  fi
  if grep -qE '^(Requires|Wants|PartOf|BindsTo|Conflicts)=.*brain-metrics' \
    "$AUTOMATION_SERVICE"; then
    fail "automation has a forbidden metrics lifecycle relation"
  else
    pass "automation has no metrics lifecycle relation"
  fi
fi

# A real user manager proves the relevant systemd rule: the last
# EnvironmentFile overrides the same key from the base file. This probe is the
# sole manager-dependent assertion and is always cleaned up.
if systemctl --user show-environment >/dev/null 2>&1; then
  BASE_ENV="$TMP_ROOT/base.env"
  LATE_ENV="$TMP_ROOT/late.env"
  READINESS_DIR="$TMP_ROOT/readiness-probe"
  mkdir -p "$READINESS_DIR"
  printf 'ok\n' > "$READINESS_DIR/health"
  printf 'METRICS_LEGACY_AUTOMATION_ENABLED=true\n' > "$BASE_ENV"
  printf 'METRICS_LEGACY_AUTOMATION_ENABLED=false\n' > "$LATE_ENV"
  chmod 0600 "$BASE_ENV" "$LATE_ENV"
  PRECEDENCE_UNIT="brain-v42-env-precedence-$BASHPID.service"
  PRECEDENCE_FILE="$TMP_ROOT/$PRECEDENCE_UNIT"
  EXEC_START_PRE="$(grep -m1 '^ExecStartPre=' "$DREAM_SERVICE")"
  PROBE_UNITS+=("$PRECEDENCE_UNIT")
  {
    printf '[Service]\nType=simple\n'
    printf 'Environment=BRAIN_DREAM_MCP_URL=file://%s/mcp\n' "$READINESS_DIR"
    printf 'EnvironmentFile=%s\n' "$BASE_ENV"
    printf 'EnvironmentFile=%s\n' "$LATE_ENV"
    printf '%s\n' "$EXEC_START_PRE"
    printf 'ExecStart=/bin/sleep 30\n'
    printf 'TimeoutStartSec=2\n'
  } > "$PRECEDENCE_FILE"
  systemctl --user --runtime link "$PRECEDENCE_FILE" >/dev/null
  systemctl --user daemon-reload
  if systemctl --user start "$PRECEDENCE_UNIT"; then
    MAIN_PID="$(systemctl --user show "$PRECEDENCE_UNIT" -p MainPID --value)"
    EFFECTIVE_FLAG="$(
      tr '\0' '\n' < "/proc/$MAIN_PID/environ" \
        | grep -E '^METRICS_LEGACY_AUTOMATION_ENABLED=' || true
    )"
    if [[ "$EFFECTIVE_FLAG" == 'METRICS_LEGACY_AUTOMATION_ENABLED=false' ]]; then
      pass "readiness gate works and late EnvironmentFile overrides true with false"
    else
      fail "late EnvironmentFile precedence is not false: $EFFECTIVE_FLAG"
    fi
  else
    fail "precedence probe service did not start"
  fi
else
  if [[ "${REQUIRE_USER_SYSTEMD:-0}" == "1" ]]; then
    echo "ERROR: user systemd manager is required for precedence probe" >&2
    exit 1
  fi
  echo "SKIP: user systemd manager unavailable for precedence probe" >&2
fi

echo "==="
if ((FAIL_COUNT > 0)); then
  echo "FAILED ($FAIL_COUNT assertion(s))" >&2
  exit 1
fi
echo "PASSED"
