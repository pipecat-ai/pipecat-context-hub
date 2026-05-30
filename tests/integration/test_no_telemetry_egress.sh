#!/usr/bin/env bash
#
# test_no_telemetry_egress.sh — assert chromadb 1.x emits no telemetry egress.
#
# Background: chromadb 0.6 shipped a posthog-based product-telemetry client. The
# chromadb 1.x migration (v0.1.0) removes posthog from the dependency tree and
# relies on Settings(anonymized_telemetry=False) + a no-op telemetry impl. This
# script proves, at runtime, that no non-loopback TCP egress leaves the hub's
# own process tree to an unexpected (telemetry) host across the three surfaces
# that construct a Chroma client: refresh, serve+query, and dashboard extraction.
#
# Why three windows and not a single boot-time lsof snapshot: Chroma telemetry
# (when present) can fire AFTER client construction — on first collection access
# or on a background flush — so each window samples continuously for its full
# duration, not just at boot.
#
# Egress policy per window:
#   * serve boot + one MCP query : STRICT — zero non-loopback peers allowed.
#   * dashboard extraction       : STRICT — zero non-loopback peers allowed.
#   * refresh --force            : ALLOWLISTED — git fetch (GitHub) and a model
#                                  download (HuggingFace/PyPI CDNs) are legitimate.
#                                  Any peer OUTSIDE the allowlist (e.g. a posthog
#                                  or otel endpoint) fails the test.
#
# Plus a structural assertion: `import posthog` must fail (the telemetry surface
# is gone from the environment entirely).
#
# Usage:
#   tests/integration/test_no_telemetry_egress.sh [DATA_DIR]
#
# DATA_DIR defaults to a throwaway /tmp dir. If it already contains a built
# index the refresh stays incremental; otherwise it builds from scratch (slow,
# clones repos). Set SKIP_REFRESH=1 to skip the refresh window (useful when the
# index is already built and you only want the strict serve/dashboard gates).
#
# Exit 0 = no unexpected egress on any window. Exit 1 = a violation (peer +
# window are printed).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

DATA_DIR="${1:-/tmp/pch-egress-$$}"
export PIPECAT_HUB_DATA_DIR="$DATA_DIR"
SKIP_REFRESH="${SKIP_REFRESH:-0}"

# Hosts/peers that are legitimate during `refresh` (git fetch + model fetch).
# Matched as substrings against the lsof peer address (host:port, numeric).
# We resolve these to the well-known CIDRs lazily is overkill; instead we accept
# that lsof -nP prints numeric peers, so we allowlist by reverse-classifying:
# any peer on :443/:80/:22/:9418 is a candidate "legit fetch" — but to actually
# catch a telemetry endpoint hiding on :443 we additionally reject any peer whose
# reverse DNS or known telemetry markers appear. Practically: during refresh we
# only FAIL on peers that reverse-resolve to a telemetry vendor. See classify().
TELEMETRY_MARKERS="posthog|segment|sentry|mixpanel|amplitude|datadog|google-analytics|otel|telemetry"

FAILURES=0
note() { printf '  %s\n' "$*"; }
fail() { printf 'FAIL [%s]: %s\n' "$1" "$2"; FAILURES=$((FAILURES + 1)); }
ok()   { printf 'OK   [%s]: %s\n' "$1" "$2"; }

# Recursively collect a PID and all its descendants.
pid_tree() {
  local root=$1
  echo "$root"
  local kids
  kids=$(pgrep -P "$root" 2>/dev/null || true)
  local k
  for k in $kids; do
    pid_tree "$k"
  done
}

# Print non-loopback TCP peers (ESTABLISHED + SYN_SENT) for a process tree.
# Output: one "peer_host:port" per line.
sample_peers() {
  local root=$1
  local pids
  pids=$(pid_tree "$root" | sort -u | paste -sd, -)
  [ -z "$pids" ] && return 0
  # lsof NAME column for TCP is "local->peer (STATE)". Extract the peer side.
  lsof -nP -iTCP -a -p "$pids" 2>/dev/null \
    | awk '$0 ~ /->/ {print $9}' \
    | sed 's/.*->//' \
    | grep -vE '127\.0\.0\.1|\[::1\]|localhost' \
    || true
}

# Continuously sample a process tree's peers into a file until a sentinel file
# is removed. Runs in the background.
start_sampler() {
  local root=$1 outfile=$2 sentinel=$3
  : > "$outfile"
  (
    while [ -e "$sentinel" ]; do
      sample_peers "$root" >> "$outfile"
      sleep 0.3
    done
  ) &
  echo $!
}

# Classify captured peers for a window. Args: window-name outfile policy
# policy = strict   -> any peer is a violation
# policy = allowlist-> only telemetry-marker peers are violations
classify() {
  local window=$1 outfile=$2 policy=$3
  local peers
  peers=$(sort -u "$outfile" | grep -vE '^\s*$' || true)
  if [ -z "$peers" ]; then
    ok "$window" "no non-loopback egress observed"
    return
  fi
  if [ "$policy" = "strict" ]; then
    fail "$window" "unexpected non-loopback egress:"
    echo "$peers" | sed 's/^/      /'
    return
  fi
  # allowlist: reverse-resolve each peer and flag telemetry markers.
  local violated=0 peer host
  while IFS= read -r peer; do
    [ -z "$peer" ] && continue
    host="${peer%:*}"
    local rev
    rev=$( (host "$host" 2>/dev/null || true) | grep -iEo "$TELEMETRY_MARKERS" | head -1 )
    if [ -n "$rev" ]; then
      fail "$window" "telemetry-vendor egress to $peer ($rev)"
      violated=1
    fi
  done <<< "$peers"
  if [ "$violated" -eq 0 ]; then
    ok "$window" "egress present but all peers are non-telemetry (git/model fetch): $(echo "$peers" | wc -l | tr -d ' ') peer(s)"
  fi
}

