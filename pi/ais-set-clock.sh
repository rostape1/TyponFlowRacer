#!/bin/bash
# ais-set-clock — step the system clock onto GPS time. Runs as root via sudo.
#
# INSTALL (one time, on the Pi, needs the password once):
#
#   sudo install -m 755 -o root -g root pi/ais-set-clock.sh /usr/local/sbin/ais-set-clock
#   echo 'rostape1 ALL=(root) NOPASSWD: /usr/local/sbin/ais-set-clock' \
#       | sudo tee /etc/sudoers.d/ais-set-clock
#   sudo chmod 440 /etc/sudoers.d/ais-set-clock
#   sudo visudo -c                       # verify before trusting it
#
# Then add --set-system-clock to nmea_capture.py's invocation in pi/startup.sh.
#
# WHY this exists. The Pi has no RTC and no internet at sea, so NTP never runs
# and fake-hwclock restores whatever time it had at last shutdown. It was up to
# 115 hours wrong, which invented the dates on 52 of 76 recordings. The logger
# already corrects its own timestamps from GPS without any privilege; this helper
# additionally fixes what the logger cannot reach — file mtimes, boat_server's
# own log lines, /api/logs' sort order.
#
# WHY a dedicated helper rather than NOPASSWD on `date`. A blanket sudo rule for
# date(1) is a root shell in all but name. This accepts exactly one argument, in
# exactly one format, and does nothing else.
#
# Argument: "YYYY-MM-DD HH:MM:SS", interpreted as UTC.

set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "usage: $0 'YYYY-MM-DD HH:MM:SS'  (UTC)" >&2
    exit 2
fi

WHEN="$1"

# Validate strictly before going anywhere near the clock. Anything that is not
# exactly this shape is rejected — this runs as root and its only input comes
# from a sentence off a radio link.
if ! [[ "$WHEN" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}$ ]]; then
    echo "refusing: '$WHEN' is not 'YYYY-MM-DD HH:MM:SS'" >&2
    exit 2
fi

# Sanity-bound the target. A GPS week-number rollover or a corrupt sentence that
# slipped through can name 1999 or 2082; stepping the clock there would be worse
# than leaving it wrong.
YEAR="${WHEN:0:4}"
if [ "$YEAR" -lt 2024 ] || [ "$YEAR" -gt 2040 ]; then
    echo "refusing: year $YEAR outside 2024-2040" >&2
    exit 3
fi

# If NTP is actually in charge, leave the clock alone — it is already better than
# GPS-via-serial and fighting it produces a jittering clock.
if command -v timedatectl >/dev/null 2>&1; then
    if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qi '^yes$'; then
        echo "NTP is synchronised; not overriding"
        exit 0
    fi
fi

date -u -s "$WHEN" >/dev/null
echo "system clock set to $WHEN UTC"

# Persist it, so the next boot starts from something sane rather than from the
# stale value that caused this in the first place.
if command -v fake-hwclock >/dev/null 2>&1; then
    fake-hwclock save || true
fi
if [ -e /dev/rtc0 ] && command -v hwclock >/dev/null 2>&1; then
    hwclock --systohc || true
fi
