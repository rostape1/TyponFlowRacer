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
XPort's protocol setting is TCP *or* UDP, never both, so `boat_server.py` runs
both coroutines at once without double-counting: whichever one the receiver is
configured for wins, and the other stays silent. This module's own standalone
CLI (`tcp_to_broadcast`) remains TCP-only and is superseded.
"""

import argparse
import asyncio
import signal
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
UDP_BUFFER_MAX = 65536
# With --udp-allow-any, each source needs its own reassembly buffer or a partial
# sentence from one gets joined to the next datagram from another. Bound how many
# we will track so an address-spraying source can't grow the dict without limit.
UDP_MAX_SOURCES = 8
# Reconnect backoff for the TCP client. Once the receiver is switched to UDP the
# TCP path fails forever by design, so a fixed 5s retry would spin and log
# indefinitely. Start responsive (a real outage recovers in 5s) and decay.
RETRY_BASE_S = 5.0
RETRY_MAX_S = 60.0


def retry_delay(fails: int) -> float:
    """Seconds to wait before reconnect attempt number `fails` + 1."""
    if fails <= 1:
        return RETRY_BASE_S
    return min(RETRY_MAX_S, RETRY_BASE_S * (2 ** (fails - 1)))


def _set_status(status, **fields):
    """Record transport state for /api/health. No-op when no dict was passed.

    Keys, so the shape lives in one place rather than across five call sites:
      state       starting | connected (TCP) | listening (UDP) | retrying | stopped
      last_error  str | None
      attempts, last_connect_ts        TCP only
      dropped, queue_depth             UDP only — lines shed under backpressure
      rejected, rejected_from          UDP only — datagrams refused by the filter
    """
    if status is not None:
        status.update(fields)


async def _fanout(text, clients_iter, send_fn):
    """Deliver one line to every current client, dropping those that can't keep up."""
    snapshot = list(clients_iter())
    if not snapshot:
        return
    await asyncio.gather(
        *(_send_or_drop(c, text, send_fn) for c in snapshot),
        return_exceptions=True,
    )


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
            # Bound the close too. aiohttp's close() writes a close frame and
            # awaits the peer — on the very transport that just timed out, that
            # can block forever, and callers await this before pulling the next
            # line. One phone leaving WiFi would then stop the whole feed, and
            # the logger is itself a /nmea client sitting behind this path.
            await asyncio.wait_for(client.close(), timeout=SEND_TIMEOUT_S)
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
    fails = 0
    _set_status(status, state="starting", last_error=None, last_connect_ts=None,
                attempts=0)
    while True:
        writer = None
        failed = True
        try:
            attempts += 1
            _set_status(status, attempts=attempts)
            reader, writer = await asyncio.open_connection(host, port)
            print(f"Connected to NMEA source {host}:{port}")
            fails = 0
            _set_status(status, state="connected", last_error=None,
                        last_connect_ts=time.time())
            while True:
                line = await _read_line(reader)
                if line is None:
                    reason = f"no data for {READ_TIMEOUT_S:.0f}s"
                    break
                if not line:
                    reason = "receiver closed the connection"
                    break
                text = line.decode("ascii", errors="ignore").strip()
                if not text:
                    continue
                await _fanout(text, clients_iter, send_fn)
            failed = False
            _set_status(status, state="retrying", last_error=reason)
        except asyncio.CancelledError:
            _set_status(status, state="stopped")
            return
        except OSError as e:
            # ConnectionRefusedError is an OSError. A refusal here is normal and
            # expected: the XPort serves one client at a time, so this is what
            # "someone else has the slot" and "the box is wedged" both look like.
            # It is ALSO the steady state once the receiver is switched to UDP,
            # which is why this path must not log on every retry.
            _set_status(status, state="retrying", last_error=f"{type(e).__name__}: {e}")
        except Exception as e:
            # This loop feeds the WebSocket fan-out AND, through it, the logger.
            # If it dies the boat looks identical to a silent receiver, which is
            # the failure `P39` was written about — so nothing gets to kill it.
            print(f"NMEA TCP bridge error ({type(e).__name__}: {e}), retrying...")
            _set_status(status, state="retrying", last_error=f"{type(e).__name__}: {e}")
        finally:
            # Against a single-client receiver, reconnecting without closing
            # orphans our OWN session: the old socket stays ESTABLISHED because
            # the event loop still holds the transport, and every retry is then
            # refused until the process restarts. That is `P40`'s lockout, with
            # no power cut needed.
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        if failed:
            fails += 1
        delay = retry_delay(fails)
        # Log only when the backoff step changes. With the receiver in UDP mode
        # this loop fails forever by design, and a line every 5s would put ~17k
        # entries a day into startup.log — which lives in the same directory as
        # the race recordings and has no rotation (`P11`, `P34`).
        if failed and delay != retry_delay(fails - 1):
            print(f"NMEA TCP source unreachable ({fails} attempts): "
                  f"{(status or {}).get('last_error')} — backing off to {delay:.0f}s")
        _set_status(status, consecutive_failures=fails, retry_in_s=delay)
        await asyncio.sleep(delay)


