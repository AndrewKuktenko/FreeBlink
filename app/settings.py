"""Settings changed from the dashboard, stored in data/settings.json (mode 600).

A value saved here overrides the same setting from .env / the environment.
"""

import json
import os
from typing import Any, Dict, Optional

from . import config

FILE = config.DATA_DIR / "settings.json"


def _load() -> Dict[str, Any]:
    try:
        return json.loads(FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save(data: Dict[str, Any]) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(FILE)


def update(**values: Optional[str]) -> None:
    """Set values; an empty string removes the dashboard override."""
    data = _load()
    for key, value in values.items():
        if value is None:
            continue
        if value == "":
            data.pop(key, None)
        else:
            data[key] = value
    _save(data)


def anthropic_api_key() -> Optional[str]:
    return _load().get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or None


def anthropic_key_source() -> Optional[str]:
    if _load().get("anthropic_api_key"):
        return "dashboard"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ".env"
    return None


def ntfy_topic() -> str:
    return _load().get("ntfy_topic") or config.NTFY_TOPIC


def key_hint(key: Optional[str]) -> Optional[str]:
    return f"…{key[-4:]}" if key and len(key) > 8 else None
