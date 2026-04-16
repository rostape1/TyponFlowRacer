#!/usr/bin/env python3
"""
NMEA Logger — scans the boat network for NMEA sources, then logs everything
from all discovered TCP ports + UDP listeners into a single timestamped file.

Scan phase:
  1. ARP scan to find devices on the subnet (or just the configured host)
  2. TCP port scan on each device (common NMEA ports + a wider range)
  3. UDP listen on common NMEA broadcast ports
  4. Connects to ALL discovered sources simultaneously

Usage:
    python nmea_logger.py                      # scan configured host, log all
    python nmea_logger.py --scan-subnet        # scan entire 192.168.47.0/24
    python nmea_logger.py --host 192.168.47.10 # scan just this host
    python nmea_logger.py --skip-scan          # skip scan, use configured host:port only
    python nmea_logger.py -o my_log.txt        # custom output file
"""

import argparse
import asyncio
import ipaddress
import signal
import socket
from datetime import datetime, timezone

from config import AIS_HOST, AIS_PORT, AIS_PROTOCOL

# Common NMEA/marine TCP ports
COMMON_PORTS = [
    10110,  # Standard NMEA-0183 over TCP
    10111, 10112, 10113, 10114, 10115,  # Additional NMEA ports
    2000,   # Actisense / some MFDs
    2947,   # gpsd
    4712,   # NMEA over IP (IEC 61162-450)
    6543,   # Vesper AIS
    7777,   # some chart plotters
    8375,   # Garmin
    10001,  # generic serial-to-IP
    20000,  # some multiplexers
    23,     # telnet (some devices serve NMEA here)
    39150, 39151, 39152, 39153,  # SignalK / common WiFi NMEA bridges
]

# Common UDP broadcast ports for NMEA
UDP_PORTS = [10110, 10111, 10112, 2000, 4712, 7777, 39150, 39151, 39152]


def make_log_filename():
    now = datetime.now()
    return f"nmea_{now.strftime('%Y-%m-%d_%H%M%S')}.txt"


def ts_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------------------
# Network scanning
# ---------------------------------------------------------------------------

async def find_hosts_on_subnet(subnet_str):
    """ARP-style scan: try to TCP-connect to port 80/10110 on each IP to find live hosts."""
    network = ipaddress.IPv4Network(subnet_str, strict=False)
    print(f"  Scanning subnet {network} for live hosts...")
    live = []

    async def probe(ip):
        ip_str = str(ip)
        for port in [10110, 80, 23, 2000]:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(ip_str, port), timeout=0.5
                )
                writer.close()
                await writer.wait_closed()
                return ip_str
            except Exception:
                continue
        return None

    tasks = [probe(ip) for ip in network.hosts()]
    results = await asyncio.gather(*tasks)
    live = [r for r in results if r is not None]
    return live


