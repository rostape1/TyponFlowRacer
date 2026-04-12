import asyncio
import logging

from config import AIS_HOST, AIS_PORT

logger = logging.getLogger(__name__)


async def ais_listener_tcp(raw_queue: asyncio.Queue, host: str, port: int):
    """Connect to AIS receiver via TCP and push raw NMEA sentences to the queue."""
    while True:
        try:
            logger.info(f"Connecting to AIS receiver via TCP at {host}:{port}...")
            reader, writer = await asyncio.open_connection(host, port)
            logger.info("Connected to AIS receiver (TCP)")

            while True:
                line = await reader.readline()
                if not line:
                    logger.warning("AIS receiver disconnected (EOF)")
                    break
                sentence = line.decode("ascii", errors="ignore").strip()
                if sentence:
                    logger.debug(f"RAW: {sentence}")
                    await raw_queue.put(sentence)

        except (ConnectionRefusedError, ConnectionResetError, OSError) as e:
            logger.warning(f"TCP connection error: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        logger.info("Reconnecting in 5 seconds...")
        await asyncio.sleep(5)


class _UdpProtocol(asyncio.DatagramProtocol):
    """UDP protocol handler for AIS NMEA data."""

    def __init__(self, raw_queue: asyncio.Queue):
        self.raw_queue = raw_queue
        self.buffer = ""

    def connection_made(self, transport):
        logger.info("UDP listener ready")

    def datagram_received(self, data, addr):
        try:
            text = data.decode("ascii", errors="ignore")
            self.buffer += text
            while "\n" in self.buffer:
                line, self.buffer = self.buffer.split("\n", 1)
                sentence = line.strip()
                if sentence:
                    logger.debug(f"RAW (UDP from {addr[0]}): {sentence}")
                    self.raw_queue.put_nowait(sentence)
        except Exception as e:
            logger.debug(f"UDP parse error: {e}")

    def error_received(self, exc):
        logger.warning(f"UDP error: {exc}")


async def ais_listener_udp(raw_queue: asyncio.Queue, port: int):
    """Listen for AIS NMEA sentences via UDP broadcast."""
    logger.info(f"Listening for AIS data via UDP on port {port}...")
    loop = asyncio.get_event_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: _UdpProtocol(raw_queue),
        local_addr=("0.0.0.0", port),
        allow_broadcast=True,
    )
    # Keep running forever
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        transport.close()


async def ais_listener(raw_queue: asyncio.Queue, host: str = AIS_HOST, port: int = AIS_PORT, protocol: str = "auto"):
    """
    Auto-detect: try TCP first, fall back to UDP.
    Or specify protocol="tcp" or protocol="udp".
    """
    if protocol == "udp":
        await ais_listener_udp(raw_queue, port)
        return

    if protocol == "tcp":
        await ais_listener_tcp(raw_queue, host, port)
        return

    # Auto mode: try TCP first, if it fails quickly, switch to UDP
    logger.info("Auto-detecting AIS connection (trying TCP then UDP)...")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5
        )
        writer.close()
        await writer.wait_closed()
        logger.info("TCP connection successful — using TCP mode")
        await ais_listener_tcp(raw_queue, host, port)
    except (ConnectionRefusedError, ConnectionResetError, OSError, asyncio.TimeoutError) as e:
        logger.info(f"TCP failed ({e}) — switching to UDP mode on port {port}")
        await ais_listener_udp(raw_queue, port)
