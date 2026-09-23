#!/bin/bash
# Runs on boot via systemd (ais-tracker.service).
# Pulls latest code, then starts the boat server.

set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"   # repo root

cd "$DIR"

# Same default as start_boat.sh, so the two can't disagree about the port.
PORT=${PORT:-8080}
export PORT

# Everything this script prints also lands in a file the boat server can serve
# at /api/health. The Pi's SSH password was lost, so journalctl became
# unreachable and "why did the logger die?" was unanswerable — a boot log that
# survives over HTTP is the only diagnostic left. Truncated each boot.
BOOT_LOG="$DIR/logs/startup.log"
mkdir -p "$DIR/logs"
exec > >(tee "$BOOT_LOG") 2>&1

echo "[startup] $(date -u +%Y-%m-%dT%H:%M:%SZ) booting"

echo "[startup] Pulling latest code..."
git fetch origin 2>&1 && git reset --hard origin/boat-mode 2>&1 || echo "[startup] git update failed (offline?), continuing with current code"

# --- SSH key install -------------------------------------------------------
# This script runs as the same user that owns ~/.ssh, so it can restore key
# access without a password or a keyboard. Idempotent: only appends a key that
# is not already present, and never rewrites an existing authorized_keys
# wholesale. sshd refuses to use the file at all if the modes are loose, so set
# them explicitly.
install_ssh_keys() {
    local src="$DIR/pi/authorized_keys.pub"
    [ -f "$src" ] || return 0
    local ak="$HOME/.ssh/authorized_keys"
    mkdir -p "$HOME/.ssh"
    chmod 700 "$HOME/.ssh"
    touch "$ak"
    chmod 600 "$ak"
    local added=0
    while IFS= read -r key; do
        # Skip blanks and comments.
        case "$key" in ''|\#*) continue ;; esac
        if ! grep -qxF "$key" "$ak" 2>/dev/null; then
            printf '%s\n' "$key" >> "$ak"
            added=$((added + 1))
        fi
    done < "$src"
    echo "[startup] ssh keys: $added added, $(grep -c . "$ak" 2>/dev/null || echo 0) total"
}
install_ssh_keys || echo "[startup] ssh key install failed (non-fatal)"

# --- NMEA logger, supervised ----------------------------------------------
# Previously this was fired once into the background and forgotten: if it died
# for any reason, nothing noticed and nothing restarted it, so the voyage
# recording simply stopped with no indication anywhere. That happened on
# 2026-09-13. Now it is respawned, with a short backoff so a persistently
# broken logger cannot spin.
#
# The initial sleep matters: start_boat.sh sweeps stale "nmea_capture.py"
# processes as its first act, and the python PID inside this supervisor is not
# the PID in KEEP_PIDS, so launching immediately would let that sweep kill the
# process we just started.
supervise_logger() {
    sleep 5
    local fails=0
    while true; do
        echo "[startup] starting NMEA logger (attempt $((fails + 1)))"
        python3 "$DIR/nmea_capture.py" \
            --ws-url "ws://localhost:$PORT/nmea" --web-port 8081
        local rc=$?
        fails=$((fails + 1))
        echo "[startup] NMEA logger exited rc=$rc — restarting in 10s"
        sleep 10
    done
}
supervise_logger &
KEEP_PIDS=$!
export KEEP_PIDS

echo "[startup] Starting boat server..."
exec "$DIR/start_boat.sh"
