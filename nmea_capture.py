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


def ts():
    """Stored timestamp: UTC, with an explicit Z so it is self-describing.

    Stays UTC on purpose. It is unambiguous, DST-proof, comparable across the
    whole archive, and it is what nmea-client.js's replay parser expects. The Z
    is new: without it a line read `2026-09-13 19:03:12` while the filename said
    `120000` (local), which looked like a 7-hour inconsistency inside one file.
    Older logs have no Z and are parsed as UTC, which is what they are.

    Display is a separate concern — every UI surface renders these in local
    time and says so. See local_str().
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


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


def make_filename():
    return os.path.join(LOG_DIR, f"nmea_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.txt")


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


def log_sentence(sentence, source_url):
    global _current_hour, _outfile
    now = datetime.now()
    hour = now.strftime("%Y-%m-%d_%H")
    if hour != _current_hour:
        if _outfile:
            _outfile.close()
        filename = make_filename()
        _outfile = open(filename, "a", encoding="utf-8")
        _outfile.write(f"# NMEA capture started {ts()} (timestamps are UTC)\n")
        _outfile.write(f"# Source: {source_url}\n#\n")
        _outfile.flush()
        stats["current_file"] = filename
        _current_hour = hour
        print(f"Logging to {filename}")

    timestamp = ts()
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
    args = parser.parse_args()

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
