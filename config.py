import os
from pathlib import Path

# Load .env file if present
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())

AIS_HOST = os.environ.get("AIS_HOST", "192.168.47.10")
AIS_PORT = int(os.environ.get("AIS_PORT", "10110"))
AIS_PROTOCOL = os.environ.get("AIS_PROTOCOL", "auto")  # "auto", "tcp", or "udp"
AISSTREAM_API_KEY = os.environ.get("AISSTREAM_API_KEY", "")
OWN_MMSI = int(os.environ.get("OWN_MMSI", "338361814"))
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ais_tracker.db"))
SERVER_HOST = os.environ.get("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8888"))
