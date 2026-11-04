"""Per-user settings (Claude API key, ntfy topic), stored in the user's folder.

The API key is encrypted at rest; nothing here is shared between Blink accounts.
"""

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import crypto


class UserSettings:
    def __init__(self, folder: Path) -> None:
        self.file = folder / "settings.json"

    def _load(self) -> Dict[str, Any]:
        try:
            return json.loads(self.file.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def _save(self, data: Dict[str, Any]) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.file)

    def anthropic_api_key(self) -> Optional[str]:
        token = self._load().get("anthropic_api_key_enc")
        if not token:
            return None
        try:
            return crypto.decrypt(token)
        except ValueError:
            return None

    def set_anthropic_api_key(self, key: Optional[str]) -> None:
        data = self._load()
        data.pop("anthropic_api_key", None)  # never keep a plaintext copy
        if key:
            data["anthropic_api_key_enc"] = crypto.encrypt(key)
        else:
            data.pop("anthropic_api_key_enc", None)
        self._save(data)

    def ntfy_topic(self) -> str:
        """Every account gets its own hard-to-guess topic, created on first use."""
        data = self._load()
        if not data.get("ntfy_topic"):
            data["ntfy_topic"] = "freeblink-" + secrets.token_urlsafe(15).replace("_", "").replace("-", "").lower()[:18]
            self._save(data)
        return data["ntfy_topic"]

    def site_url(self) -> str:
        """Address the owner opens FreeBlink at; notifications link back to it."""
        return self._load().get("site_url", "")

    def remember_site_url(self, url: str) -> None:
        url = url.rstrip("/")
        data = self._load()
        if data.get("site_url") != url:
            data["site_url"] = url
            self._save(data)

    def faces_enabled(self) -> bool:
        return bool(self._load().get("faces_enabled"))

    def set_faces_enabled(self, enabled: bool) -> None:
        data = self._load()
        data["faces_enabled"] = enabled
        if enabled:  # turning it on is where the owner confirms the people they add agreed
            data["faces_consent_at"] = time.time()
        self._save(data)

    def set_ntfy_topic(self, topic: str) -> None:
        data = self._load()
        data["ntfy_topic"] = topic
        self._save(data)


def key_hint(key: Optional[str]) -> Optional[str]:
    return key[-4:] if key and len(key) > 8 else None
