"""Runtime settings, read from environment variables (or a .env file)."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value[:1] in ('"', "'") and value[-1:] == value[:1]:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()  # inline comment
        if value:  # an empty KEY= must not mask a value configured elsewhere
            os.environ.setdefault(key.strip(), value)


_load_dotenv()

DATA_DIR = Path(os.environ.get("BLINK_DATA_DIR", ROOT / "data"))
CREDENTIALS_FILE = DATA_DIR / "blink_credentials.json"
LIVE_DIR = DATA_DIR / "live"

# Protects the web UI itself. Required whenever HOST is not localhost.
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))

# How often to poll Blink for new state (seconds). Blink throttles aggressive polling.
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "60"))

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
# Stop a live view when no browser has fetched it for this many seconds.
LIVE_IDLE_TIMEOUT = int(os.environ.get("LIVE_IDLE_TIMEOUT", "20"))

CLIPS_DIR = DATA_DIR / "clips"  # cache of downloaded clip videos
CLIP_THUMBS_DIR = DATA_DIR / "clip_thumbs"
EVENTS_DIR = DATA_DIR / "events"
MODELS_DIR = DATA_DIR / "models"
STILLS_DIR = DATA_DIR / "stills"  # last live-view frame per camera
# Degrees moved per D-pad press on a Blink Mini Pan-Tilt Mount.
PAN_STEP = int(os.environ.get("PAN_STEP", "15"))
TILT_STEP = int(os.environ.get("TILT_STEP", "10"))

# ---------- AI object detection ----------
# "claude" (Claude vision via the Anthropic API, needs ANTHROPIC_API_KEY) or "yolo" (local model, free).
DETECT_ENGINE = os.environ.get("DETECT_ENGINE", "claude").strip().lower()
CLAUDE_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5-5")
CLAUDE_EFFORT = os.environ.get("AI_EFFORT", "low")
CLAUDE_FRAMES = int(os.environ.get("AI_FRAMES", "4"))  # frames sent per clip
CLAUDE_FRAME_WIDTH = int(os.environ.get("AI_FRAME_WIDTH", "640"))  # px; smaller = fewer tokens
DETECT_ENABLED = os.environ.get("DETECT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
# Any YOLO detection model ultralytics can download, e.g. yolo26n.pt (fastest) .. yolo26l.pt (most accurate).
DETECT_MODEL = os.environ.get("DETECT_MODEL", "yolo26s.pt")
# COCO class names to alert on. Full list: person, bicycle, car, motorcycle, bus, truck, bird, cat, dog, ...
DETECT_CLASSES = [c.strip().lower() for c in os.environ.get(
    "DETECT_CLASSES", "person,car,truck,dog,cat,bicycle,motorcycle").split(",") if c.strip()]
DETECT_MIN_CONFIDENCE = float(os.environ.get("DETECT_MIN_CONFIDENCE", "0.5"))
# Only analyze clips from these cameras (comma-separated names); empty = all cameras.
DETECT_CAMERAS = [c.strip().lower() for c in os.environ.get("DETECT_CAMERAS", "").split(",") if c.strip()]
DETECT_POLL_INTERVAL = int(os.environ.get("DETECT_POLL_INTERVAL", "30"))
DETECT_FPS = float(os.environ.get("DETECT_FPS", "2"))  # frames per second sampled from each clip
DETECT_MAX_FRAMES = int(os.environ.get("DETECT_MAX_FRAMES", "60"))
EVENTS_KEEP = int(os.environ.get("EVENTS_KEEP", "500"))
# Objects that get parked. They only alert if they're in a new spot compared with
# recent clips from the same camera (so a car in your driveway doesn't alert on every clip).
STATIONARY_CLASSES = [c.strip().lower() for c in os.environ.get(
    "STATIONARY_CLASSES", "car,truck,bus,motorcycle,bicycle").split(",") if c.strip()]
STATIONARY_WINDOW_HOURS = float(os.environ.get("STATIONARY_WINDOW_HOURS", "24"))

# ---------- notifications (ntfy) ----------
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")
# Don't repeat the same object on the same camera within this many seconds.
NOTIFY_COOLDOWN = int(os.environ.get("NOTIFY_COOLDOWN", "300"))
# Optional URL opened when you tap a notification (e.g. your Tailscale address for this app).
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
