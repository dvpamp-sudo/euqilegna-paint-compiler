from __future__ import annotations

from pathlib import Path
import os

APP_NAME = "Euqilegna Interactive Digital Art Studio"
APP_VERSION = "8.0"

PROJECT_ROOT = Path(__file__).resolve().parent
LOCAL_APP_DATA = Path(
    os.environ.get(
        "LOCALAPPDATA",
        str(Path.home() / "AppData" / "Local"),
    )
)
DATA_ROOT = LOCAL_APP_DATA / "EuqilegnaDigitalArtStudio"
GENERATED_ROOT = DATA_ROOT / "generated"
PREMIUM_GENERATED_ROOT = GENERATED_ROOT / "premium"
USER_UPLOAD_ROOT = GENERATED_ROOT / "uploads"
STATE_ROOT = DATA_ROOT / "state"
GALLERY_FILE = STATE_ROOT / "gallery.json"
SETTINGS_FILE = STATE_ROOT / "settings.json"
ANALYTICS_FILE = STATE_ROOT / "analytics.jsonl"

REQUIRED_DIRECTORIES = (
    DATA_ROOT,
    GENERATED_ROOT,
    PREMIUM_GENERATED_ROOT,
    USER_UPLOAD_ROOT,
    STATE_ROOT,
)


def initialize_storage() -> None:
    for path in REQUIRED_DIRECTORIES:
        path.mkdir(parents=True, exist_ok=True)
