#!/usr/bin/env bash
# scripts/mpv_control.example.sh — Reference impl of the 'mpv_control'
# helper that the probe's mpv_file_pos_sec sensor expects on $PATH.
#
# Speaks to mpv via its IPC socket (mpv must be started with
# `--input-ipc-server=/tmp/mpvsocket`). Returns the playback time-pos
# in integer seconds on stdout.
#
# Install:
#   sudo cp scripts/mpv_control.example.sh /usr/local/bin/mpv_control
#   sudo chmod +x /usr/local/bin/mpv_control
#
# Usage by the probe:
#   subprocess.run(['mpv_control', 'file_pos_sec'], timeout=3)
#
# Other commands (toggle_pause, etc.) can be added — the probe only
# reads file_pos_sec.

set -u

SOCKET="${MPV_SOCKET:-/tmp/mpvsocket}"

if [ ! -S "$SOCKET" ]; then
    echo "mpv socket not found at $SOCKET — is mpv running with --input-ipc-server?" >&2
    exit 2
fi

case "${1:-}" in
    file_pos_sec)
        # 'time-pos' returns float; round to int for the probe payload.
        # socat is the reliable way to talk to a unix-socket — netcat
        # variants differ in -U flag handling.
        if ! command -v socat >/dev/null 2>&1; then
            echo "socat not on PATH (apt install socat / brew install socat)" >&2
            exit 2
        fi
        response=$(echo '{"command":["get_property","time-pos"]}' \
            | socat - "$SOCKET" 2>/dev/null)
        # response: {"data":12.345,"error":"success","request_id":0}
        pos=$(echo "$response" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    if d.get("error") == "success":
        print(int(d["data"]))
        sys.exit(0)
    sys.exit(1)
except Exception:
    sys.exit(1)
')
        if [ -z "$pos" ]; then
            echo "could not parse time-pos from mpv response: $response" >&2
            exit 1
        fi
        echo "$pos"
        ;;
    *)
        echo "Usage: $0 {file_pos_sec}" >&2
        exit 2
        ;;
esac
