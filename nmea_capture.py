#!/usr/bin/env python3
"""
NMEA capture — taps the boat server's WebSocket (/nmea) and logs all
sentences to hourly-rotated files. Serves a mobile-friendly status page.

Reads from the boat server WebSocket so it doesn't compete with the
server for the single TCP connection from the instruments.

Usage:
    python nmea_capture.py                        # default ws url
    python nmea_capture.py --ws-url wss://localhost:8443/nmea
    python nmea_capture.py --web-port 8080
"""

import argparse
import asyncio
import glob
import html
import os
import ssl
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    import websockets
except ImportError:
    raise SystemExit("Install websockets: pip install websockets")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
KEEP_DAYS = 28

stats = {
    "start_time": None,
    "sentences": 0,
    "current_file": "",
    "connected": False,
    "source": "",
    "recent": deque(maxlen=10),
}


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def make_filename():
    return os.path.join(LOG_DIR, f"nmea_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.txt")


def count_log_files():
    return len(glob.glob(os.path.join(LOG_DIR, "nmea_*.txt")))


def cleanup_old_logs():
    cutoff = time.time() - KEEP_DAYS * 86400
    for path in glob.glob(os.path.join(LOG_DIR, "nmea_*.txt")):
        if os.path.getmtime(path) < cutoff:
            try:
                os.remove(path)
                print(f"Deleted old log: {path}")
            except OSError:
                pass


def cleanup_loop():
    while True:
        cleanup_old_logs()
        time.sleep(3600)


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
    now = time.time()
    uptime = format_uptime(now - stats["start_time"]) if stats["start_time"] else "—"
    conn_dot = "🟢" if stats["connected"] else "🔴"
    conn_text = "Connected" if stats["connected"] else "Disconnected"
    recent_lines = ""
    for t, sentence in reversed(stats["recent"]):
        recent_lines += f"<div class='line'><span class='ts'>{html.escape(t)}</span> {html.escape(sentence)}</div>\n"
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
 <div class="row"><span class="label">Log files</span><span class="value">{count_log_files()}</span></div>
 <div class="row"><span class="label">Current file</span><span class="value" style="font-size:0.8em">{html.escape(os.path.basename(stats['current_file']))}</span></div>
</div>
<div class="card recent">
 <h2>Recent sentences</h2>
 {recent_lines}
</div>
<div class="footer">Auto-refreshes every 5s</div>
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
        _outfile.write(f"# NMEA capture started {ts()} UTC\n")
        _outfile.write(f"# Source: {source_url}\n#\n")
        _outfile.flush()
        stats["current_file"] = filename
        _current_hour = hour
        print(f"Logging to {filename}")

    timestamp = ts()
    entry = f"{timestamp}  {sentence}\n"
    _outfile.write(entry)
    _outfile.flush()
    stats["sentences"] += 1
    stats["recent"].append((timestamp, sentence))


_current_hour = None
_outfile = None


async def capture_ws(ws_url):
    stats["source"] = ws_url
    if ws_url.startswith("wss://"):
        ssl_ctx = ssl.create_default_context()
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
        except Exception as e:
            print(f"WebSocket error: {e}")

        stats["connected"] = False
        print("Reconnecting in 5s...")
        await asyncio.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="NMEA capture via boat server WebSocket")
    parser.add_argument("--ws-url", default="wss://localhost:8443/nmea",
                        help="Boat server NMEA WebSocket URL (default: wss://localhost:8443/nmea)")
    parser.add_argument("--web-port", type=int, default=8080, help="Status page HTTP port (default: 8080)")
    parser.add_argument("--bind", default="0.0.0.0", help="Status page bind address (default: 0.0.0.0)")
    args = parser.parse_args()

    os.makedirs(LOG_DIR, exist_ok=True)
    stats["start_time"] = time.time()

    print(f"NMEA Capture — source {args.ws_url}")
    print(f"Status page  — http://{args.bind}:{args.web_port}")
    print("Press Ctrl+C to stop.\n")

    web_thread = threading.Thread(target=start_web_server, args=(args.bind, args.web_port), daemon=True)
    web_thread.start()

    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()

    try:
        asyncio.run(capture_ws(args.ws_url))
    except KeyboardInterrupt:
        print("\nDone.")


if __name__ == "__main__":
    main()
