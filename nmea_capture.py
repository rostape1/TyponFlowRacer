#!/usr/bin/env python3
"""
NMEA capture with web status page — connects to a TCP source, logs all
sentences to hourly-rotated files, and serves a mobile-friendly status page.

Usage:
    python nmea_capture.py                             # default host:port
    python nmea_capture.py --host 192.168.47.10 --port 10110
    python nmea_capture.py --web-port 8080             # status page port
"""

import argparse
import glob
import html
import os
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    from config import AIS_HOST, AIS_PORT
except ImportError:
    AIS_HOST = "192.168.47.10"
    AIS_PORT = 10110

LOG_DIR = "logs"

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


def capture(host, port):
    stats["source"] = f"{host}:{port}"
    current_hour = None
    outfile = None

    while True:
        print(f"Connecting to {host}:{port}...")
        stats["connected"] = False
        try:
            sock = socket.create_connection((host, port), timeout=10)
            stats["connected"] = True
            print("Connected.")
            buf = b""
            with sock:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        print("Connection closed by remote.")
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        sentence = line.decode("ascii", errors="replace").strip()
                        if not sentence:
                            continue

                        now = datetime.now()
                        hour = now.strftime("%Y-%m-%d_%H")
                        if hour != current_hour:
                            if outfile:
                                outfile.close()
                            filename = make_filename()
                            outfile = open(filename, "a", encoding="utf-8")
                            outfile.write(f"# NMEA capture started {ts()} UTC\n")
                            outfile.write(f"# Source: tcp:{host}:{port}\n#\n")
                            outfile.flush()
                            stats["current_file"] = filename
                            current_hour = hour
                            print(f"Logging to {filename}")

                        timestamp = ts()
                        entry = f"{timestamp}  {sentence}\n"
                        print(entry, end="")
                        outfile.write(entry)
                        outfile.flush()
                        stats["sentences"] += 1
                        stats["recent"].append((timestamp, sentence))

        except (OSError, socket.timeout) as e:
            print(f"Error: {e}")

        stats["connected"] = False
        print("Reconnecting in 5s...")
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="NMEA capture with web status page")
    parser.add_argument("--host", default=AIS_HOST, help=f"TCP host (default: {AIS_HOST})")
    parser.add_argument("--port", type=int, default=AIS_PORT, help=f"TCP port (default: {AIS_PORT})")
    parser.add_argument("--web-port", type=int, default=8080, help="Status page HTTP port (default: 8080)")
    parser.add_argument("--bind", default="0.0.0.0", help="Status page bind address (default: 0.0.0.0)")
    args = parser.parse_args()

    os.makedirs(LOG_DIR, exist_ok=True)
    stats["start_time"] = time.time()

    print(f"NMEA Capture — source {args.host}:{args.port}")
    print(f"Status page  — http://{args.bind}:{args.web_port}")
    print("Press Ctrl+C to stop.\n")

    web_thread = threading.Thread(target=start_web_server, args=(args.bind, args.web_port), daemon=True)
    web_thread.start()

    try:
        capture(args.host, args.port)
    except KeyboardInterrupt:
        print("\nDone.")


if __name__ == "__main__":
    main()
