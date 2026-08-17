#!/usr/bin/env bash
#
# validate-headless.sh — the make-or-break headless-GPU validation gate.
#
# Run from pc-serveur AFTER a no-login headless reboot of the dev-pc. This is
# the gate that proves the embedding came up on the GPU — NOT a silent CPU
# fallback (judge C1) — and within the agreed cold-start bound (judge M1).
#
# A 1536-float vector ALONE is NOT proof of GPU: qodo silently falls back to
# CPU and still returns a 1536-d vector. So the gate asserts, over :8003:
#   1. GET /healthz       -> HTTP 200 (supervisor liveness; carries NO device).
#   2. GET /              -> proxied to qodo (WAKES it). JSON must report
#                            device=="cuda" AND cuda_available==true. The
#                            operator stops qodo BEFORE running this gate, so
#                            THIS call pays the real cold start (probe -> qodo
#                            start). A cold-start stopwatch is started just
#                            before this GET /.
#   3. POST /embed        -> outer length 1, inner length 1536.
#        - The cold-start bound FIRST_EMBED_CEILING_SEC is enforced over the
#          WHOLE (GET / + first /embed) window — because the cold cost lives in
#          the GET / wake, not the /embed itself.
#        - A second, WARM /embed is TIMED against WARM_LATENCY_CEILING_SEC.
#   On any 503 from GET / or /embed it prints supervisor /status (state,
#   gpu_free_mib) for diagnosis (gpu_busy vs container_unavailable).
#
# Prints exactly one terminal line:  HEADLESS-GPU GATE: PASS  (exit 0)
#                              or:   HEADLESS-GPU GATE: FAIL — <reason>  (exit 1)
#
# JSON is parsed with python3 (present on pc-serveur AND in WSL) — jq is NOT
# assumed. The target is EMBED_URL (default http://192.168.1.11:8003) so the
# pytest stub server can point the gate at itself.
#
# Usage:
#   ./validate-headless.sh
#   EMBED_URL=http://192.168.1.11:8003 ./validate-headless.sh
#
# Tunables (env; provisional bounds — measure on the box, then freeze):
#   EMBED_URL                 default http://192.168.1.11:8003
#   FIRST_EMBED_CEILING_SEC   default 120  (GET /+first /embed cold-start bound, M1)
#   WARM_LATENCY_CEILING_SEC  default 10   (warm /embed latency ceiling)
#   CURL_CONNECT_TIMEOUT_SEC  default 5    (per-call connect timeout)

set -euo pipefail

EMBED_URL="${EMBED_URL:-http://192.168.1.11:8003}"
FIRST_EMBED_CEILING_SEC="${FIRST_EMBED_CEILING_SEC:-120}"
WARM_LATENCY_CEILING_SEC="${WARM_LATENCY_CEILING_SEC:-10}"
CURL_CONNECT_TIMEOUT_SEC="${CURL_CONNECT_TIMEOUT_SEC:-5}"

# Strip any trailing slash so "${EMBED_URL}/healthz" never doubles up.
EMBED_URL="${EMBED_URL%/}"

log() { echo "[gate] $*"; }

fail() {
  # $1 = human-readable reason.
  echo "HEADLESS-GPU GATE: FAIL — $1" >&2
  exit 1
}

pass() {
  echo "HEADLESS-GPU GATE: PASS"
  exit 0
}

# Print supervisor /status (best-effort) for diagnosis on a 503.
print_status() {
  local status_body
  status_body="$(curl -fsS --connect-timeout "$CURL_CONNECT_TIMEOUT_SEC" \
    "${EMBED_URL}/status" 2>/dev/null || true)"
  if [[ -n "$status_body" ]]; then
    log "/status: $(printf '%s' "$status_body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("<unparseable /status>"); sys.exit(0)
print("state={} last_request_ts={} gpu_free_mib={} target_container={}".format(
    d.get("state"), d.get("last_request_ts"),
    d.get("gpu_free_mib"), d.get("target_container")))
' 2>/dev/null || echo '<unparseable /status>')"
  else
    log "/status: <unreachable>"
  fi
}

