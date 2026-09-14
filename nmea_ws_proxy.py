#!/usr/bin/env python3
"""NMEA-to-WebSocket proxy for browser access to boat instruments.

Standalone CLI for legacy use, plus reusable `nmea_tcp_broadcast()` and
`nmea_udp_broadcast()` coroutines that boat_server.py uses to feed aiohttp
WebSocket clients.

Two transports exist because the receiver's Lantronix XPort serves TCP to
**one client at a time** and refuses everyone else. An abrupt Pi power-off
sends no FIN, so the slot can stay claimed by a session nobody owns, and the
Pi is then locked out for an unbounded time (see `P40`). UDP datagram mode has
no session and no slot, so no client's power state can wedge the feed. The
XPort's protocol setting is TCP *or* UDP, never both, so running both listeners
here cannot double-count: whichever one the receiver is configured for wins,
and the other stays silent.
"""

import argparse
import asyncio
import signal
import socket
import ssl
import time

try:
    import websockets
except ImportError:
    websockets = None  # only required for the standalone CLI path

clients = set()
tcp_reader = None

# One stalled client must not hold up the feed for everyone else: a phone walking
# out of WiFi range is routine on a boat.
SEND_TIMEOUT_S = 2.0
# A silent TCP peer looks identical to a healthy idle one, so bound the read and
# reconnect instead of blocking forever.
READ_TIMEOUT_S = 30.0
# UDP is connectionless, so the kernel buffer is the only backstop between a
# burst on the wire and a slow WebSocket client. Bound our own queue too, and
# count what we shed: silently losing sentences on a navigation feed is exactly
# the "looks live and isn't" failure this repo exists to avoid.
UDP_QUEUE_MAX = 2000
# A source that never sends a line terminator must not grow the reassembly
# buffer without bound. One NMEA sentence is 82 bytes by spec; this is generous.
UDP_MAX_BUFFER = 65536


def _note(status, **fields):
    """Record transport state for /api/health. No-op when no dict was passed."""
    if status is not None:
        status.update(fields)


async def _ws_send(client, text):
    """Default send shape for the `websockets` library clients."""
    await client.send(text)


async def _send_or_drop(client, text, send_fn):
    """Deliver one line to one client; drop the client if it can't keep up."""
    try:
        await asyncio.wait_for(send_fn(client, text), timeout=SEND_TIMEOUT_S)
    except asyncio.TimeoutError:
        print(f"Dropping stalled NMEA client after {SEND_TIMEOUT_S:.0f}s")
        try:
            await client.close()
        except Exception:
            pass
    except Exception:
        pass  # per-client failures are swallowed by contract


async def _read_line(reader):
    """One line from the TCP source.

    Returns bytes, or None when the connection should be torn down and retried.
    Oversized unterminated lines (past readline()'s 64 KiB limit) are discarded
    rather than allowed to kill the bridge permanently.
    """
    while True:
        try:
            return await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT_S)
        except asyncio.TimeoutError:
            print(f"No NMEA data for {READ_TIMEOUT_S:.0f}s, reconnecting...")
            return None
        except (ValueError, asyncio.LimitOverrunError) as e:
            print(f"Discarding oversized NMEA line: {e}")


