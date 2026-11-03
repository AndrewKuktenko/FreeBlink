"""Accounts. Every Blink account that signs in gets its own Tenant: its own Blink session,
clip watcher, live views, settings and data folder. Browsers hold a random session token
(cookie); the server stores only its SHA-256 hash.
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import shutil
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

from . import config, crypto
from .blink_service import NEEDS_2FA, READY, BlinkError, BlinkService
from .events import EventWatcher
from .live import LiveManager
from .notifier import Notifier
from .settings import UserSettings

log = logging.getLogger(__name__)

PENDING_TTL = 10 * 60  # seconds to enter a 2FA code
MAX_PENDING = 50


def tenant_id(username: str) -> str:
    """Stable folder name for a Blink account that doesn't reveal the email."""
    return hashlib.sha256(("freeblink:" + username.strip().lower()).encode()).hexdigest()[:20]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Tenant:
    def __init__(self, tid: str, service: Optional[BlinkService] = None) -> None:
        self.id = tid
        self.folder = config.USERS_DIR / tid
        self.service = service or BlinkService(self.folder)
        self.settings = UserSettings(self.folder)
        self.live = LiveManager(self.folder)
        self.watcher: Optional[EventWatcher] = None
        self._meta_file = self.folder / "meta.json"

    async def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        if self.service.session is None:
            await self.service.startup()
        notifier = Notifier(self.service.session, self.settings.ntfy_topic)
        self.watcher = EventWatcher(self.service, notifier, self.folder, self.settings)
        self.watcher.start()
        self.touch(force=True)

    async def stop(self) -> None:
        if self.watcher:
            await self.watcher.stop()
        await self.live.stop_all()
        self.live.close()
        await self.service.shutdown()

    def last_seen(self) -> float:
        try:
            return float(json.loads(self._meta_file.read_text())["last_seen"])
        except (FileNotFoundError, ValueError, KeyError):
            return time.time()

    def touch(self, force: bool = False) -> None:
        """Record that the owner used the site (written at most once an hour)."""
        now = time.time()
        if force or now - getattr(self, "_touched", 0) > 3600:
            self._touched = now
            self._meta_file.write_text(json.dumps({"last_seen": now}))


