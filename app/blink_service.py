"""Owns the single Blink session: login, 2FA, token persistence, polling, media."""

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from aiohttp import ClientSession
from blinkpy import api
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink
from blinkpy.camera import BlinkCamera

from . import config
from .media import extract_frames

log = logging.getLogger(__name__)

LOGGED_OUT = "logged_out"
NEEDS_2FA = "needs_2fa"
READY = "ready"

# blinkpy models standalone Minis and doorbells as their own "sync module".
SYSTEM_KINDS = {"BlinkSyncModule": "sync_module", "BlinkOwl": "mini", "BlinkLotus": "doorbell"}


class BlinkError(Exception):
    pass


class _SigninStatus(logging.Handler):
    """blinkpy logs, but doesn't return, the HTTP status of a failed sign-in. Capture it."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.status: Optional[int] = None

    def emit(self, record: logging.LogRecord) -> None:
        if str(record.msg).startswith("OAuth signin failed") and record.args:
            try:
                self.status = int(record.args[0])
            except (TypeError, ValueError):
                pass


_signin = _SigninStatus()
logging.getLogger("blinkpy.api").addHandler(_signin)


def camera_key(cam: BlinkCamera) -> str:
    return f"{cam.network_id}-{cam.camera_id}"


class BlinkService:
    def __init__(self) -> None:
        self.session: Optional[ClientSession] = None
        self.blink: Optional[Blink] = None
        self.state = LOGGED_OUT
        self.error: Optional[str] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        # Thumbnail bytes keyed by the Blink URL they came from.
        self._thumb_cache: Dict[str, bytes] = {}
        # Clip id -> media metadata from the last clip listing.
        self._clips: Dict[str, Dict[str, Any]] = {}
        self._usb_lock = asyncio.Lock()

    # ---------- lifecycle ----------

    async def startup(self) -> None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.session = ClientSession()
        if config.CREDENTIALS_FILE.exists():
            try:
                saved = json.loads(config.CREDENTIALS_FILE.read_text())
                await self._resume(saved)
            except Exception as exc:  # noqa: BLE001 - surface any resume failure to UI
                log.warning("Could not resume saved Blink session: %s", exc)
                self.error = "Saved session expired. Please sign in again."
                self.state = LOGGED_OUT
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def shutdown(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
        if self.session:
            await self.session.close()

    async def _resume(self, saved: Dict[str, Any]) -> None:
        blink = self._new_blink(saved)
        try:
            ok = await blink.start()
        except BlinkTwoFARequiredError:
            # Refresh token was rejected and blinkpy fell back to a full login.
            raise BlinkError("Saved session needs a new login")
        if not ok:
            raise BlinkError("Saved session rejected")
        self.blink = blink
        self.state = READY
        self.error = None
        await self._save()
        log.info("Resumed Blink session for %s", saved.get("username"))
        self._schedule_first_refresh()

    def _new_blink(self, login_data: Dict[str, Any]) -> Blink:
        blink = Blink(session=self.session, refresh_rate=config.REFRESH_INTERVAL)
        blink.auth = Auth(
            login_data,
            no_prompt=True,
            session=self.session,
            callback=self._on_token_refresh,
        )
        return blink

    def _schedule_first_refresh(self) -> None:
        """blinkpy only reads the USB clip list on refresh; do one now, not a minute from now."""
        async def run() -> None:
            try:
                await self.blink.refresh(force=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("Initial refresh failed: %s", exc)
        asyncio.get_event_loop().create_task(run())

    def _on_token_refresh(self) -> None:
        asyncio.get_event_loop().create_task(self._save())

    async def _save(self) -> None:
        """Persist tokens so restarts don't need 2FA. The password is never written."""
        if not self.blink:
            return
        data = dict(self.blink.auth.login_attributes)
        data.pop("password", None)
        tmp = config.CREDENTIALS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(config.CREDENTIALS_FILE)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(config.REFRESH_INTERVAL)
            if self.state != READY:
                continue
            try:
                await self.blink.refresh(force=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("Blink refresh failed: %s", exc)

    # ---------- auth ----------

    async def login(self, username: str, password: str) -> str:
        async with self._lock:
            self.blink = self._new_blink({"username": username, "password": password})
            self.error = None
            _signin.status = None
            try:
                ok = await self.blink.start()
            except BlinkTwoFARequiredError:
                self.state = NEEDS_2FA
                return self.state
            if not ok:
                self.blink = None
                self.state = LOGGED_OUT
                if _signin.status == 429:
                    raise BlinkError(
                        "Blink is temporarily blocking sign-in after too many attempts. "
                        "Wait about 15 minutes, then try once."
                    )
                raise BlinkError("Login failed. Check your email and password.")
            self.state = READY
            await self._save()
            self._schedule_first_refresh()
            return self.state

    async def verify_2fa(self, code: str) -> str:
        async with self._lock:
            if self.state != NEEDS_2FA or not self.blink:
                raise BlinkError("No login waiting for a verification code.")
            ok = await self.blink.send_2fa_code(code.strip())
            if not ok:
                raise BlinkError("Verification failed. Check the code and try again.")
            self.state = READY
            await self._save()
            self._schedule_first_refresh()
            return self.state

    async def logout(self) -> None:
        async with self._lock:
            self.blink = None
            self.state = LOGGED_OUT
            self._thumb_cache.clear()
            self._clips.clear()
            if config.CREDENTIALS_FILE.exists():
                config.CREDENTIALS_FILE.unlink()

    def _require(self) -> Blink:
        if self.state != READY or not self.blink:
            raise BlinkError("Not signed in to Blink.")
        return self.blink

    # ---------- devices ----------

    def find_camera(self, key: str) -> BlinkCamera:
        for cam in self._require().cameras.values():
            if camera_key(cam) == key:
                return cam
        raise BlinkError(f"Unknown camera {key}")

    def devices(self) -> Dict[str, Any]:
        blink = self._require()
        # Blink only fills last_record from cloud clips; USB-only setups need the clip list.
        newest: Dict[str, str] = {}
        for clip in self._clips.values():
            cam_name, created = clip.get("camera"), clip.get("created_at")
            if cam_name and created and _ts(created) > _ts(newest.get(cam_name)):
                newest[cam_name] = created
        systems = []
        for name, sync in blink.sync.items():
            cams = []
            for cam in sync.cameras.values():
                attrs = cam.attributes
                cams.append({
                    "key": camera_key(cam),
                    "name": cam.name,
                    "kind": cam.camera_type or "camera",
                    "product_type": cam.product_type,
                    "motion_enabled": cam.motion_enabled,
                    "motion_detected": cam.motion_detected,
                    "armed": cam.arm,
                    "battery": attrs.get("battery"),
                    "battery_voltage": attrs.get("battery_voltage"),
                    "temperature_f": attrs.get("temperature_calibrated") or attrs.get("temperature"),
                    "wifi_strength": cam.wifi_strength,
                    "last_record": max(filter(None, [cam.last_record, newest.get(cam.name)]), key=_ts, default=None),
                    "thumbnail_version": _thumb_version(cam.thumbnail) or _still_version(camera_key(cam)),
                })
            systems.append({
                "name": name,
                "kind": SYSTEM_KINDS.get(type(sync).__name__, "sync_module"),
                "armed": sync.arm,
                "online": sync.online,
                "cameras": cams,
            })
        return {
            "systems": systems,
            "last_refresh": blink.last_refresh,
        }

    async def set_system_armed(self, name: str, armed: bool) -> None:
        blink = self._require()
        if name not in blink.sync:
            raise BlinkError(f"Unknown system {name}")
        await blink.sync[name].async_arm(armed)
        await self._refresh_soon()

    async def set_motion(self, key: str, enabled: bool) -> None:
        cam = self.find_camera(key)
        await cam.async_arm(enabled)
        await self._refresh_soon()

    async def snap(self, key: str) -> None:
        cam = self.find_camera(key)
        await cam.snap_picture()
        await self._refresh_soon()

    async def refresh(self) -> None:
        await self._require().refresh(force=True)

    async def _refresh_soon(self) -> None:
        # Blink applies commands asynchronously; give it a moment, then pull fresh state.
        await asyncio.sleep(2)
        try:
            await self._require().refresh(force=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("Refresh after command failed: %s", exc)

    # ---------- media ----------

    async def _fetch(self, url: str) -> Optional[bytes]:
        blink = self._require()
        response = await api.http_get(blink, url=url, stream=True, json=False)
        if response is None or response.status != 200:
            return None
        return await response.read()

    async def thumbnail(self, key: str) -> Optional[bytes]:
        cam = self.find_camera(key)
        url = cam.thumbnail
        if url and url not in self._thumb_cache:
            data = await self._fetch(url)
            if data is not None:
                self._cache_thumb(url, data)
        if url and url in self._thumb_cache:
            return self._thumb_cache[url]
        # No Blink thumbnail (common for Minis): use the last live-view frame, if any.
        still = config.STILLS_DIR / f"{key}.jpg"
        return still.read_bytes() if still.exists() else None

    def _cache_thumb(self, url: str, data: bytes) -> None:
        self._thumb_cache[url] = data
        while len(self._thumb_cache) > 100:
            self._thumb_cache.pop(next(iter(self._thumb_cache)))

    async def recent_clip_items(self, hours: int = 24) -> List[Dict[str, Any]]:
        """Cloud clips and Sync Module USB clips from the last `hours`, newest first.

        Each item: id, camera, system, created_at (ISO), source ("cloud"/"usb").
        """
        blink = self._require()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result: List[Dict[str, Any]] = []

        since = cutoff.astimezone().strftime("%Y/%m/%d %H:%M:%S")
        for item in await blink.get_videos_metadata(since=since, stop=5):
            if item.get("deleted"):
                continue
            clip = {
                "id": f"cloud-{item['id']}",
                "camera": item.get("device_name"),
                "system": item.get("network_name"),
                "created_at": item.get("created_at"),
                "source": "cloud",
                "_media": item.get("media"),
                "_thumbnail": item.get("thumbnail"),
            }
            result.append(clip)

        for sync_name, sync in blink.sync.items():
            storage = getattr(sync, "_local_storage", None) or {}
            for media in storage.get("manifest", []):
                created = media.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created < cutoff:
                    continue
                result.append({
                    "id": f"usb-{sync.network_id}-{media.id}",
                    "camera": media.name,
                    "system": sync_name,
                    "created_at": created.isoformat(),
                    "source": "usb",
                    "_sync": sync,
                    "_local": media,
                })

        for clip in result:
            self._clips[clip["id"]] = clip
        result.sort(key=lambda c: _ts(c["created_at"]), reverse=True)
        return result

    async def clips(self, hours: int = 24) -> List[Dict[str, Any]]:
        items = await self.recent_clip_items(hours)
        return [{k: v for k, v in c.items() if not k.startswith("_")} for c in items]

    def _clip(self, clip_id: str) -> Dict[str, Any]:
        self._require()
        item = self._clips.get(clip_id)
        if not item:
            raise BlinkError("Unknown clip; reload the clip list.")
        return item

    async def clip_video(self, clip_id: str) -> Optional[bytes]:
        item = self._clip(clip_id)
        cache = config.CLIPS_DIR / f"{clip_id}.mp4"
        if cache.exists():
            return cache.read_bytes()
        if item["source"] == "cloud":
            data = await self._fetch(urljoin(self.blink.urls.base_url, item["_media"]))
        else:
            data = await self._fetch_usb_clip(item)
        if data:
            config.CLIPS_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
            self._prune_clip_cache()
        return data

    async def _fetch_usb_clip(self, item: Dict[str, Any]) -> Optional[bytes]:
        """Ask the Sync Module to upload a USB clip to Blink, then download it."""
        media = item["_local"]
        sync = item["_sync"]
        async with self._usb_lock:  # the Sync Module handles one upload at a time
            url = self.blink.urls.base_url + media.url(sync._local_storage.get("last_manifest_id"))
            for attempt in range(4):
                if attempt == 0 or attempt == 2:
                    await media.prepare_download(self.blink)
                data = await self._fetch(url)
                if data and len(data) > 1024:
                    return data
                await asyncio.sleep(3 * (attempt + 1))
        log.warning("Could not fetch USB clip %s", item["id"])
        return None

    async def _thumbnail_from_video(self, clip_id: str) -> Optional[bytes]:
        """USB clips have no Blink thumbnail: fetch the clip once and keep a frame from it."""
        thumb = config.CLIP_THUMBS_DIR / f"{clip_id}.jpg"
        if thumb.exists():
            return thumb.read_bytes()
        video = await self.clip_video(clip_id)
        if not video:
            return None
        frames = await extract_frames(video, 1)
        if not frames:
            return None
        config.CLIP_THUMBS_DIR.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(frames[0])
        return frames[0]

    def _prune_clip_cache(self, keep: int = 300) -> None:
        files = sorted(config.CLIPS_DIR.glob("*.mp4"), key=lambda f: f.stat().st_mtime)
        for f in files[:-keep]:
            f.unlink(missing_ok=True)

    async def clip_thumbnail(self, clip_id: str) -> Optional[bytes]:
        item = self._clip(clip_id)
        path = item.get("_thumbnail")
        if not path:
            return await self._thumbnail_from_video(clip_id)
        url = urljoin(self.blink.urls.base_url, path)
        cached = self._thumb_cache.get(url)
        if cached:
            return cached
        data = await self._fetch(url)
        if data is None and not url.endswith(".jpg"):
            data = await self._fetch(url + ".jpg")
        if data is not None:
            self._cache_thumb(url, data)
        return data


def _still_version(key: str) -> str:
    still = config.STILLS_DIR / f"{key}.jpg"
    return f"s{int(still.stat().st_mtime)}" if still.exists() else ""


def _ts(value: Optional[str]) -> float:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


def _thumb_version(url: Optional[str]) -> str:
    """Short token that changes whenever Blink has a new thumbnail (for cache busting)."""
    if not url:
        return ""
    return hashlib.sha1(url.encode()).hexdigest()[:10]


__all__: Tuple[str, ...] = ("BlinkService", "BlinkError", "camera_key", "LOGGED_OUT", "NEEDS_2FA", "READY")
