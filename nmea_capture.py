#!/usr/bin/env python3
"""
NMEA capture — taps the boat server's WebSocket (/nmea) and logs all
sentences to hourly-rotated files. Serves a mobile-friendly status page.

Reads from the boat server WebSocket so it doesn't compete with the
server for the single TCP connection from the instruments.

Usage:
    python nmea_capture.py                        # default ws url
    python nmea_capture.py --ws-url ws://localhost:8080/nmea
    python nmea_capture.py --web-port 8081
"""

import argparse
import asyncio
import glob
import html
import os
import shutil
import ssl
import subprocess
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    import websockets
except ImportError:
    raise SystemExit("Install websockets: pip install websockets")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")

# Logs are NEVER deleted — they are race data. The old KEEP_DAYS sweep was
# removed deliberately. What replaces it is an alert, because "keep forever"
# on an SD card is how P11 happens: the card fills, writes start failing, and
# NMEA logging stops without anything saying so. Free space is the number
# that matters (not a percentage), so the status page shows it in bytes and
# goes amber then red as it runs out.
DISK_WARN_BYTES = 5 * 1024**3   # amber below 5 GB free
DISK_CRIT_BYTES = 1 * 1024**3   # red below 1 GB free

# Don't project headroom from too small a sample — an early guess is worse than
# no number, because this is the panel the operator is supposed to trust.
HEADROOM_MIN_SAMPLE_S = 600

# The status page refreshes every 5s; the disk sweep is O(number of logs).
DISK_STATS_TTL_S = 30

stats = {
    # No wall-clock start time on purpose: nothing may subtract one (P33).
    "start_monotonic": None,
    "bytes_written": 0,
    "write_error": None,
    "sentences": 0,
    "current_file": "",
    "connected": False,
    "source": "",
    "recent": deque(maxlen=10),
}

_current_hour = None
_outfile = None
_disk_cache = None   # (monotonic_time, result) — see disk_stats()


# --- the log clock ---------------------------------------------------------
# The Pi has no RTC. With no internet at sea NTP never runs, so fake-hwclock
# restores whatever time the Pi had at its last shutdown and the clock stays
# behind by however long it was powered off — cumulatively, across boots.
#
# On 2026-09-23 a survey found 52 of 76 recordings mis-stamped, by up to 115
# hours. Nothing looked broken, because the filename and the line prefixes came
# from the same wrong clock and therefore agreed with each other. The GPS time
# inside $..RMC was the only ground truth in the file.
#
# So the logger keeps its own clock, derived from GPS, and never trusts the
# system clock for anything it writes. This deliberately does NOT touch the
# system clock (that needs root — see --set-system-clock); it only corrects what
# goes into the logs, which is what makes a filename findable.
#
# P33 still applies with full force: this offset must never be used to measure a
# duration. uptime_seconds() stays on time.monotonic().

# How far the system clock may drift from GPS before we treat it as a different
# clock and rotate. Two seconds is comfortably above jitter between a sentence
# arriving and being stamped, and far below any real clock error.
CLOCK_STEP_TOLERANCE_S = 2.0

_gps_offset = None      # seconds to ADD to system time to get truth; None = unknown
_gps_pending = None     # candidate offset awaiting a second, agreeing reading


def reset_gps_clock():
    """Drop all clock state. For tests."""
    global _gps_offset, _gps_pending
    _gps_offset = None
    _gps_pending = None


def nmea_checksum_ok(sentence):
    """True if the sentence's trailing *HH checksum matches its body.

    Unvalidated sentences must never be allowed to move the clock. Over UDP a
    dropped datagram splices the tail of one sentence onto the head of another
    (P40), and a spliced RMC is structurally perfect — it would parse to an
    arbitrary date and step the log clock to it.
    """
    if not sentence or sentence[0] not in "$!":
        return False
    star = sentence.rfind("*")
    if star < 1 or star + 3 > len(sentence.rstrip()):
        return False
    declared = sentence[star + 1:star + 3]
    try:
        want = int(declared, 16)
    except ValueError:
        return False
    got = 0
    for ch in sentence[1:star]:
        got ^= ord(ch)
    return got == want


