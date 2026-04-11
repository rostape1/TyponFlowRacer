import os

AIS_HOST = os.environ.get("AIS_HOST", "192.168.47.10")
AIS_PORT = int(os.environ.get("AIS_PORT", "10110"))
AIS_PROTOCOL = os.environ.get("AIS_PROTOCOL", "auto")  # "auto", "tcp", or "udp"
OWN_MMSI = int(os.environ.get("OWN_MMSI", "338361814"))
DB_PATH = os.environ.get("DB_PATH", os.path.expanduser("~/.ais_tracker/ais_tracker.db"))
SERVER_HOST = os.environ.get("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8888"))