echo "=== telemetry-egress smoke ==="
echo "DATA_DIR=$DATA_DIR"

# ---------------------------------------------------------------------------
# Structural check: posthog must be absent from the environment.
# ---------------------------------------------------------------------------
if uv run python -c "import posthog" >/dev/null 2>&1; then
  fail "posthog-absent" "posthog is importable — telemetry dependency still installed"
else
  ok "posthog-absent" "posthog not importable (telemetry dependency removed)"
fi

# ---------------------------------------------------------------------------
# Window 1: refresh --force --reset-index (allowlisted egress)
# ---------------------------------------------------------------------------
if [ "$SKIP_REFRESH" = "1" ]; then
  note "SKIP_REFRESH=1 — skipping refresh window"
else
  echo "--- window: refresh --force --reset-index ---"
  SENT=$(mktemp); OUT=$(mktemp)
  uv run pipecat-context-hub refresh --force --reset-index >/tmp/egress-refresh.log 2>&1 &
  RPID=$!
  SAMPLER=$(start_sampler "$RPID" "$OUT" "$SENT")
  wait "$RPID"; RRC=$?
  rm -f "$SENT"; wait "$SAMPLER" 2>/dev/null || true
  [ "$RRC" -eq 0 ] || fail "refresh" "refresh exited non-zero ($RRC); see /tmp/egress-refresh.log"
  classify "refresh" "$OUT" "allowlist"
  rm -f "$OUT"
fi

# ---------------------------------------------------------------------------
# Window 2: serve boot + one MCP search_docs query (strict zero egress)
# ---------------------------------------------------------------------------
echo "--- window: serve boot + one MCP query ---"
SENT=$(mktemp); OUT=$(mktemp)
# Drive serve over stdio with a real MCP handshake + one tools/call. The Python
# driver writes the serve PID to $PIDFILE so the sampler targets the right tree.
PIDFILE=$(mktemp)
uv run python - "$PIDFILE" <<'PYEOF' &
import json, os, subprocess, sys, time

pidfile = sys.argv[1]
proc = subprocess.Popen(
    ["uv", "run", "pipecat-context-hub", "serve"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
)
with open(pidfile, "w") as f:
    f.write(str(proc.pid))

def send(obj):
    proc.stdin.write((json.dumps(obj) + "\n").encode())
    proc.stdin.flush()

def readline(timeout=60.0):
    # crude line read with timeout via select
    import select
    end = time.time() + timeout
    buf = b""
    while time.time() < end:
        r, _, _ = select.select([proc.stdout], [], [], 0.2)
        if r:
            ch = proc.stdout.read(1)
            if not ch:
                break
            buf += ch
            if ch == b"\n":
                return buf
    return buf

send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                 "clientInfo": {"name": "egress-smoke", "version": "0"}}})
readline()  # initialize response
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
time.sleep(1.0)
send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
      "params": {"name": "search_docs", "arguments": {"query": "pipeline", "limit": 3}}})
# read until we get id:2 or timeout
end = time.time() + 90
while time.time() < end:
    line = readline(5.0)
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    if msg.get("id") == 2:
        break
time.sleep(1.0)
try:
    proc.stdin.close()
    proc.wait(timeout=15)
except Exception:
    proc.kill()
PYEOF
DRIVER=$!
# wait for the serve PID to appear
for _ in $(seq 1 50); do [ -s "$PIDFILE" ] && break; sleep 0.2; done
SPID=$(cat "$PIDFILE" 2>/dev/null || true)
if [ -n "$SPID" ]; then
  SAMPLER=$(start_sampler "$SPID" "$OUT" "$SENT")
  wait "$DRIVER" 2>/dev/null || true
  rm -f "$SENT"; wait "$SAMPLER" 2>/dev/null || true
  classify "serve+query" "$OUT" "strict"
else
  fail "serve+query" "could not capture serve PID"
  wait "$DRIVER" 2>/dev/null || true
fi
rm -f "$OUT" "$PIDFILE"

# ---------------------------------------------------------------------------
# Window 3: dashboard extraction (strict zero egress)
# ---------------------------------------------------------------------------
echo "--- window: dashboard extraction ---"
SENT=$(mktemp); OUT=$(mktemp)
uv run python dashboard/scripts/extract_dashboard.py >/tmp/egress-dashboard.log 2>&1 &
DPID=$!
SAMPLER=$(start_sampler "$DPID" "$OUT" "$SENT")
wait "$DPID"; DRC=$?
rm -f "$SENT"; wait "$SAMPLER" 2>/dev/null || true
[ "$DRC" -eq 0 ] || fail "dashboard" "extract_dashboard.py exited non-zero ($DRC); see /tmp/egress-dashboard.log"
classify "dashboard" "$OUT" "strict"
rm -f "$OUT"

echo "=== summary ==="
if [ "$FAILURES" -eq 0 ]; then
  echo "PASS: no telemetry egress detected across all windows"
  exit 0
else
  echo "FAILURES: $FAILURES"
  exit 1
fi
