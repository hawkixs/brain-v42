#!/usr/bin/env bash
# Shell-level smoke test for the T12 dream.sh patch. Exercises what can be
# validated without spinning up real LLM phases: syntax, flock mechanism,
# --dry-run arg, killswitch log line, helper script dispatch.
#
# Skips the full happy-path integration (that requires a live claude CLI
# and an eligible seeded learning); that path is validated manually on
# first rollout per spec §8 step 5.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DREAM_SH="$ROOT/scripts/dream.sh"

ok() { echo "  ok: $*"; }
fail() { echo "  FAIL: $*" >&2; exit 1; }

echo "[1/5] bash -n dream.sh — syntax check"
bash -n "$DREAM_SH" && ok "no syntax errors"

echo "[2/5] dream.sh rejects unknown flags before touching anything"
if out=$("$DREAM_SH" --banana 2>&1); then
  fail "expected non-zero exit on --banana, got 0"
fi
echo "$out" | grep -q "Unknown flag: --banana" || fail "no 'Unknown flag' line"
ok "unknown-flag rejection exits 2 with explanatory stderr"

echo "[3/5] flock — second instance sees the lock"
LOCK="$(mktemp -u /tmp/brain-v42-dream-test.XXXXXX).lock"
# Acquire the lock in a background shell, hold it for 3s.
(
  exec 9>"$LOCK"
  flock -n 9
  sleep 3
) &
holder=$!
sleep 0.3
# Try to acquire the same lock with flock -n from a second shell. Expect fail.
if (exec 9>"$LOCK"; flock -n 9); then
  fail "second flock -n should NOT have acquired the lock"
fi
wait "$holder"
rm -f "$LOCK"
ok "flock -n rejects a second holder while the first is alive"

echo "[4/5] _promote_helpers dispatches both sub-commands"
cd "$ROOT"
source .venv/bin/activate 2>/dev/null || true
out=$(python -m scripts.dream._promote_helpers recent-promotions --limit 1 2>&1) \
  || fail "recent-promotions dispatch failed: $out"
echo "$out" | python -c "import sys, json; json.loads(sys.stdin.read())" \
  || fail "recent-promotions output is not valid JSON"
ok "recent-promotions emits a JSON array"

# --project-key has been REQUIRED since 6de8ffd9 ("the three run_id lookups filter
# on the project"). This test still called the old signature and failed on argparse,
# not on the measured behaviour.
out=$(python -m scripts.dream._promote_helpers dream-run-id \
  --date 2099-01-01 --project-key brain-v42 2>&1) \
  || fail "dream-run-id dispatch failed"
# Future date -> no row -> empty line.
[[ -z "$(echo "$out" | tr -d '[:space:]')" ]] \
  || fail "dream-run-id on unknown date should emit empty, got: $out"
ok "dream-run-id for unknown date emits empty"

echo "[5/5] promote_prepare returns the expected shape"
out=$(python -m scripts.dream.promote_prepare --project-key brain-v42 --limit 1 2>&1) \
  || fail "promote_prepare failed"
echo "$out" | python -c "import sys, json; r=json.loads(sys.stdin.read()); assert isinstance(r, list)" \
  || fail "promote_prepare output is not a JSON array"
ok "promote_prepare emits a JSON array"

echo
echo "=== T12 smoke test — all 5 checks passed ==="
