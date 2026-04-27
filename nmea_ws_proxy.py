#!/usr/bin/env python3
"""NMEA TCP-to-WebSocket proxy for browser access to boat instruments."""

import argparse
import asyncio
import signal

try:
    import websockets
except ImportError:
    print("Install websockets: pip install websockets")
    raise SystemExit(1)

clients = set()
tcp_reader = None


async def tcp_to_broadcast(host, port):
    global tcp_reader
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


async def main(tcp_host, tcp_port, ws_port):
    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set_result, None)

    tcp_task = asyncio.create_task(tcp_to_broadcast(tcp_host, tcp_port))
    async with websockets.serve(ws_handler, "0.0.0.0", ws_port):
        print(f"WebSocket proxy on ws://0.0.0.0:{ws_port}")
        await stop

    tcp_task.cancel()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="NMEA TCP→WebSocket proxy")
    p.add_argument("--tcp-host", default="192.168.47.10")
    p.add_argument("--tcp-port", type=int, default=10110)
    p.add_argument("--ws-port", type=int, default=8765)
    args = p.parse_args()
    asyncio.run(main(args.tcp_host, args.tcp_port, args.ws_port))