# ── 1. /healthz must be HTTP 200 ───────────────────────────────────────
# On a connect failure curl already prints '000'; capture rc and set code=000
# explicitly rather than appending '|| echo 000' (which would yield '000000').
healthz_code="$(curl -s -o /dev/null -w '%{http_code}' \
  --connect-timeout "$CURL_CONNECT_TIMEOUT_SEC" \
  "${EMBED_URL}/healthz" 2>/dev/null)" || healthz_code="000"
if [[ "$healthz_code" != "200" ]]; then
  fail "/healthz returned HTTP $healthz_code (expected 200); supervisor not live"
fi
log "/healthz: HTTP 200"

# ── 2. GET / must report device==cuda AND cuda_available==true ─────────
# This proxies to qodo (WAKES it) and is the ONLY proof of GPU vs CPU. The
# operator forces IDLE_STOPPED before running this gate (see README op-seq), so
# this GET / triggers the GENUINE cold start (probe -> qodo start). We start the
# cold-start stopwatch RIGHT HERE — before GET /  — so the wake cost is timed
# (FIX 4a): the boot->first-embed bound covers GET / + the first /embed.
COLD_START="$(date +%s.%N)"

# Capture status + body WITHOUT -f (FIX 5): a supervisor 503
# (gpu_busy/container_unavailable) carries a JSON body we must surface, not
# discard as a bare "unreachable". -f would drop the body on 5xx.
root_tmp="$(mktemp)"
root_code="$(curl -s -o "$root_tmp" -w '%{http_code}' \
  --connect-timeout "$CURL_CONNECT_TIMEOUT_SEC" \
  "${EMBED_URL}/" 2>/dev/null)" || root_code="000"
root_body="$(cat "$root_tmp")"
rm -f "$root_tmp"

if [[ "$root_code" == "503" ]]; then
  log "GET / -> HTTP 503 (supervisor not ready)"
  print_status
  detail="$(printf '%s' "$root_body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)
print(str(d.get("error", "")))
' 2>/dev/null || echo '')"
  fail "GET / returned 503 (${detail:-gpu_busy or container_unavailable}) — see /status above"
fi
if [[ "$root_code" != "200" ]]; then
  fail "GET / returned HTTP $root_code (expected 200); supervisor unreachable; cannot verify device"
fi
if [[ -z "$root_body" ]]; then
  fail "GET / returned empty body on HTTP 200; cannot verify device"
fi

device="$(printf '%s' "$root_body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("__PARSE_ERROR__"); sys.exit(0)
dev = str(d.get("device", "")).lower()
cu = d.get("cuda_available")
# Emit "<device>|<cuda_available>" for the shell to assert on.
print("{}|{}".format(dev, "true" if cu is True else "false"))
' 2>/dev/null || echo '__PARSE_ERROR__')"

if [[ "$device" == "__PARSE_ERROR__" ]]; then
  fail "GET / body is not valid JSON; cannot verify device. Body: ${root_body:0:200}"
fi

dev_field="${device%%|*}"
cuda_field="${device##*|}"
if [[ "$dev_field" != "cuda" || "$cuda_field" != "true" ]]; then
  fail "CPU fallback rejected — GET / reports device=${dev_field} cuda_available=${cuda_field} (need device=cuda + cuda_available=true)"
fi
log "GET /: device=cuda cuda_available=true"

# ── 3. POST /embed — dim 1536, timed (first=cold, second=warm) ─────────
# Returns the elapsed seconds (float) on stdout via a global; sets EMBED_HTTP
# and EMBED_BODY for the caller to inspect.
EMBED_HTTP=""
EMBED_BODY=""
EMBED_ELAPSED=""
do_embed() {
  local start end
  local tmp_body
  tmp_body="$(mktemp)"
  start="$(date +%s.%N)"
  # curl already prints '000' on connect failure; capture rc and set '000'
  # explicitly (appending '|| echo 000' would produce '000000').
  EMBED_HTTP="$(curl -s -o "$tmp_body" -w '%{http_code}' \
    --connect-timeout "$CURL_CONNECT_TIMEOUT_SEC" \
    -X POST "${EMBED_URL}/embed" \
    -H 'Content-Type: application/json' \
    -d '{"texts":["gate"]}' 2>/dev/null)" || EMBED_HTTP="000"
  end="$(date +%s.%N)"
  EMBED_BODY="$(cat "$tmp_body")"
  rm -f "$tmp_body"
  EMBED_ELAPSED="$(python3 -c "print(f'{${end} - ${start}:.3f}')" 2>/dev/null || echo '0')"
}

