import os
from pathlib import Path

APP_VERSION = "2.3.1"

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("SMSYNC_DB", str(BASE_DIR / "smsync.db"))
DATA_DIR = Path(DB_PATH).resolve().parent
RECORDINGS_DIR = Path(os.environ.get("SMSYNC_RECORDINGS", str(DATA_DIR / "recordings")))
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