class _NmeaDatagramProtocol(asyncio.DatagramProtocol):
    """Reassemble NMEA lines out of UDP datagrams onto a queue.

    A datagram is not a line. An XPort in datagram mode ships whatever the
    serial side produced before its flush trigger fired, so one packet may
    carry several sentences, or split one across two. A remainder is therefore
    carried between packets — per source address, because with the filter off
    two devices interleaving would otherwise splice one's tail onto the other's
    head. If a packet is lost the join still yields one malformed sentence,
    which is why `nmea-parser.js` validates the checksum on AIS sentences too
    (`P40`).
    """

    def __init__(self, queue, allow_from=None, status=None):
        self._queue = queue
        # None means "accept anything". Normalise here rather than trusting the
        # caller: a bare string would turn the membership test below into a
        # substring match, which quietly accepts the wrong sources.
        if allow_from is None:
            self._allow_from = None
        elif isinstance(allow_from, str):
            self._allow_from = {allow_from}
        else:
            self._allow_from = set(allow_from)
        self._status = status
        self._bufs = {}
        self._warned = {}
        self._rejected = 0
        self._dropped = 0

    def datagram_received(self, data, addr):
        src = addr[0]
        if self._allow_from is not None and src not in self._allow_from:
            # Position and AIS data steer a boat. Take it only from the
            # configured receiver — and make the refusal visible in
            # /api/health, because "rejecting every datagram" and "receiver is
            # silent" are otherwise the same reading, which is the `P39`
            # failure this endpoint exists to prevent.
            self._rejected += 1
            _set_status(self._status, rejected=self._rejected, rejected_from=src)
            now = time.monotonic()
            if now - self._warned.get(src, -1e9) > 60:
                if len(self._warned) > UDP_MAX_SOURCES:
                    self._warned.clear()
                self._warned[src] = now
                print(f"Ignoring NMEA UDP from {src} "
                      f"(expected {sorted(self._allow_from)})")
            return

        buf = self._bufs.get(src, b"") + data
        # Split first, then bound only the unterminated remainder. Bounding the
        # whole buffer would discard the complete sentences sitting in front of
        # it and resync mid-sentence, manufacturing exactly the spliced line
        # this class exists to avoid producing.
        *lines, remainder = buf.split(b"\n")
        if len(remainder) > UDP_BUFFER_MAX:
            print(f"Discarding {len(remainder)}B unterminated NMEA UDP remainder from {src}")
            remainder = b""
        if len(self._bufs) >= UDP_MAX_SOURCES and src not in self._bufs:
            self._bufs.clear()
        self._bufs[src] = remainder
        for raw in lines:
            text = raw.decode("ascii", errors="ignore").strip()
            if not text:
                continue
            try:
                self._queue.put_nowait(text)
            except asyncio.QueueFull:
                # Shed the OLDEST line, not the newest. Keeping a full queue of
                # stale positions while discarding fresh ones would walk
                # own-ship through a 40-second-old track while /api/health
                # showed lines flowing and last_line_age_s near zero — "looks
                # live and isn't", with a green light on it.
                try:
                    self._queue.get_nowait()
                except Exception:
                    pass
                self._dropped += 1
                _set_status(self._status, dropped=self._dropped)
                if self._dropped % 100 == 1:
                    print(f"NMEA UDP queue full, shed {self._dropped} lines")
                try:
                    self._queue.put_nowait(text)
                except Exception:
                    pass

    def error_received(self, exc):
        # ICMP port-unreachable and friends. Not fatal for a listener.
        _set_status(self._status, last_error=f"{type(exc).__name__}: {exc}")


async def nmea_udp_broadcast(port, clients_iter, send_fn, bind_host="0.0.0.0",
                             allow_from=None, status=None):
    """Listen for NMEA over UDP and broadcast each line to all clients.

    The point of this transport is that it has no session and no single-client
    slot, so cutting the Pi's power can never leave anything claimed on the
    receiver. Same `clients_iter`/`send_fn` contract as the TCP path.

    Args:
        allow_from: iterable of acceptable source IPs, or None to accept any.
            Callers must resolve hostnames first — a name compared against a
            dotted quad matches nothing and silently discards the whole feed.
    """
    loop = asyncio.get_running_loop()
    allow = set(allow_from) if allow_from is not None else None
    _set_status(status, state="starting", last_error=None, dropped=0, rejected=0,
                allow_from=sorted(allow) if allow else None)
    while True:
        transport = None
        try:
            queue = asyncio.Queue(maxsize=UDP_QUEUE_MAX)
            # No SO_REUSEADDR. UDP has no TIME_WAIT, so a cleanly-exited
            # predecessor never blocks this bind — the flag would buy nothing
            # and cost the loudest signal available: on Linux it lets a second
            # process bind the same port, after which each datagram goes to
            # only one of them and both report "listening". A hard EADDRINUSE
            # surfaces a stale instance in /api/health instead.
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _NmeaDatagramProtocol(queue, allow, status),
                local_addr=(bind_host, port),
            )
            print(f"Listening for NMEA UDP on {bind_host}:{port}"
                  + (f" from {sorted(allow)}" if allow else " from any source"))
            _set_status(status, state="listening", last_error=None)
            while True:
                text = await queue.get()
                _set_status(status, queue_depth=queue.qsize())
                await _fanout(text, clients_iter, send_fn)
        except asyncio.CancelledError:
            _set_status(status, state="stopped")
            if transport:
                transport.close()
            return
        except Exception as e:
            print(f"NMEA UDP listener error ({type(e).__name__}: {e}), retrying in 5s...")
            _set_status(status, state="retrying", last_error=f"{type(e).__name__}: {e}")
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