async def scan_tcp_port(host, port, timeout=1.5):
    """Check if a TCP port is open and serving NMEA-like data."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        # Try to read a line to confirm it's sending NMEA
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            data = line.decode("ascii", errors="replace").strip()
            writer.close()
            await writer.wait_closed()
            if data:
                return (host, port, data)
        except asyncio.TimeoutError:
            # Port is open but nothing came — might still be valid
            writer.close()
            await writer.wait_closed()
            return (host, port, "(open, no data in 2s)")
    except Exception:
        return None


async def scan_host_ports(host, ports):
    """Scan a list of TCP ports on a single host."""
    tasks = [scan_tcp_port(host, p) for p in ports]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]


async def probe_udp_ports(ports, listen_time=3.0):
    """Listen briefly on each UDP port to see if anything arrives."""
    found = []

    for port in ports:
        try:
            got_data = asyncio.Event()
            sample = {}

            class Probe(asyncio.DatagramProtocol):
                def datagram_received(self, data, addr):
                    text = data.decode("ascii", errors="replace").strip()
                    sample["addr"] = addr
                    sample["data"] = text
                    got_data.set()

                def error_received(self, exc):
                    pass

            loop = asyncio.get_event_loop()
            transport, _ = await loop.create_datagram_endpoint(
                Probe, local_addr=("0.0.0.0", port), allow_broadcast=True
            )
            try:
                await asyncio.wait_for(got_data.wait(), timeout=listen_time)
                found.append((sample["addr"][0], port, "udp", sample.get("data", "")))
            except asyncio.TimeoutError:
                pass
            finally:
                transport.close()
        except OSError:
            # Port already in use
            pass

    return found


async def run_scan(hosts, scan_subnet=False, subnet=None):
    """
    Scan for NMEA sources. Returns:
      tcp_sources: list of (host, port, sample_sentence)
      udp_sources: list of (host, port, sample_sentence)
    """
    print("\n=== NMEA Network Scan ===\n")

    # Step 1: Find live hosts
    all_hosts = list(hosts)
    if scan_subnet and subnet:
        discovered = await find_hosts_on_subnet(subnet)
        for h in discovered:
            if h not in all_hosts:
                all_hosts.append(h)
        print(f"  Found {len(discovered)} live hosts: {', '.join(discovered) if discovered else 'none'}")
    else:
        print(f"  Scanning host(s): {', '.join(all_hosts)}")

    # Step 2: TCP port scan on each host
    print(f"\n  Scanning {len(COMMON_PORTS)} common NMEA TCP ports per host...")
    tcp_sources = []
    for host in all_hosts:
        results = await scan_host_ports(host, COMMON_PORTS)
        for host, port, sample in results:
            tcp_sources.append((host, port, sample))

    if tcp_sources:
        print(f"\n  TCP sources found:")
        for host, port, sample in tcp_sources:
            preview = sample[:70] if len(sample) > 70 else sample
            print(f"    {host}:{port}  ->  {preview}")
    else:
        print(f"\n  No TCP NMEA sources found.")

    # Step 3: UDP probe
    print(f"\n  Listening on {len(UDP_PORTS)} UDP ports for 3 seconds each...")
    udp_sources = await probe_udp_ports(UDP_PORTS, listen_time=3.0)

    if udp_sources:
        print(f"\n  UDP sources found:")
        for host, port, proto, sample in udp_sources:
            preview = sample[:70] if len(sample) > 70 else sample
            print(f"    :{port} (from {host})  ->  {preview}")
    else:
        print(f"\n  No UDP NMEA broadcasts detected.")

    # Summary
    total = len(tcp_sources) + len(udp_sources)
    print(f"\n  Total: {len(tcp_sources)} TCP + {len(udp_sources)} UDP = {total} source(s)\n")
    print("=" * 40)

    return tcp_sources, udp_sources


# ---------------------------------------------------------------------------
# Logging (multi-source)
# ---------------------------------------------------------------------------

async def log_tcp(host, port, outfile, label=None):
    """Connect to one TCP source and log forever with reconnect."""
    tag = label or f"tcp:{host}:{port}"
    while True:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            print(f"  [{tag}] Connected")

            while True:
                line = await reader.readline()
                if not line:
                    print(f"  [{tag}] EOF — reconnecting...")
                    break
                sentence = line.decode("ascii", errors="replace").strip()
                if sentence:
                    entry = f"{ts_now()}  [{tag}]  {sentence}\n"
                    outfile.write(entry)
                    outfile.flush()

        except (ConnectionRefusedError, ConnectionResetError, OSError) as e:
            print(f"  [{tag}] Error: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        await asyncio.sleep(5)


class _MultiUdpLogger(asyncio.DatagramProtocol):
    def __init__(self, port, outfile):
        self.port = port
        self.outfile = outfile
        self.buffer = ""

    def connection_made(self, transport):
        print(f"  [udp::{self.port}] Listening")

    def datagram_received(self, data, addr):
        tag = f"udp:{addr[0]}:{self.port}"
        text = data.decode("ascii", errors="replace")
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            sentence = line.strip()
            if sentence:
                entry = f"{ts_now()}  [{tag}]  {sentence}\n"
                self.outfile.write(entry)
                self.outfile.flush()

    def error_received(self, exc):
        print(f"  [udp::{self.port}] Error: {exc}")


async def log_udp(port, outfile):
    """Listen on one UDP port and log forever."""
    loop = asyncio.get_event_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _MultiUdpLogger(port, outfile),
        local_addr=("0.0.0.0", port),
        allow_broadcast=True,
    )
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        transport.close()


async def run(args):
    filename = args.output or make_log_filename()
    outfile = open(filename, "a", encoding="utf-8")

    print(f"NMEA Logger")
    print(f"Output: {filename}")
    print(f"Press Ctrl+C to stop.\n")

    tcp_sources = []
    udp_ports_to_listen = set()

    if args.skip_scan:
        # Just use the configured host:port
        tcp_sources = [(args.host, args.port, "")]
        print(f"  Skipping scan — connecting to {args.host}:{args.port}")
    else:
        # Run network scan
        subnet = None
        if args.scan_subnet:
            # Derive /24 subnet from host IP
            subnet = str(ipaddress.IPv4Network(f"{args.host}/24", strict=False))

        found_tcp, found_udp = await run_scan(
            hosts=[args.host],
            scan_subnet=args.scan_subnet,
            subnet=subnet,
        )
        tcp_sources = [(h, p, s) for h, p, s in found_tcp]
        for h, p, proto, s in found_udp:
            udp_ports_to_listen.add(p)

        # If scan found nothing, fall back to configured host:port
        if not tcp_sources and not udp_ports_to_listen:
            print(f"  No sources found — falling back to {args.host}:{args.port}")
            tcp_sources = [(args.host, args.port, "")]

    # Also listen on UDP for the configured port and common ports
    # (even if TCP was found — some instruments may only broadcast UDP)
    if not args.skip_scan:
        udp_ports_to_listen.add(args.port)
        # Add a few extra common ones
        for p in [10110, 10111, 2000, 39150]:
            udp_ports_to_listen.add(p)

    # Write header
    header = f"# NMEA Log started {ts_now()} UTC\n"
    header += f"# TCP sources: {', '.join(f'{h}:{p}' for h, p, _ in tcp_sources)}\n"
    header += f"# UDP ports: {', '.join(str(p) for p in sorted(udp_ports_to_listen))}\n"
    header += "#\n"
    outfile.write(header)
    outfile.flush()

    # Launch all loggers concurrently
    print(f"\n  Starting {len(tcp_sources)} TCP + {len(udp_ports_to_listen)} UDP loggers...\n")
    tasks = []

    for host, port, _ in tcp_sources:
        tasks.append(asyncio.create_task(log_tcp(host, port, outfile)))

    for port in sorted(udp_ports_to_listen):
        tasks.append(asyncio.create_task(log_udp(port, outfile)))

    # Wait forever (until cancelled)
    await asyncio.gather(*tasks)


def main():
    parser = argparse.ArgumentParser(
        description="Scan for NMEA sources and log all sentences to a file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python nmea_logger.py                   # scan configured host, log all found sources
  python nmea_logger.py --scan-subnet     # scan entire subnet for NMEA devices
  python nmea_logger.py --skip-scan       # no scan, just connect to configured host:port
  python nmea_logger.py -o boat_log.txt   # custom output filename
        """,
    )
    parser.add_argument("--host", default=AIS_HOST, help=f"Primary host to scan (default: {AIS_HOST})")
    parser.add_argument("--port", type=int, default=AIS_PORT, help=f"Primary port (default: {AIS_PORT})")
    parser.add_argument("--scan-subnet", action="store_true",
                        help="Scan the entire /24 subnet for NMEA devices")
    parser.add_argument("--skip-scan", action="store_true",
                        help="Skip scanning, just connect to host:port via TCP")
    parser.add_argument("-o", "--output", default=None,
                        help="Output file (default: nmea_YYYY-MM-DD_HHMMSS.txt)")
    args = parser.parse_args()

    loop = asyncio.new_event_loop()
    outfile_ref = [None]  # mutable ref for shutdown handler

    def shutdown():
        print("\n\nStopping...")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)

    try:
        loop.run_until_complete(run(args))
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
        print("Done.")


if __name__ == "__main__":
    main()
