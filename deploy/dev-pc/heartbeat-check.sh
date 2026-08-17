#!/usr/bin/env bash
#
# heartbeat-check.sh — thin GPU-liveness heartbeat for the dev-pc embedding.
#
# Ships WITH the cutover (judge N1/H2), NOT as a follow-up: it is the ONLY
# detector of a silent unattended headless-boot failure — the exact 3am fault
# this whole migration exists to fix. A boot-task that fails quietly (password
# rotated, GPU-PV broke at session 0, qodo fell back to CPU) must PAGE, not go
# unnoticed.
#
# It curls GET / — NOT /healthz. /healthz is supervisor liveness only and
# carries NO device field, so it would stay green even if qodo silently ran on
# CPU. GET / proxies to qodo and reports device=="cuda"|"cpu" — the real signal.
#
# This is a thin wrapper over the same assertion core as validate-headless.sh
# (a subset: liveness + device==cuda; no latency/dim timing — the heartbeat is
# meant to be cheap and frequent). On any failure it prints a single ALERT:
# line and exits non-zero so cron/monitoring can route it.
#
# Endpoint contract (services/embedding_supervisor/main.py), over :8003:
#   GET /  -> 200 JSON {"device":"cuda"|"cpu","cuda_available":bool,...}
#
# Exit 0  => GET / == 200 AND device==cuda AND cuda_available==true.
# Exit 1  => anything else; an "ALERT: ..." line is printed to stderr.
#
# Target is EMBED_URL (default http://192.168.1.11:8003) so it can be pointed
# at a stub in tests, exactly like validate-headless.sh.
#
# ── pc-serveur cron (install WITH cutover; op-seq step 7) ───────────────
# This MUST run on pc-serveur (192.168.1.12) as hawixs — NOT on the dev-pc. A
# dead WSL on the dev-pc would also kill a dev-pc-hosted check, so the heartbeat
# has to live on a separate host to detect that exact failure.
#
# Edit hawixs's crontab on pc-serveur — `crontab -e` — and add the lines below.
# It runs every 5 minutes. The 'OK' line (stdout) is appended to the log; the
# ALERT line (stderr) is NOT redirected, so on a non-zero exit cron captures it
# and mails it to MAILTO. Adjust the repo path to wherever this checkout lives.
#
#   MAILTO="ops@example.invalid"   # set to a real address; pc-serveur needs a working MTA
#   */5 * * * * /home/hawixs/hawkixs_infra/git_repo/brain_v42/deploy/dev-pc/heartbeat-check.sh >> /var/log/brain-embedding-heartbeat.log
#
# IMPORTANT: do NOT append `2>&1` — that swallows stderr so the ALERT never
# reaches cron's mailer and MAILTO never fires (the bug this comment fixes).
# A working MTA (sendmail/postfix/etc.) AND a real MAILTO must be configured on
# pc-serveur, or no page is delivered no matter how loud the ALERT.
#
# (red-monitor / red-alerts integration is a later enrichment — this cron line
#  is the baseline that ships now.)

set -euo pipefail

EMBED_URL="${EMBED_URL:-http://192.168.1.11:8003}"
CURL_CONNECT_TIMEOUT_SEC="${CURL_CONNECT_TIMEOUT_SEC:-5}"

# Strip any trailing slash so "${EMBED_URL}/" never doubles up.
EMBED_URL="${EMBED_URL%/}"

alert() {
  # $1 = human-readable reason. Single greppable ALERT line + non-zero exit.
  echo "ALERT: brain-embedding heartbeat FAILED ($EMBED_URL) — $1" >&2
  exit 1
}

# GET / — proxies to qodo, the only call that reveals device=cuda vs cpu.
# Capture the body and the HTTP status in one curl so we never desync them.
tmp_body="$(mktemp)"
# curl already prints '000' on connect failure; capture rc and set '000'
# explicitly (appending '|| echo 000' would produce '000000', not '000').
http_code="$(curl -s -o "$tmp_body" -w '%{http_code}' \
  --connect-timeout "$CURL_CONNECT_TIMEOUT_SEC" \
  "${EMBED_URL}/" 2>/dev/null)" || http_code="000"
root_body="$(cat "$tmp_body")"
rm -f "$tmp_body"

if [[ "$http_code" != "200" ]]; then
  alert "GET / returned HTTP $http_code (expected 200) — supervisor/qodo down or unreachable"
fi

device="$(printf '%s' "$root_body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("__PARSE_ERROR__"); sys.exit(0)
dev = str(d.get("device", "")).lower()
cu = d.get("cuda_available")
print("{}|{}".format(dev, "true" if cu is True else "false"))
' 2>/dev/null || echo '__PARSE_ERROR__')"

if [[ "$device" == "__PARSE_ERROR__" ]]; then
  alert "GET / body is not valid JSON; cannot read device. Body: ${root_body:0:200}"
fi

dev_field="${device%%|*}"
cuda_field="${device##*|}"
if [[ "$dev_field" != "cuda" || "$cuda_field" != "true" ]]; then
  alert "device=${dev_field} cuda_available=${cuda_field} (need device=cuda + cuda_available=true) — silent CPU fallback or GPU lost"
fi

echo "OK: brain-embedding heartbeat — GET / 200, device=cuda ($EMBED_URL)"
exit 0
