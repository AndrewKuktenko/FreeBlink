"""Watch Blink for new motion clips, run detection, store events, notify."""

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

from . import config, settings
from .blink_service import READY, BlinkService, _ts
from .detector import ClipAnalysis, Detector, iou
from .notifier import Notifier

log = logging.getLogger(__name__)

EMOJI = {"person": "bust_in_silhouette", "car": "red_car", "truck": "truck", "dog": "dog",
         "cat": "cat", "bicycle": "bike", "motorcycle": "motorcycle", "bird": "bird"}


class EventStore:
    def __init__(self) -> None:
        self.dir = config.EVENTS_DIR
        self.index_file = self.dir / "events.json"
        self.seen_file = self.dir / "seen_clips.json"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events: List[Dict[str, Any]] = self._load(self.index_file, [])
        seen = self._load(self.seen_file, None)
        self.has_baseline = seen is not None
        self.seen: List[str] = seen or []

    @staticmethod
    def _load(path, default):
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            return default

    def _write(self, path, data) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)

    def mark_seen(self, clip_ids: List[str]) -> None:
        known = set(self.seen)
        self.seen.extend(c for c in clip_ids if c not in known)
        self.seen = self.seen[-5000:]
        self.has_baseline = True
        self._write(self.seen_file, self.seen)

    def is_seen(self, clip_id: str) -> bool:
        return clip_id in self.seen

    def add(self, event: Dict[str, Any], snapshot: Optional[bytes]) -> None:
        if snapshot:
            (self.dir / f"{event['id']}.jpg").write_bytes(snapshot)
        self.events.insert(0, event)
        for old in self.events[config.EVENTS_KEEP:]:
            (self.dir / f"{old['id']}.jpg").unlink(missing_ok=True)
        self.events = self.events[:config.EVENTS_KEEP]
        self._write(self.index_file, self.events)

    def snapshot(self, event_id: str) -> Optional[bytes]:
        if not event_id.replace("-", "").isalnum():
            return None
        path = self.dir / f"{event_id}.jpg"
        return path.read_bytes() if path.exists() else None


