"""Push notifications via ntfy (https://ntfy.sh or a self-hosted ntfy server)."""

import base64
import logging
from typing import Callable, Optional

from aiohttp import ClientSession

from . import config

log = logging.getLogger(__name__)


def _header(value: str) -> str:
    """HTTP headers must be latin-1; ntfy accepts RFC 2047 for anything else."""
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(value.encode()).decode() + "?="


class Notifier:
    def __init__(self, session: ClientSession, topic: Callable[[], str]) -> None:
        self.session = session
        self.topic = topic

    @property
    def enabled(self) -> bool:
        return bool(self.topic())

    @property
    def target(self) -> str:
        return f"{config.NTFY_SERVER.rstrip('/')}/{self.topic()}"

    async def send(
        self,
        title: str,
        message: str,
        image: Optional[bytes] = None,
        tags: str = "",
        priority: int = 4,
    ) -> None:
        if not self.enabled:
            raise RuntimeError("Notifications are off: no ntfy topic set")
        headers = {
            "Title": _header(title),
            "Message": _header(message),
            "Priority": str(priority),
        }
        if tags:
            headers["Tags"] = tags
        if config.PUBLIC_URL:
            headers["Click"] = config.PUBLIC_URL
        if config.NTFY_TOKEN:
            headers["Authorization"] = f"Bearer {config.NTFY_TOKEN}"
        if image:
            headers["Filename"] = "snapshot.jpg"
        async with self.session.put(self.target, data=image or b"", headers=headers) as resp:
            if resp.status >= 300:
                body = (await resp.text())[:300]
                raise RuntimeError(f"ntfy returned {resp.status}: {body}")
        log.info("Notification sent: %s", title)