async def nmea_tcp_broadcast(host, port, clients_iter, send_fn, status=None):
    """Connect to a TCP NMEA source and broadcast each line to all clients.

    Args:
        host, port: TCP source.
        clients_iter: zero-arg callable returning an iterable snapshot of
            currently connected clients (so callers can manage the set with
            their own connection lifecycle).
        send_fn: async callable `send_fn(client, text)` that delivers one
            NMEA line to one client. Exceptions are swallowed per-client, and a
            client that takes longer than SEND_TIMEOUT_S is closed and dropped.
        status: optional dict, updated in place with connection state so
            /api/health can tell "receiver refusing" from "receiver silent"
            from "bridge dead" (`P39`, `P40`).
    """
    attempts = 0
    _note(status, state="starting", last_error=None, last_connect_ts=None, attempts=0)
    while True:
        try:
            attempts += 1
            _note(status, attempts=attempts)
            reader, _ = await asyncio.open_connection(host, port)
            print(f"Connected to NMEA source {host}:{port}")
            _note(status, state="connected", last_error=None,
                  last_connect_ts=time.time())
            while True:
                line = await _read_line(reader)
                if not line:
                    break
                text = line.decode("ascii", errors="ignore").strip()
                if not text:
                    continue
                snapshot = list(clients_iter())
                if not snapshot:
                    continue
                await asyncio.gather(
                    *(_send_or_drop(c, text, send_fn) for c in snapshot),
                    return_exceptions=True,
                )
            _note(status, state="retrying", last_error="source went silent")
        except asyncio.CancelledError:
            _note(status, state="stopped")
            return
        except OSError as e:
            # ConnectionRefusedError is an OSError. A refusal here is normal and
            # expected: the XPort serves one client at a time, so this is what
            # "someone else has the slot" and "the box is wedged" both look like.
            print(f"TCP connection failed: {e}, retrying in 5s...")
            _note(status, state="retrying", last_error=f"{type(e).__name__}: {e}")
        except Exception as e:
            # This loop feeds the WebSocket fan-out AND, through it, the logger.
            # If it dies the boat looks identical to a silent receiver, which is
            # the failure `P39` was written about — so nothing gets to kill it.
            print(f"NMEA TCP bridge error ({type(e).__name__}: {e}), retrying in 5s...")
            _note(status, state="retrying", last_error=f"{type(e).__name__}: {e}")
        await asyncio.sleep(5)


class _NmeaDatagramProtocol(asyncio.DatagramProtocol):
    """Reassemble NMEA lines out of UDP datagrams onto a queue.

    A datagram is not a line. An XPort in datagram mode ships whatever the
    serial side produced before its flush trigger fired, so one packet may
    carry several sentences, or split one across two packets. Carrying a
    remainder between packets handles the split case; if a packet is lost the
    join produces one malformed sentence, which is why every consumer must keep
    validating the NMEA checksum rather than trusting line framing.
    """

    def __init__(self, queue, allow_from=None, status=None):
        self._queue = queue
        self._allow_from = allow_from
        self._status = status
        self._buf = b""
        self._warned_source = None
        self._dropped = 0

    def datagram_received(self, data, addr):
        if self._allow_from and addr[0] != self._allow_from:
            # Position and AIS data steer a boat. Take it only from the
            # configured receiver, and say so once per offending source rather
            # than filling the log.
            if self._warned_source != addr[0]:
                self._warned_source = addr[0]
                print(f"Ignoring NMEA UDP from {addr[0]} "
                      f"(expected {self._allow_from})")
            return
        self._buf += data
        if len(self._buf) > UDP_MAX_BUFFER:
            print(f"Discarding {len(self._buf)}B unterminated NMEA UDP buffer")
            self._buf = b""
            return
        *lines, self._buf = self._buf.split(b"\n")
        for raw in lines:
            text = raw.decode("ascii", errors="ignore").strip()
            if not text:
                continue
            try:
                self._queue.put_nowait(text)
            except asyncio.QueueFull:
                self._dropped += 1
                _note(self._status, dropped=self._dropped)
                if self._dropped % 100 == 1:
                    print(f"NMEA UDP queue full, dropped {self._dropped} lines")

    def error_received(self, exc):
        # ICMP port-unreachable and friends. Not fatal for a listener.
        _note(self._status, last_error=f"{type(exc).__name__}: {exc}")