class Sessions:
    def __init__(self) -> None:
        self.file = config.SESSIONS_FILE
        try:
            self.data: Dict[str, Dict] = json.loads(self.file.read_text())
        except (FileNotFoundError, ValueError):
            self.data = {}

    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        os.chmod(tmp, 0o600)
        tmp.replace(self.file)

    def create(self, tid: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.data = {h: s for h, s in self.data.items() if s["expires"] > now}
        self.data[_hash(token)] = {"tenant": tid, "expires": now + config.SESSION_DAYS * 86400}
        self._save()
        return token

    def tenant(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        entry = self.data.get(_hash(token))
        if not entry or entry["expires"] < time.time():
            return None
        return entry["tenant"]

    def delete(self, token: Optional[str]) -> None:
        if token and self.data.pop(_hash(token), None):
            self._save()

    def delete_tenant(self, tid: str) -> None:
        self.data = {h: s for h, s in self.data.items() if s["tenant"] != tid}
        self._save()

    def has_tenant(self, tid: str) -> bool:
        now = time.time()
        return any(s["tenant"] == tid and s["expires"] > now for s in self.data.values())


@dataclass
class Pending:
    tid: str
    service: BlinkService
    created: float


class RateLimiter:
    """Sign-in attempts per client IP, so the server can't be used to hammer Blink."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit, self.window = limit, window
        self.hits: Dict[str, Deque[float]] = {}

    def check(self, key: str) -> None:
        now = time.time()
        q = self.hits.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            raise BlinkError("Too many sign-in attempts. Wait 15 minutes and try again.")
        q.append(now)
        if len(self.hits) > 10000:  # forget idle IPs
            self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < self.window}


class TenantManager:
    def __init__(self) -> None:
        self.tenants: Dict[str, Tenant] = {}
        self.sessions = Sessions()
        self.pending: Dict[str, Pending] = {}
        self.limiter = RateLimiter(config.LOGIN_ATTEMPTS, 15 * 60)
        self.resume_errors: Dict[str, str] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._janitor: Optional[asyncio.Task] = None

    def _lock(self, tid: str) -> asyncio.Lock:
        return self._locks.setdefault(tid, asyncio.Lock())

    # ---------- lifecycle ----------

    async def startup(self) -> None:
        config.USERS_DIR.mkdir(parents=True, exist_ok=True)
        _migrate_single_user()
        for folder in sorted(config.USERS_DIR.iterdir()):
            if (folder / "blink.enc").exists():
                tenant = Tenant(folder.name)
                if time.time() - tenant.last_seen() > config.INACTIVE_DAYS * 86400:
                    continue  # resumes when the owner signs in again
                await self._start(tenant)
        log.info("Watching %d Blink account(s)", len(self.tenants))
        self._janitor = asyncio.create_task(self._janitor_loop())

    async def shutdown(self) -> None:
        if self._janitor:
            self._janitor.cancel()
        for p in self.pending.values():
            await p.service.shutdown()
        for tenant in list(self.tenants.values()):
            await tenant.stop()

    async def _start(self, tenant: Tenant) -> None:
        await tenant.start()
        if tenant.service.state == READY:
            self.tenants[tenant.id] = tenant
            self.resume_errors.pop(tenant.id, None)
        else:
            self.resume_errors[tenant.id] = tenant.service.error or "Please sign in again."
            await tenant.stop()

    async def _janitor_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for token, p in list(self.pending.items()):
                if now - p.created > PENDING_TTL:
                    self.pending.pop(token, None)
                    await p.service.shutdown()
            for tid, tenant in list(self.tenants.items()):
                if now - tenant.last_seen() > config.INACTIVE_DAYS * 86400 and not self.sessions.has_tenant(tid):
                    log.info("Account %s inactive; pausing it", tid)
                    self.tenants.pop(tid, None)
                    await tenant.stop()

    # ---------- lookups ----------

    async def for_session(self, token: Optional[str]) -> Optional[Tenant]:
        tid = self.sessions.tenant(token)
        if not tid:
            return None
        tenant = self.tenants.get(tid)
        if tenant is None and (config.USERS_DIR / tid / "blink.enc").exists():
            async with self._lock(tid):  # paused or failed earlier: try to resume it
                tenant = self.tenants.get(tid)
                if tenant is None:
                    await self._start(Tenant(tid))
                    tenant = self.tenants.get(tid)
        if tenant is None or tenant.service.state != READY:
            return None
        tenant.touch()
        return tenant

    def pending_for(self, token: Optional[str]) -> Optional[Pending]:
        p = self.pending.get(token or "")
        if p and time.time() - p.created > PENDING_TTL:
            return None
        return p

    # ---------- sign in / out ----------

    async def login(self, ip: str, username: str, password: str) -> Tuple[str, str]:
        """Returns (state, token). state READY -> session token; NEEDS_2FA -> pending token."""
        self.limiter.check(ip)
        if len(self.pending) >= MAX_PENDING:
            raise BlinkError("Too many sign-ins in progress. Try again in a few minutes.")
        tid = tenant_id(username)
        service = BlinkService(config.USERS_DIR / tid)
        await service.startup(resume=False)
        try:
            state = await service.login(username, password)
        except Exception:
            await service.shutdown()
            raise
        if state == NEEDS_2FA:
            token = secrets.token_urlsafe(32)
            self.pending[token] = Pending(tid, service, time.time())
            return state, token
        return READY, await self._adopt(tid, service)

    async def verify_2fa(self, ip: str, pending_token: Optional[str], code: str) -> str:
        self.limiter.check(ip)
        p = self.pending_for(pending_token)
        if p is None:
            raise BlinkError("Your sign-in expired. Enter your email and password again.")
        await p.service.verify_2fa(code)  # raises on a wrong code; the pending login stays
        self.pending.pop(pending_token, None)
        return await self._adopt(p.tid, p.service)

    async def _adopt(self, tid: str, service: BlinkService) -> str:
        """A fresh, verified Blink login replaces whatever session this account had."""
        async with self._lock(tid):
            old = self.tenants.pop(tid, None)
            if old:
                await old.stop()
            tenant = Tenant(tid, service)
            await tenant.start()
            self.tenants[tid] = tenant
            self.resume_errors.pop(tid, None)
        return self.sessions.create(tid)

    async def cancel_pending(self, token: Optional[str]) -> None:
        p = self.pending.pop(token or "", None)
        if p:
            await p.service.shutdown()

    def sign_out(self, token: Optional[str]) -> None:
        """This browser only. The account keeps being watched so phone alerts continue."""
        self.sessions.delete(token)

    async def delete_account(self, tid: str) -> None:
        """Stop watching, forget the Blink token and erase every file for this account."""
        async with self._lock(tid):
            tenant = self.tenants.pop(tid, None)
            if tenant:
                await tenant.stop()
            self.sessions.delete_tenant(tid)
            shutil.rmtree(config.USERS_DIR / tid, ignore_errors=True)
        log.info("Deleted account %s", tid)


def _migrate_single_user() -> None:
    """Move a pre-multi-user install (data/blink_credentials.json etc.) into users/<id>/."""
    legacy = config.DATA_DIR / "blink_credentials.json"
    if not legacy.exists():
        return
    try:
        creds = json.loads(legacy.read_text())
        tid = tenant_id(creds["username"])
    except (ValueError, KeyError):
        log.warning("Legacy credentials unreadable; skipping migration")
        return
    folder = config.USERS_DIR / tid
    folder.mkdir(parents=True, exist_ok=True)
    crypto.write_secret(folder / "blink.enc", json.dumps(creds))
    for name in ("clips", "clip_thumbs", "stills", "events"):
        src = config.DATA_DIR / name
        if src.exists() and not (folder / name).exists():
            shutil.move(str(src), str(folder / name))
    settings = UserSettings(folder)
    old_settings = config.DATA_DIR / "settings.json"
    try:
        old = json.loads(old_settings.read_text())
    except (FileNotFoundError, ValueError):
        old = {}
    key = old.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if key:
        settings.set_anthropic_api_key(key)
    topic = old.get("ntfy_topic") or config.NTFY_TOPIC
    if topic:
        settings.set_ntfy_topic(topic)
    legacy.unlink()
    old_settings.unlink(missing_ok=True)
    shutil.rmtree(config.DATA_DIR / "live", ignore_errors=True)
    log.info("Migrated the existing Blink account into users/%s", tid)
