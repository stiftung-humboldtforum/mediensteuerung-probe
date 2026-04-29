#!/usr/bin/env bash
# scripts/smoke-test.sh — Post-Deploy / Pre-Deploy Verification
#
# Verifies that a running probe responds correctly via an MQTT broker.
# Run this AFTER the probe is started (systemctl start humboldt-probe
# or via NSSM), against the same broker the probe connects to.
#
# Requirements: mosquitto-clients (mosquitto_sub / mosquitto_pub).
#   macOS:   brew install mosquitto
#   Debian:  sudo apt install mosquitto-clients
#
# Usage:
#   ./scripts/smoke-test.sh <broker-host> [<probe-fqdn>]
#
# Examples:
#   ./scripts/smoke-test.sh 127.0.0.1
#   ./scripts/smoke-test.sh mqtt.example.com kiosk-01.museum.local
#
# Exit codes:
#   0 — all checks passed
#   1 — at least one check failed
#   2 — invocation error (missing args, missing dependency)

set -u
set -o pipefail

# --- Args / defaults ------------------------------------------------------

if [ $# -lt 1 ]; then
    echo "Usage: $0 <broker-host> [<probe-fqdn>]" >&2
    echo "  Default <probe-fqdn> = $(hostname -f 2>/dev/null || hostname)" >&2
    exit 2
fi

BROKER="$1"
PROBE_FQDN="${2:-$(hostname -f 2>/dev/null || hostname)}"
PORT="${PORT:-1883}"  # override via env: PORT=8883 ./smoke-test.sh ...
TIMEOUT="${TIMEOUT:-10}"  # seconds per subscribe wait

# --- Pre-flight -----------------------------------------------------------

command -v mosquitto_sub >/dev/null 2>&1 || {
    echo "ERROR: mosquitto_sub not found on PATH (install mosquitto-clients)" >&2
    exit 2
}
command -v mosquitto_pub >/dev/null 2>&1 || {
    echo "ERROR: mosquitto_pub not found on PATH" >&2
    exit 2
}

PASS=0
FAIL=0

check() {
    local name="$1"; shift
    local expected="$1"; shift
    local topic="$1"; shift

    # mosquitto_sub with -W (wait timeout) and -C 1 (exit after 1 message)
    local got
    got=$(mosquitto_sub -h "$BROKER" -p "$PORT" \
                        -W "$TIMEOUT" -C 1 \
                        -t "$topic" 2>/dev/null || true)

    if [ -z "$got" ]; then
        printf '[%s] %s — timeout after %ss waiting for %s\n' "FAIL" "$name" "$TIMEOUT" "$topic"
        FAIL=$((FAIL + 1))
        return 1
    fi

    if echo "$got" | grep -qE "$expected"; then
        printf '[%s] %s — got: %s\n' "OK  " "$name" "$(echo "$got" | head -c 100)"
        PASS=$((PASS + 1))
        return 0
    else
        printf '[%s] %s — expected /%s/ got: %s\n' "FAIL" "$name" "$expected" "$(echo "$got" | head -c 100)"
        FAIL=$((FAIL + 1))
        return 1
    fi
}

echo "Smoke-testing probe: $PROBE_FQDN via $BROKER:$PORT (timeout ${TIMEOUT}s/check)"
echo

# --- 1. Probe is connected (retained '1') ---------------------------------

# 'connected' is retained — should arrive immediately.
check "connected = '1'" '^1$' "probe/$PROBE_FQDN/connected"

# --- 2. Capabilities are advertised ---------------------------------------

check "capabilities published" '.+' "probe/$PROBE_FQDN/capabilities"

# --- 3. boot_time published -----------------------------------------------

check "boot_time has Unix-epoch result" '"result": *[0-9]+\.[0-9]+' \
      "probe/$PROBE_FQDN/boot_time"

# --- 4. Periodic sensor: temperatures (within 6s) -------------------------

# Periodic sensors aren't retained; we wait one cycle (5s + buffer).
TIMEOUT_SAVE="$TIMEOUT"; TIMEOUT=8
check "temperatures cycle" '"status": *"complete"' \
      "probe/$PROBE_FQDN/temperatures"
TIMEOUT="$TIMEOUT_SAVE"

# --- 5. errors-Topic --------------------------------------------------

TIMEOUT_SAVE="$TIMEOUT"; TIMEOUT=8
check "errors aggregation" '"data"' \
      "probe/$PROBE_FQDN/errors"
TIMEOUT="$TIMEOUT_SAVE"

# --- 6. Manager-command: blocked method ('eval' nicht in capabilities) -----

# Triggert eine response auf 'probe/$fqdn/eval'. Vorher subscribe starten,
# dann publish, dann response abfangen.
echo
echo "  → testing blocked command 'eval' ..."
RESP_FILE=$(mktemp)
mosquitto_sub -h "$BROKER" -p "$PORT" -W 5 -C 1 \
              -t "probe/$PROBE_FQDN/eval" > "$RESP_FILE" 2>/dev/null &
SUB_PID=$!
sleep 0.5
mosquitto_pub -h "$BROKER" -p "$PORT" -t "manager/$PROBE_FQDN/eval" -m '' 2>/dev/null
wait "$SUB_PID" 2>/dev/null || true
RESP=$(cat "$RESP_FILE")
rm -f "$RESP_FILE"
if echo "$RESP" | grep -qE 'Method not allowed'; then
    echo "[OK  ] blocked command rejected with 'Method not allowed'"
    PASS=$((PASS + 1))
else
    echo "[FAIL] blocked command — expected 'Method not allowed', got: $RESP"
    FAIL=$((FAIL + 1))
fi

# --- 7. Manager-command: module-attribute access (security) ---------------

# 'os' ist Modul-Attribut, mit Whitelist-Schutz aus S1+S2 muss
# entweder 'Method not allowed' (wenn nicht in capabilities) oder
# 'Unknown method' (wenn in capabilities aber nicht in COMMANDS) kommen.
echo
echo "  → testing module-attribute attack 'os' ..."
RESP_FILE=$(mktemp)
mosquitto_sub -h "$BROKER" -p "$PORT" -W 5 -C 1 \
              -t "probe/$PROBE_FQDN/os" > "$RESP_FILE" 2>/dev/null &
SUB_PID=$!
sleep 0.5
mosquitto_pub -h "$BROKER" -p "$PORT" -t "manager/$PROBE_FQDN/os" -m '' 2>/dev/null
wait "$SUB_PID" 2>/dev/null || true
RESP=$(cat "$RESP_FILE")
rm -f "$RESP_FILE"
if echo "$RESP" | grep -qE 'Method not allowed|Unknown method'; then
    echo "[OK  ] module-attribute access blocked"
    PASS=$((PASS + 1))
else
    echo "[FAIL] module-attribute access — expected reject, got: $RESP"
    FAIL=$((FAIL + 1))
fi

# --- Summary --------------------------------------------------------------

echo
echo "──────────────────────────────────────────────"
echo "Result: $PASS passed, $FAIL failed"
if [ "$FAIL" -eq 0 ]; then
    echo "Smoke-test PASSED."
    exit 0
else
    echo "Smoke-test FAILED — investigate before continuing deploy."
    exit 1
fi
