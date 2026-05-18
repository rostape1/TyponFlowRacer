#!/usr/bin/env python3
"""NMEA TCP-to-WebSocket proxy for browser access to boat instruments.

Standalone CLI for legacy use, plus a reusable `nmea_tcp_broadcast()` coroutine
that boat_server.py uses to feed aiohttp WebSocket clients.
"""

import argparse
import asyncio
import signal
import ssl

try:
    import websockets
except ImportError:
    websockets = None  # only required for the standalone CLI path

clients = set()
tcp_reader = None


async def nmea_tcp_broadcast(host, port, clients_iter, send_fn):
    """Connect to a TCP NMEA source and broadcast each line to all clients.

    Args:
        host, port: TCP source.
        clients_iter: zero-arg callable returning an iterable snapshot of
            currently connected clients (so callers can manage the set with
            their own connection lifecycle).
        send_fn: async callable `send_fn(client, text)` that delivers one
            NMEA line to one client. Exceptions are swallowed per-client.
    """
    while True:
        try:
            reader, _ = await asyncio.open_connection(host, port)
            print(f"Connected to NMEA source {host}:{port}")
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("ascii", errors="ignore").strip()
                if not text:
                    continue
                snapshot = list(clients_iter())
                if not snapshot:
                    continue
                await asyncio.gather(
                    *(send_fn(c, text) for c in snapshot),
                    return_exceptions=True,
                )
        except (ConnectionRefusedError, OSError) as e:
            print(f"TCP connection failed: {e}, retrying in 5s...")
        except asyncio.CancelledError:
            return
        await asyncio.sleep(5)


async def tcp_to_broadcast(host, port):
    """Standalone-CLI variant that broadcasts to the module-level `clients` set."""
    global tcp_reader

    async def _send(c, text):
        await c.send(text)

    while True:
        try:
            reader, _ = await asyncio.open_connection(host, port)
            tcp_reader = reader
            print(f"Connected to NMEA source {host}:{port}")
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("ascii", errors="ignore").strip()
                if text and clients:
                    await asyncio.gather(
                        *(c.send(text) for c in clients.copy()),
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