async def nmea_udp_broadcast(port, clients_iter, send_fn, bind_host="0.0.0.0",
                             allow_from=None, status=None):
    """Listen for NMEA over UDP and broadcast each line to all clients.

    The point of this transport is that it has no session and no single-client
    slot, so cutting the Pi's power can never leave anything claimed on the
    receiver. Same `clients_iter`/`send_fn` contract as the TCP path.

    Args:
        allow_from: if set, only accept datagrams from this source IP.
    """
    loop = asyncio.get_running_loop()
    _note(status, state="starting", last_error=None, dropped=0)
    while True:
        transport = None
        try:
            queue = asyncio.Queue(maxsize=UDP_QUEUE_MAX)
            # Bind by hand: asyncio's reuse_address kwarg is deprecated, and
            # without SO_REUSEADDR a restart can hit EADDRINUSE on the socket
            # the previous process just released.
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((bind_host, port))
            sock.setblocking(False)
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _NmeaDatagramProtocol(queue, allow_from, status),
                sock=sock,
            )
            print(f"Listening for NMEA UDP on {bind_host}:{port}"
                  + (f" from {allow_from}" if allow_from else ""))
            _note(status, state="listening", last_error=None)
            while True:
                text = await queue.get()
                snapshot = list(clients_iter())
                if not snapshot:
                    continue
                await asyncio.gather(
                    *(_send_or_drop(c, text, send_fn) for c in snapshot),
                    return_exceptions=True,
                )
        except asyncio.CancelledError:
            _note(status, state="stopped")
            if transport:
                transport.close()
            return
        except Exception as e:
            print(f"NMEA UDP listener error ({type(e).__name__}: {e}), retrying in 5s...")
            _note(status, state="retrying", last_error=f"{type(e).__name__}: {e}")
            if transport:
                transport.close()
        await asyncio.sleep(5)


async def tcp_to_broadcast(host, port):
    """Standalone-CLI variant that broadcasts to the module-level `clients` set."""
    global tcp_reader

    while True:
        try:
            reader, _ = await asyncio.open_connection(host, port)
            tcp_reader = reader
            print(f"Connected to NMEA source {host}:{port}")
            while True:
                line = await _read_line(reader)
                if not line:
                    break
                text = line.decode("ascii", errors="ignore").strip()
                if text and clients:
                    await asyncio.gather(
                        *(_send_or_drop(c, text, _ws_send) for c in clients.copy()),
                        return_exceptions=True,
                    )
        except (ConnectionRefusedError, OSError) as e:
            print(f"TCP connection failed: {e}, retrying in 5s...")
        except asyncio.CancelledError:
            return
        tcp_reader = None
        await asyncio.sleep(5)


async def ws_handler(ws):
    clients.add(ws)
    print(f"Browser connected ({len(clients)} clients)")
    try:
        async for _ in ws:
            pass
    finally:
        clients.discard(ws)
        print(f"Browser disconnected ({len(clients)} clients)")


async def main(tcp_host, tcp_port, ws_port, wss_port, ssl_cert, ssl_key):
    if websockets is None:
        print("Install websockets: pip install websockets")
        raise SystemExit(1)
    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set_result, None)

    tcp_task = asyncio.create_task(tcp_to_broadcast(tcp_host, tcp_port))

    ws_server = await websockets.serve(ws_handler, "0.0.0.0", ws_port)
    print(f"WebSocket proxy on ws://0.0.0.0:{ws_port}")

    wss_server = None
    if ssl_cert and ssl_key:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(ssl_cert, ssl_key)
        wss_server = await websockets.serve(ws_handler, "0.0.0.0", wss_port, ssl=ssl_ctx)
        print(f"Secure WebSocket proxy on wss://0.0.0.0:{wss_port}")

    await stop

    ws_server.close()
    if wss_server:
        wss_server.close()
    tcp_task.cancel()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="NMEA TCP→WebSocket proxy")
    p.add_argument("--tcp-host", default="192.168.47.10")
    p.add_argument("--tcp-port", type=int, default=10110)
    p.add_argument("--ws-port", type=int, default=8765)
    p.add_argument("--wss-port", type=int, default=8766)
    p.add_argument("--ssl-cert", default=None)
    p.add_argument("--ssl-key", default=None)
    args = p.parse_args()
    asyncio.run(main(args.tcp_host, args.tcp_port, args.ws_port,
                      args.wss_port, args.ssl_cert, args.ssl_key))

