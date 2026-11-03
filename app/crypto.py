"""Encryption at rest for Blink tokens and API keys (Fernet: AES-128-CBC + HMAC-SHA256)."""

import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from . import config

KEY_FILE = config.DATA_DIR / "secret.key"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = config.SECRET_KEY.encode() if config.SECRET_KEY else None
    if key is None:
        if KEY_FILE.exists():
            key = KEY_FILE.read_bytes().strip()
        else:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key)
    return Fernet(key)


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    """Raises ValueError if the data was encrypted with a different key or was tampered with."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Could not decrypt (wrong SECRET_KEY?)") from exc


def write_secret(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(encrypt(text))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def read_secret(path) -> str:
    return decrypt(path.read_text().strip())