def parse_rmc_utc(sentence):
    """Epoch seconds from an $..RMC, or None if it cannot be trusted.

    Rejects: a bad checksum, a status other than 'A' (an invalid fix may carry
    the receiver's own uninitialised clock), missing fields, and an impossible
    date or time. RMC is the only sentence the boat emits that carries a *date*
    as well as a time — GGA and GLL have time only — so it is the only usable
    clock source. There is no ZDA on this bus.
    """
    if not sentence or "RMC" not in sentence[:7]:
        return None
    if not nmea_checksum_ok(sentence):
        return None
    parts = sentence.split(",")
    if len(parts) < 10:
        return None
    hhmmss, status, ddmmyy = parts[1], parts[2], parts[9]
    if status != "A":
        return None
    if len(hhmmss) < 6 or len(ddmmyy) != 6:
        return None
    try:
        dt = datetime(
            2000 + int(ddmmyy[4:6]), int(ddmmyy[2:4]), int(ddmmyy[0:2]),
            int(hhmmss[0:2]), int(hhmmss[2:4]), int(hhmmss[4:6]),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None      # impossible date or time — do not roll it over
    return dt.timestamp()


def observe_gps_time(sentence, now=None):
    """Feed a sentence to the clock. True if the offset changed (caller rotates).

    Adoption needs **two consecutive agreeing readings**. One reading is not
    enough: a single corrupt sentence that happens to pass the checksum would
    otherwise redate the whole recording.

    A change of more than CLOCK_STEP_TOLERANCE_S is treated as a different clock
    and reported, so log_sentence can start a new file. That matters: if NTP
    finally reaches the Pi mid-recording, or the first fix arrives minutes in, a
    single file would otherwise contain two clocks — which breaks the replay
    parser and makes playback's auto-advance gap check meaningless.
    """
    global _gps_offset, _gps_pending
    gps = parse_rmc_utc(sentence)
    if gps is None:
        return False
    sys_now = time.time() if now is None else now
    measured = gps - sys_now

    # Already settled and still agreeing: nothing to do. Returning True here
    # would rotate the log file once per second.
    if _gps_offset is not None and abs(measured - _gps_offset) <= CLOCK_STEP_TOLERANCE_S:
        _gps_pending = None
        return False

    if _gps_pending is not None and abs(measured - _gps_pending) <= CLOCK_STEP_TOLERANCE_S:
        _gps_offset = measured
        _gps_pending = None
        return True

    _gps_pending = measured
    return False


def clock_state():
    """What the log clock is doing, for the header line and the status page."""
    if _gps_offset is None:
        return {"source": "system", "offset_s": None}
    return {"source": "gps", "offset_s": _gps_offset}


def clock_now(now=None):
    """The best available UTC time, GPS-derived when we have a fix.

    Falls back to the system clock rather than refusing: losing sentences is
    worse than stamping them with an unverified time. Every artifact written
    under the fallback is marked, so it can never be silently mistaken for good
    data — see make_filename().
    """
    sys_now = time.time() if now is None else now
    return datetime.fromtimestamp(sys_now + (_gps_offset or 0.0), timezone.utc)


# Where the privileged helper lives if it has been installed. Setting the system
# clock needs CAP_SYS_TIME and this process runs as `rostape1`, so the step has
# to go through sudo. Optional by design: without it the GPS offset above still
# makes every log correct, which is the part that matters.
SET_CLOCK_HELPER = "/usr/local/sbin/ais-set-clock"

# Off by default: stepping the system clock needs a one-time privileged setup on
# the Pi, and the logs are already correct without it. --set-system-clock turns
# it on once that setup exists.
SET_SYSTEM_CLOCK = False

_clock_push = {"tried": False, "ok": None, "detail": "not attempted"}


def push_clock_to_system(offset_s, helper=SET_CLOCK_HELPER, runner=None):
    """Try once to step the system clock onto GPS truth. Never fatal.

    Correcting the system clock as well as the log clock fixes the things the
    offset cannot reach: file mtimes, boat_server.py's own log lines, and
    /api/logs' sort order. But it is strictly a bonus — if the helper or its
    sudoers entry is missing we say so once and carry on, because the logs are
    already right.

    Attempted once per process. Re-stepping on every reading would fight NTP if
    the Pi later gets internet, and a clock that jitters is worse than one that
    is merely wrong.
    """
    if _clock_push["tried"]:
        return _clock_push["ok"]
    _clock_push["tried"] = True

    if abs(offset_s) <= CLOCK_STEP_TOLERANCE_S:
        _clock_push.update(ok=None, detail="system clock already correct")
        return None
    if not os.path.exists(helper):
        _clock_push.update(
            ok=False,
            detail=f"helper {helper} not installed — see docs/logging-and-playback.md")
        print(f"[clock] {_clock_push['detail']}")
        return False

    target = datetime.fromtimestamp(time.time() + offset_s, timezone.utc)
    cmd = ["sudo", "-n", helper, target.strftime("%Y-%m-%d %H:%M:%S")]
    try:
        run = runner or (lambda c: subprocess.run(
            c, capture_output=True, text=True, timeout=10))
        res = run(cmd)
        if res.returncode == 0:
            _clock_push.update(ok=True, detail=f"system clock stepped {offset_s / 3600:+.2f}h")
        else:
            _clock_push.update(
                ok=False,
                detail=f"helper failed rc={res.returncode}: {(res.stderr or '').strip()[:120]}")
    except (OSError, subprocess.SubprocessError) as e:
        _clock_push.update(ok=False, detail=f"{type(e).__name__}: {e}")
    print(f"[clock] {_clock_push['detail']}")
    return _clock_push["ok"]


def ts(now=None):
    """Stored timestamp: UTC, with an explicit Z so it is self-describing.

    Reads the **GPS-derived** clock, not the system clock — see clock_now(). The
    Pi's own clock invented dates for 52 of 76 recordings before this existed.

    Stays UTC on purpose. It is unambiguous, DST-proof, comparable across the
    whole archive, and it is what nmea-client.js's replay parser expects. The Z
    is new: without it a line read `2026-09-13 19:03:12` while the filename said
    `120000` (local), which looked like a 7-hour inconsistency inside one file.
    Older logs have no Z and are parsed as UTC, which is what they are.

    Display is a separate concern — every UI surface renders these in local
    time and says so. See local_str().
    """
    return clock_now(now).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


def tzname():
    """The local zone abbreviation — PDT or PST in San Francisco."""
    return datetime.now().astimezone().strftime("%Z") or "local"


def local_str(dt_or_epoch, fmt="%H:%M:%S"):
    """Format for humans: local time (PDT/PST here), never UTC."""
    if isinstance(dt_or_epoch, (int, float)):
        dt = datetime.fromtimestamp(dt_or_epoch)
    else:
        dt = dt_or_epoch
    return dt.strftime(fmt)


def utc_ts_to_local(t):
    """Turn a stored `ts()` string back into a local-time display string."""
    try:
        clean = t.rstrip("Z")
        dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%H:%M:%S.%f")[:-3]
    except (ValueError, TypeError):
        return t  # never lose the line over a formatting problem


def make_filename(now=None):
    """Log path, named in LOCAL time from the GPS-derived clock.

    `_noclock` is appended when there is no GPS fix yet, so a recording written
    under an unverified system clock is identifiable from its name alone. Before
    this marker existed, 52 of 76 files carried invented dates and looked
    completely normal — which is why the marker matters more than the name does.
    """
    dt = clock_now(now).astimezone()
    suffix = "" if _gps_offset is not None else "_noclock"
    return os.path.join(LOG_DIR, f"nmea_{dt.strftime('%Y-%m-%d_%H%M%S')}{suffix}.txt")


def format_bytes(n):
    if n is None:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024.0


def disk_stats():
    """Log footprint and remaining space. Never deletes anything (see P11).

    Memoised: the status page auto-refreshes every 5s and the log directory now
    grows without bound (~8.8k files/year at hourly rotation), so an un-cached
    sweep would run thousands of stat() calls per second against the SD card —
    in the same process that is writing the live NMEA stream. Free space does
    not need 5-second freshness.
    """
    global _disk_cache
    now = time.monotonic()
    if _disk_cache and (now - _disk_cache[0]) < DISK_STATS_TTL_S:
        return _disk_cache[1]

    paths = glob.glob(os.path.join(LOG_DIR, "nmea_*.txt"))
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass  # rotated or removed under us; not worth failing the page for

    try:
        free = shutil.disk_usage(LOG_DIR).free
    except OSError:
        free = None

    if free is None:
        level, dot = "unknown", "⚪"
    elif free < DISK_CRIT_BYTES:
        level, dot = "critical", "🔴"
    elif free < DISK_WARN_BYTES:
        level, dot = "low", "🟡"
    else:
        level, dot = "ok", "🟢"

    # Rate from bytes THIS process wrote, over its own uptime. Needs a real
    # sample before it means anything: 10 minutes of a quiet bus would otherwise
    # project decades, and one minute of a busy one projects hours.
    elapsed = uptime_seconds()
    if elapsed and elapsed >= HEADROOM_MIN_SAMPLE_S and stats["bytes_written"] > 0:
        rate = stats["bytes_written"] / elapsed
    else:
        rate = None

    result = {"files": len(paths), "bytes": total, "free": free,
              "level": level, "dot": dot, "rate": rate}
    _disk_cache = (now, result)
    return result


def format_headroom(free, rate):
    """Turn free bytes into something actionable: days of logging left."""
    if not free or not rate or rate <= 0:
        return ""
    days = free / (rate * 86400)
    if days >= 365:
        return " · >1 year left"
    if days >= 1:
        return f" · ~{days:.0f} days left"
    return f" · ~{days * 24:.0f} hours left"


def disk_alert_html(disk):
    """Loud banner when the card is filling. Nothing is ever auto-deleted, so
    the only thing standing between a full card and silent capture failure is
    the operator seeing this and copying logs off."""
    # A write that is already failing outranks any projection.
    if stats["write_error"]:
        return ("<div class='alert crit'>LOGGING STOPPED — cannot write to the log "
                f"file ({html.escape(stats['write_error'])}). If the card is full, "
                "copy logs off the Pi and delete them by hand.</div>")
    if disk["level"] == "ok":
        return ""
    if disk["level"] == "unknown":
        return ("<div class='alert warn'>Could not read free space on the log "
                "volume. Check the card is still mounted.</div>")
    if disk["level"] == "critical":
        return ("<div class='alert crit'>Disk almost full — capture will stop "
                "when it runs out. Copy logs off the Pi and delete them by hand.</div>")
    return ("<div class='alert warn'>Disk filling up. Logs are never deleted "
            "automatically — copy them off when convenient.</div>")


def uptime_seconds():
    """Elapsed time since this process started.

    Monotonic, NOT wall clock. The Pi has no battery-backed clock: it boots
    believing whatever fake-hwclock saved, then NTP steps the clock forward
    once the network is up. Subtracting two time.time() readings across that
    step counted the correction as elapsed time and reported a 35-minute-old
    process as having been up 112 days.
    """
    if stats["start_monotonic"] is None:
        return None
    return time.monotonic() - stats["start_monotonic"]


def format_uptime(seconds):
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    m = int((seconds % 3600) // 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


def status_html():
    elapsed = uptime_seconds()
    uptime = format_uptime(elapsed) if elapsed is not None else "—"
    disk = disk_stats()
    # A failing write is a storage fault, not a link fault. Say which, so nobody
    # goes and power-cycles the VHF over a full SD card (P34).
    if stats["write_error"]:
        conn_dot, conn_text = "🔴", "Logging FAILED — disk"
    elif stats["connected"]:
        conn_dot, conn_text = "🟢", "Connected"
    else:
        conn_dot, conn_text = "🔴", "Disconnected"
    recent_lines = ""
    for t, sentence in reversed(stats["recent"]):
        shown = utc_ts_to_local(t)
        recent_lines += f"<div class='line'><span class='ts'>{html.escape(shown)}</span> {html.escape(sentence)}</div>\n"
    if not recent_lines:
        recent_lines = "<div class='line dim'>No sentences yet</div>"

    # The clock panel. A wrong log clock is invisible in the data — filenames and
    # line stamps agree with each other because they share the bad clock — so it
    # has to be stated here or it recurs silently.
    cs = clock_state()
    if cs["source"] == "gps":
        clock_dot, clock_level = "🟢", "ok"
        clock_text = "GPS"
        clock_note = f"offset {cs['offset_s'] / 3600:+.2f}h vs system clock"
    else:
        clock_dot, clock_level = "🔴", "critical"
        clock_text = "system (UNVERIFIED)"
        clock_note = "no GPS fix yet — filenames marked _noclock"
    clock_alert = "" if cs["source"] == "gps" else (
        "<div class='alert crit'>No GPS fix, so log timestamps come from the Pi's own "
        "clock, which has no battery backup and is routinely days wrong. Files written "
        "now are named <code>_noclock</code> and their dates cannot be trusted.</div>")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>NMEA Capture</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,system-ui,sans-serif;background:#0a1628;color:#c8d6e5;padding:16px;min-height:100vh}}
h1{{font-size:1.4em;margin-bottom:12px;color:#f5f6fa}}
.card{{background:rgba(255,255,255,0.06);border-radius:12px;padding:16px;margin-bottom:12px;backdrop-filter:blur(8px)}}
.row{{display:flex;justify-content:space-between;align-items:center;padding:6px 0}}
.label{{color:#8395a7;font-size:0.85em}}
.value{{font-size:1.1em;font-weight:600;font-variant-numeric:tabular-nums}}
.big{{font-size:1.8em;color:#f5f6fa}}
.conn{{display:flex;align-items:center;gap:8px}}
.recent{{margin-top:12px}}
.recent h2{{font-size:1em;color:#8395a7;margin-bottom:8px}}
.line{{font-family:'SF Mono',Menlo,monospace;font-size:0.75em;padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.05);word-break:break-all;line-height:1.4}}
.ts{{color:#8395a7}}
.dim{{color:#576574}}
.ok{{color:#2ecc71}}
.low{{color:#f39c12}}
.critical{{color:#e74c3c}}
.unknown{{color:#8395a7}}
.alert{{margin-top:10px;padding:10px 12px;border-radius:8px;font-size:0.85em;line-height:1.4}}
.alert.warn{{background:rgba(243,156,18,0.15);border:1px solid rgba(243,156,18,0.4);color:#f39c12}}
.alert.crit{{background:rgba(231,76,60,0.15);border:1px solid rgba(231,76,60,0.5);color:#e74c3c}}
.footer{{text-align:center;color:#576574;font-size:0.75em;margin-top:16px}}
</style>
</head>
<body>
<h1>NMEA Capture</h1>
<div class="card">
 <div class="row"><span class="label">Status</span><span class="value conn">{conn_dot} {conn_text}</span></div>
 <div class="row"><span class="label">Source</span><span class="value">{html.escape(stats['source'])}</span></div>
 <div class="row"><span class="label">Uptime</span><span class="value">{uptime}</span></div>
</div>
<div class="card">
 <div class="row"><span class="label">Log clock</span><span class="value conn {clock_level}">{clock_dot} {clock_text}</span></div>
 <div class="row"><span class="label">&nbsp;</span><span class="value" style="font-size:0.8em">{html.escape(clock_note)}</span></div>
 <div class="row"><span class="label">System clock</span><span class="value" style="font-size:0.8em">{html.escape(_clock_push['detail'])}</span></div>
 {clock_alert}
</div>
<div class="card">
 <div class="row"><span class="label">Sentences</span><span class="value big">{stats['sentences']:,}</span></div>
 <div class="row"><span class="label">Log files</span><span class="value">{disk['files']}</span></div>
 <div class="row"><span class="label">Current file</span><span class="value" style="font-size:0.8em">{html.escape(os.path.basename(stats['current_file']))}</span></div>
</div>
<div class="card">
 <div class="row"><span class="label">Logs on disk</span><span class="value">{format_bytes(disk['bytes'])}</span></div>
 <div class="row"><span class="label">Free space</span><span class="value conn {disk['level']}">{disk['dot']} {format_bytes(disk['free']) if disk['free'] is not None else 'unknown'}</span></div>
 <div class="row"><span class="label">Headroom</span><span class="value" style="font-size:0.85em">{html.escape(format_headroom(disk['free'], disk['rate']).lstrip(' ·') or '—')}</span></div>
 {disk_alert_html(disk)}
</div>
<div class="card recent">
 <h2>Recent sentences <span style="font-weight:400;text-transform:none">({tzname()}, local)</span></h2>
 {recent_lines}
</div>
<div class="footer">Auto-refreshes every 5s · times shown in {tzname()} · log files store UTC</div>
</body>
</html>"""


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(status_html().encode())

    def log_message(self, format, *args):
        pass


def start_web_server(bind, port):
    server = HTTPServer((bind, port), StatusHandler)
    server.serve_forever()


def log_sentence(sentence, source_url, now=None):
    global _current_hour, _outfile

    # Feed the clock BEFORE stamping, so this very sentence lands in the right
    # file with the right time. A change of clock forces a rotation: one file
    # must never contain two clocks, or the replay parser sees time run
    # backwards and playback's auto-advance compares meaningless gaps.
    clock_changed = observe_gps_time(sentence, now=now)

    if clock_changed and SET_SYSTEM_CLOCK:
        push_clock_to_system(clock_state()["offset_s"])

    stamp_dt = clock_now(now)
    hour = stamp_dt.astimezone().strftime("%Y-%m-%d_%H")
    if hour != _current_hour or clock_changed or _outfile is None:
        if _outfile:
            _outfile.close()
        filename = make_filename(now)
        _outfile = open(filename, "a", encoding="utf-8")
        st = clock_state()
        if st["source"] == "gps":
            _outfile.write(
                f"# clock: gps (offset {st['offset_s'] / 3600:+.2f}h from system clock)\n")
        else:
            _outfile.write(
                "# clock: system — NO GPS FIX YET, these timestamps are UNVERIFIED\n")
        _outfile.write(f"# NMEA capture started {ts(now)} (timestamps are UTC)\n")
        _outfile.write(f"# Source: {source_url}\n#\n")
        _outfile.flush()
        stats["current_file"] = filename
        _current_hour = hour
        print(f"Logging to {filename} [clock: {st['source']}]")

    timestamp = ts(now)
    entry = f"{timestamp}  {sentence}\n"

    # Catch the write failure HERE rather than letting it unwind into
    # capture_ws()'s `except OSError`, which would report a full SD card as
    # "Disconnected" — indistinguishable from a VHF or receiver fault, and the
    # first thing an operator would go and power-cycle. A stopped log is a
    # storage failure and has to say so (P34).
    try:
        _outfile.write(entry)
        _outfile.flush()
    except OSError as e:
        stats["write_error"] = f"{type(e).__name__}: {e}"
        return

    stats["write_error"] = None
    # Bytes written by THIS process — the numerator for the headroom estimate.
    # Deliberately not the size of the log directory: that is four months of
    # archive, and dividing it by this process's uptime reports a Pi restarted
    # a minute ago as filling the card at hundreds of MB/s.
    stats["bytes_written"] += len(entry.encode("utf-8"))
    stats["sentences"] += 1
    stats["recent"].append((timestamp, sentence))


async def capture_ws(ws_url):
    stats["source"] = ws_url
    if ws_url.startswith("wss://"):
        ssl_ctx = ssl.create_default_context()
        # The Pi's cert is self-signed, so verification has to be off for a
        # loopback connection to itself — but only for loopback. Any other host
        # gets normal verification.
        host = urllib.parse.urlsplit(ws_url).hostname or ""
        if host in ("localhost", "127.0.0.1", "::1"):
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
    else:
        ssl_ctx = None

    while True:
        print(f"Connecting to {ws_url}...")
        stats["connected"] = False
        try:
            connect_kwargs = {"ssl": ssl_ctx} if ssl_ctx is not None else {}
            async with websockets.connect(ws_url, **connect_kwargs) as ws:
                stats["connected"] = True
                print("Connected.")
                async for message in ws:
                    sentence = message.strip()
                    if sentence:
                        log_sentence(sentence, ws_url)
        except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
            print(f"WebSocket error: {e}")

        stats["connected"] = False
        print("Reconnecting in 5s...")
        await asyncio.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="NMEA capture via boat server WebSocket")
    parser.add_argument("--ws-url", default="ws://localhost:8080/nmea",
                        help="Boat server NMEA WebSocket URL (default: ws://localhost:8080/nmea)")
    parser.add_argument("--web-port", type=int, default=8081,
                        help="Status page HTTP port (default: 8081; 8080 is the boat server)")
    parser.add_argument("--bind", default="0.0.0.0", help="Status page bind address (default: 0.0.0.0)")
    parser.add_argument(
        "--set-system-clock", action="store_true",
        help="Also step the SYSTEM clock onto GPS time, via the privileged helper at "
             f"{SET_CLOCK_HELPER}. Optional: log timestamps are GPS-corrected either "
             "way. Needs the one-time sudoers setup (docs/logging-and-playback.md).")
    args = parser.parse_args()

    global SET_SYSTEM_CLOCK
    SET_SYSTEM_CLOCK = args.set_system_clock

    os.makedirs(LOG_DIR, exist_ok=True)
    stats["start_monotonic"] = time.monotonic()

    print(f"NMEA Capture — source {args.ws_url}")
    print(f"Status page  — http://{args.bind}:{args.web_port}")
    print("Press Ctrl+C to stop.\n")

    web_thread = threading.Thread(target=start_web_server, args=(args.bind, args.web_port), daemon=True)
    web_thread.start()

    try:
        asyncio.run(capture_ws(args.ws_url))
    except KeyboardInterrupt:
        print("\nDone.")


if __name__ == "__main__":
    main()
