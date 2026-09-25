import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("SMSYNC_DB", str(BASE_DIR / "smsync.db"))
DATA_DIR = Path(DB_PATH).resolve().parent
RECORDINGS_DIR = Path(os.environ.get("SMSYNC_RECORDINGS", str(DATA_DIR / "recordings")))
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

_token_file = BASE_DIR / ".token"


def get_token() -> str:
    """Server auth token. Set SMSYNC_TOKEN to pin it; otherwise a random one is
    generated once and stored in server/.token so restarts keep working."""
    token = os.environ.get("SMSYNC_TOKEN")
    if token:
        return token
    if _token_file.exists():
        return _token_file.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(24)
    _token_file.write_text(token, encoding="utf-8")
    return token


TOKEN = get_token()