class EventWatcher:
    def __init__(self, service: BlinkService, notifier: Notifier) -> None:
        self.service = service
        self.notifier = notifier
        self.detector = Detector()  # YOLO; loads its model only if DETECT_ENGINE=yolo
        self._claude = None
        self.store = EventStore()
        self._task: Optional[asyncio.Task] = None
        self._cooldown: Dict[str, float] = {}
        self.last_check: Optional[float] = None
        self.last_error: Optional[str] = None
        # Never alert on clips recorded before the app started watching (with a little slack
        # for clips still uploading). Blink's USB clip list can load minutes after startup.
        self.started_at = time.time() - 120

    def start(self) -> None:
        if config.DETECT_ENABLED:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": config.DETECT_ENABLED,
            "engine": config.DETECT_ENGINE,
            "model": config.CLAUDE_MODEL if config.DETECT_ENGINE == "claude" else config.DETECT_MODEL,
            "effort": config.CLAUDE_EFFORT if config.DETECT_ENGINE == "claude" else None,
            "api_key_set": bool(settings.anthropic_api_key()),
            "classes": config.DETECT_CLASSES,
            "min_confidence": config.DETECT_MIN_CONFIDENCE,
            "cameras": config.DETECT_CAMERAS or "all",
            "notifications": self.notifier.enabled,
            "ntfy_topic": settings.ntfy_topic() or None,
            "ntfy_server": config.NTFY_SERVER,
            "poll_interval": config.DETECT_POLL_INTERVAL,
            "last_check": self.last_check,
            "last_error": self.last_error,
        }

    async def _loop(self) -> None:
        while True:
            try:
                if self.service.state == READY:
                    await self.check_once()
                    self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep watching after a bad clip/API hiccup
                log.exception("Event check failed")
                self.last_error = str(exc)
            await asyncio.sleep(config.DETECT_POLL_INTERVAL)

    async def check_once(self) -> int:
        """Process clips we haven't seen. Returns how many were analyzed."""
        items = await self.service.recent_clip_items(hours=6)
        ids = [str(i["id"]) for i in items]
        self.last_check = time.time()
        if not self.store.has_baseline:
            # First run: don't flood you with alerts about old clips.
            self.store.mark_seen(ids)
            log.info("Detection baseline set (%d existing clips ignored)", len(ids))
            return 0

        old = [str(i["id"]) for i in items
               if not self.store.is_seen(str(i["id"])) and _ts(i.get("created_at")) < self.started_at]
        if old:
            self.store.mark_seen(old)
            log.info("Ignoring %d clip(s) recorded before the app started watching", len(old))
        new = [i for i in items if not self.store.is_seen(str(i["id"]))]
        new.sort(key=lambda i: i.get("created_at") or "")
        analyzed = 0
        for item in new:
            clip_id = str(item["id"])
            camera = item.get("camera") or "Camera"
            if config.DETECT_CAMERAS and camera.lower() not in config.DETECT_CAMERAS:
                self.store.mark_seen([clip_id])
                continue
            await self._process(item)
            self.store.mark_seen([clip_id])
            analyzed += 1
        return analyzed

    async def _process(self, item: Dict[str, Any]) -> None:
        clip_id = str(item["id"])
        camera = item.get("camera") or "Camera"
        video = await self.service.clip_video(clip_id)
        if not video:
            log.warning("Could not download clip %s from %s", clip_id, camera)
            return
        if config.DETECT_ENGINE == "claude":
            await self._process_claude(item, video)
            return
        started = time.monotonic()
        analysis = await self.detector.analyze_clip(video)
        found, parked = self._split_stationary(camera, analysis)
        log.info("Clip %s (%s): %d frames in %.1fs -> %s%s", clip_id, camera, analysis.frames_analyzed,
                 time.monotonic() - started, found or "nothing new",
                 f" (ignored parked: {', '.join(parked)})" if parked else "")

        event = {
            "id": f"{clip_id}-{int(time.time())}",
            "clip_id": clip_id,
            "camera": camera,
            "system": item.get("system"),
            "source": item.get("source"),
            "created_at": item.get("created_at"),
            "detected": found,
            "parked": parked,
            "all_labels": analysis.labels,
            "boxes": [[label, conf, box] for label, conf, box in analysis.boxes],
            "analyzed_at": time.time(),
            "notified": False,
        }
        if found and self.notifier.enabled:
            event["notified"] = await self._notify(camera, found, analysis.snapshot)
        self.store.add(event, analysis.snapshot)

    def _claude_analyzer(self):
        key = settings.anthropic_api_key()
        if self._claude is None or self._claude.api_key != key:  # key changed in the dashboard
            from .claude_vision import ClaudeAnalyzer

            if not key:
                raise RuntimeError("Claude is not configured: add your Anthropic API key in Settings")
            try:
                self._claude = ClaudeAnalyzer(key)
            except Exception as exc:  # the SDK raises here when no credential is configured
                raise RuntimeError(f"Claude is not configured: set ANTHROPIC_API_KEY in .env ({exc})")
        return self._claude

    async def _process_claude(self, item: Dict[str, Any], video: bytes) -> None:
        """Describe every clip and push the description to the phone."""
        import anthropic

        clip_id = str(item["id"])
        camera = item.get("camera") or "Camera"
        started = time.monotonic()
        try:
            verdict = await self._claude_analyzer().analyze_clip(video, camera)
        except anthropic.BadRequestError as exc:
            # Something about this clip the API won't take; record it and move on.
            log.warning("Claude rejected clip %s: %s", clip_id, exc.message)
            self.last_error = f"Claude rejected clip {clip_id}: {exc.message}"
            return
        # Rate limits, outages and auth errors propagate: the clip stays unseen and is retried.
        log.info("Clip %s (%s): %d frames, %.1fs, %d in / %d out tokens [%s] -> %s: %s",
                 clip_id, camera, verdict.frames_analyzed, time.monotonic() - started,
                 verdict.input_tokens, verdict.output_tokens, verdict.model,
                 verdict.title, verdict.description)

        event = {
            "id": f"{clip_id}-{int(time.time())}",
            "clip_id": clip_id,
            "camera": camera,
            "system": item.get("system"),
            "source": item.get("source"),
            "created_at": item.get("created_at"),
            "engine": "claude",
            "title": verdict.title,
            "description": verdict.description,
            "subjects": verdict.subjects,
            "detected": {s: 1 for s in verdict.subjects},
            "tokens": [verdict.input_tokens, verdict.output_tokens],
            "analyzed_at": time.time(),
            "notified": False,
        }
        if self.notifier.enabled:
            title = verdict.title or "Motion"
            try:
                await self.notifier.send(
                    f"{camera}: {title}", verdict.description, image=verdict.snapshot,
                    tags=",".join(EMOJI[s] for s in verdict.subjects if s in EMOJI),
                    priority=5 if "person" in verdict.subjects else 4)
                event["notified"] = True
            except Exception as exc:  # noqa: BLE001
                log.warning("Notification failed: %s", exc)
                self.last_error = f"Notification failed: {exc}"
        self.store.add(event, verdict.snapshot)

    def _split_stationary(self, camera: str, analysis: ClipAnalysis):
        """Return ({label: conf} worth alerting on, [labels ignored as parked]).

        A box of a STATIONARY_CLASSES object that overlaps a box of the same kind
        from a recent clip on this camera is treated as parked and ignored.
        """
        cutoff = time.time() - config.STATIONARY_WINDOW_HOURS * 3600
        previous = [
            (label, box)
            for e in self.store.events[:200]
            if e["camera"] == camera and e.get("analyzed_at", 0) >= cutoff
            for label, _conf, box in e.get("boxes", [])
        ]
        found: Dict[str, float] = {}
        parked = set()
        for label, conf, box in analysis.boxes:
            if label in config.STATIONARY_CLASSES and any(
                _same_kind(label, old_label) and iou(box, old_box) >= 0.6
                for old_label, old_box in previous
            ):
                parked.add(label)
                continue
            found[label] = max(found.get(label, 0.0), conf)
        return found, sorted(parked - set(found))

    async def _notify(self, camera: str, found: Dict[str, float], snapshot: Optional[bytes]) -> bool:
        now = time.time()
        fresh = {}
        for label, conf in found.items():
            key = f"{camera.lower()}|{label}"
            if now - self._cooldown.get(key, 0) >= config.NOTIFY_COOLDOWN:
                fresh[label] = conf
        if not fresh:
            return False
        for label in fresh:
            self._cooldown[f"{camera.lower()}|{label}"] = now

        labels = sorted(fresh, key=fresh.get, reverse=True)
        title = f"{_human(labels)} at {camera}"
        message = ", ".join(f"{l} {fresh[l]:.0%}" for l in labels)
        tags = ",".join(EMOJI.get(l, "") for l in labels if l in EMOJI)
        try:
            await self.notifier.send(title, message, image=snapshot, tags=tags,
                                     priority=5 if "person" in fresh else 4)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Notification failed: %s", exc)
            self.last_error = f"Notification failed: {exc}"
            return False


_VEHICLES = {"car", "truck", "bus"}  # the model often flips between these for one vehicle


def _same_kind(a: str, b: str) -> bool:
    return a == b or (a in _VEHICLES and b in _VEHICLES)


def _human(labels: List[str]) -> str:
    words = [l.capitalize() if i == 0 else l for i, l in enumerate(labels)]
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]
