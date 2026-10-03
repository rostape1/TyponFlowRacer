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
import collections
import signal
import socket
import ssl
import struct
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
# The receiver broadcasts, and the Pi sits on the boat LAN twice (wired .201 and
# WiFi .231), so every datagram arrives once per interface — the WiFi copy ~0.1 s
# late, held for the AP's beacon. Deliver whichever copy arrives first and drop
# the other interface's copy if it lands within this window. The two links lose
# different datagrams (WiFi ~3%, wired ~0.4%, measured 2026-09-30), so taking
# the first copy of each keeps a datagram whenever EITHER link carried it.
UDP_COPY_WINDOW_S = 0.5
# Datagrams remembered for copy matching. ~100/s x 0.5 s needs ~50; this bound
# only matters under a flood.
UDP_COPY_MAX = 4096
# Datagrams read per event-loop wakeup, so a burst can't starve the fan-out.
UDP_READ_BATCH = 256
_PKTINFO = getattr(socket, "IP_PKTINFO", None)
# struct in_pktinfo: ifindex (4 bytes) + spec_dst + addr — same on Linux and macOS.
_PKTINFO_ANCBUF = socket.CMSG_SPACE(12) if _PKTINFO is not None else 0
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
      shadowed, first_from             UDP only — second copies dropped, and which
                                       interface delivered each datagram first
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

    It also drops the second copy of each broadcast. The receiver broadcasts
    and the Pi is on the boat LAN twice, so each datagram arrives once per
    interface. A datagram is a copy only if the same bytes from the same source
    already arrived on a DIFFERENT interface within UDP_COPY_WINDOW_S. Dropping
    identical text regardless of interface would be wrong: at 20 Hz, heading
    and attitude repeat the same value legitimately. Sticking to one interface
    is wrong too — it throws away the other link's cover for its losses.
    """

    def __init__(self, queue, allow_from=None, status=None, clock=time.monotonic):
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
        self._clock = clock
        # (src, datagram bytes) -> [[arrival ifindex, {ifindexes whose copy it absorbed}], ...]
        # oldest first; _order holds the same entries in global arrival order.
        self._recent = {}
        self._order = collections.deque()
        self._shadowed = 0
        self._first_from = {}
        self._names = {}
        _set_status(status, first_from=self._first_from)

    def _is_copy(self, src, ifindex, data):
        """True if this datagram is another interface's copy of one already taken.

        Matching is one-for-one: two identical datagrams on eth0 absorb at most
        two copies from wlan0, so a legitimately repeated reading survives on
        the link that carried it. `None` (no IP_PKTINFO) is never a copy — the
        behaviour before this existed.
        """
        if ifindex is None:
            return False
        now = self._clock()
        order = self._order
        while order and (now - order[0][0] > UDP_COPY_WINDOW_S
                         or len(order) >= UDP_COPY_MAX):
            _t, old_key, _entry = order.popleft()
            # FIFO overall and per key, so the oldest entry for old_key is first.
            entries = self._recent[old_key]
            entries.pop(0)
            if not entries:
                del self._recent[old_key]
        key = (src, data)
        for entry in self._recent.get(key, ()):
            if entry[0] != ifindex and ifindex not in entry[1]:
                entry[1].add(ifindex)
                self._shadowed += 1
                _set_status(self._status, shadowed=self._shadowed)
                return True
        entry = [ifindex, set()]
        self._recent.setdefault(key, []).append(entry)
        order.append((now, key, entry))
        name = self._names.get(ifindex)
        if name is None:
            name = self._names[ifindex] = _iface_name(ifindex)
        # Which link actually leads. Mostly wired with a WiFi share of a few
        # percent means both links are up and covering for each other.
        self._first_from[name] = self._first_from.get(name, 0) + 1
        return False

    def datagram_received(self, data, addr, ifindex=None):
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

        if self._is_copy(src, ifindex, data):
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


def _iface_name(ifindex):
    if ifindex is None:
        return "any"
    try:
        return socket.if_indextoname(ifindex)
    except OSError:
        return f"if{ifindex}"


def _ifindex(ancdata):
    """Arrival interface from recvmsg() ancillary data, or None."""
    for level, kind, data in ancdata:
        if level == socket.IPPROTO_IP and kind == _PKTINFO and len(data) >= 4:
            return struct.unpack("=I", data[:4])[0]
    return None


def _open_udp_socket(bind_host, port):
    """Non-blocking UDP socket that reports each datagram's arrival interface.

    Our own socket rather than create_datagram_endpoint(), because asyncio's
    transport reads with recvfrom() and throws away the ancillary data that
    names the interface. Still no SO_REUSEADDR — see nmea_udp_broadcast().
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if _PKTINFO is not None:
            sock.setsockopt(socket.IPPROTO_IP, _PKTINFO, 1)
        sock.setblocking(False)
        sock.bind((bind_host, port))
    except BaseException:
        sock.close()
        raise
    return sock


def _drain(sock, proto):
    """Event-loop reader callback: hand queued datagrams to the protocol."""
    for _ in range(UDP_READ_BATCH):
        try:
            data, anc, _flags, addr = sock.recvmsg(65535, _PKTINFO_ANCBUF)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as e:
            proto.error_received(e)
            return
        proto.datagram_received(data, addr, _ifindex(anc))


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
                shadowed=0,
                allow_from=sorted(allow) if allow else None)
    while True:
        sock = None
        try:
            queue = asyncio.Queue(maxsize=UDP_QUEUE_MAX)
            # No SO_REUSEADDR. UDP has no TIME_WAIT, so a cleanly-exited
            # predecessor never blocks this bind — the flag would buy nothing
            # and cost the loudest signal available: on Linux it lets a second
            # process bind the same port, after which each datagram goes to
            # only one of them and both report "listening". A hard EADDRINUSE
            # surfaces a stale instance in /api/health instead.
            sock = _open_udp_socket(bind_host, port)
            proto = _NmeaDatagramProtocol(queue, allow, status)
            loop.add_reader(sock.fileno(), _drain, sock, proto)
            print(f"Listening for NMEA UDP on {bind_host}:{port}"
                  + (f" from {sorted(allow)}" if allow else " from any source")
                  + ("" if _PKTINFO is not None else
                     " (no IP_PKTINFO: broadcast copies are not deduplicated)"))
            _set_status(status, state="listening", last_error=None)
            while True:
                text = await queue.get()
                _set_status(status, queue_depth=queue.qsize())
                await _fanout(text, clients_iter, send_fn)
        except asyncio.CancelledError:
            _set_status(status, state="stopped")
            _close_udp(loop, sock)
            return
        except Exception as e:
            print(f"NMEA UDP listener error ({type(e).__name__}: {e}), retrying in 5s...")
            _set_status(status, state="retrying", last_error=f"{type(e).__name__}: {e}")
            _close_udp(loop, sock)
        await asyncio.sleep(5)


def _close_udp(loop, sock):
    if sock is None:
        return
    try:
        loop.remove_reader(sock.fileno())
    except Exception:
        pass
    sock.close()


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