# First /embed. The cold-start cost is in the GET / wake above (probe -> qodo
# start), which the COLD_START stopwatch already captures; the boot->first-embed
# bound is enforced below over the WHOLE (GET / + first /embed) window.
do_embed
if [[ "$EMBED_HTTP" == "503" ]]; then
  log "POST /embed -> HTTP 503 (supervisor not ready)"
  print_status
  fail "POST /embed returned 503 (gpu_busy or container_unavailable) — see /status above"
fi
if [[ "$EMBED_HTTP" != "200" ]]; then
  fail "POST /embed (first) returned HTTP $EMBED_HTTP (expected 200)"
fi

first_elapsed="$EMBED_ELAPSED"
# Validate the FIRST embed shape: outer length 1, inner length 1536.
shape="$(printf '%s' "$EMBED_BODY" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("__PARSE_ERROR__"); sys.exit(0)
if not isinstance(d, list) or not d or not isinstance(d[0], list):
    print("__SHAPE_ERROR__"); sys.exit(0)
print("{}|{}".format(len(d), len(d[0])))
' 2>/dev/null || echo '__PARSE_ERROR__')"

if [[ "$shape" == "__PARSE_ERROR__" || "$shape" == "__SHAPE_ERROR__" ]]; then
  fail "POST /embed body is not a [[float,...]] vector list. Body: ${EMBED_BODY:0:200}"
fi
outer="${shape%%|*}"
inner="${shape##*|}"
if [[ "$outer" != "1" ]]; then
  fail "POST /embed outer length is $outer (expected 1 for a single text)"
fi
if [[ "$inner" != "1536" ]]; then
  fail "POST /embed inner dim is $inner (expected 1536); wrong/CPU model or bad embedding"
fi
log "POST /embed (first): 1x1536 in ${first_elapsed}s"

# Enforce the boot->first-embed cold-start bound over the WHOLE window:
# GET / (the wake) + the first /embed, measured from COLD_START set just before
# GET /. Timing the /embed alone (the old bug) hid the cold-start cost in GET /.
COLD_END="$(date +%s.%N)"
cold_total="$(python3 -c "print(f'{${COLD_END} - ${COLD_START}:.3f}')" 2>/dev/null || echo '0')"
if python3 -c "import sys; sys.exit(0 if float('$cold_total') <= float('$FIRST_EMBED_CEILING_SEC') else 1)"; then
  log "GET /+first-embed within boot bound (${cold_total}s <= ${FIRST_EMBED_CEILING_SEC}s)"
else
  fail "boot->first-embed (GET /+embed) took ${cold_total}s > ${FIRST_EMBED_CEILING_SEC}s ceiling"
fi

# Second call: warm path. Timed vs WARM_LATENCY_CEILING_SEC.
do_embed
if [[ "$EMBED_HTTP" != "200" ]]; then
  fail "POST /embed (warm) returned HTTP $EMBED_HTTP (expected 200)"
fi
warm_elapsed="$EMBED_ELAPSED"
log "POST /embed (warm): ${warm_elapsed}s"
if python3 -c "import sys; sys.exit(0 if float('$warm_elapsed') <= float('$WARM_LATENCY_CEILING_SEC') else 1)"; then
  log "warm latency within ceiling (${warm_elapsed}s <= ${WARM_LATENCY_CEILING_SEC}s)"
else
  fail "warm /embed latency ${warm_elapsed}s > ${WARM_LATENCY_CEILING_SEC}s ceiling"
fi

# ── All checks green ───────────────────────────────────────────────────
pass
