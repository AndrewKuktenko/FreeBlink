"""Watch Blink for new motion clips, run detection, store events, notify."""

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config
from .blink_service import READY, BlinkService, _ts
from .detector import ClipAnalysis, Detector, iou
from .faces import FaceAccount, available_for, match_clip, shared_face_engine
from .notifier import Notifier
from .settings import UserSettings

if TYPE_CHECKING:
    from .claude_vision import ConcernContext

log = logging.getLogger(__name__)

EMOJI = {"person": "bust_in_silhouette", "car": "red_car", "truck": "truck", "dog": "dog",
         "cat": "cat", "bicycle": "bike", "motorcycle": "motorcycle", "bird": "bird"}


class EventStore:
    def __init__(self, folder: Path) -> None:
        self.dir = folder
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

    def update_clip(self, clip_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """Change fields on the newest event for `clip_id`. Returns it, or None if there's none."""
        for event in self.events:
            if event["clip_id"] == clip_id:
                event.update(fields)
                self._write(self.index_file, self.events)
                return event
        return None

    def snapshot(self, event_id: str) -> Optional[bytes]:
        if not event_id.replace("-", "").isalnum():
            return None
        path = self.dir / f"{event_id}.jpg"
        return path.read_bytes() if path.exists() else None


_detector: Optional[Detector] = None


def shared_detector() -> Detector:
    """One YOLO model for all accounts; it only loads if DETECT_ENGINE=yolo."""
    global _detector
    if _detector is None:
        _detector = Detector()
    return _detector


class EventWatcher:
    def __init__(self, service: BlinkService, notifier: Notifier, folder: Path, settings: UserSettings,
                 faces: Optional[FaceAccount] = None) -> None:
        self.service = service
        self.notifier = notifier
        self.settings = settings
        self.faces = faces or FaceAccount(folder)
        self.detector = shared_detector()
        self._claude = None
        self.store = EventStore(folder / "events")
        self._task: Optional[asyncio.Task] = None
        self._cooldown: Dict[str, float] = {}
        self._alerted: Dict[str, float] = {}  # camera -> last urgent suspicious-activity push
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
            "api_key_set": bool(self.settings.anthropic_api_key()),
            "classes": config.DETECT_CLASSES,
            "min_confidence": config.DETECT_MIN_CONFIDENCE,
            "cameras": config.DETECT_CAMERAS or "all",
            "notifications": self.notifier.enabled,
            "ntfy_topic": self.settings.ntfy_topic(),
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
        no_key = config.DETECT_ENGINE == "claude" and self._claude_analyzer() is None
        if no_key and not self.faces_active():
            await self._notify_plain(item)  # no key: don't download clips we can't describe
            return
        video = await self.service.clip_video(clip_id)
        if not video:
            log.warning("Could not download clip %s from %s", clip_id, camera)
            return
        people = await self._recognize(clip_id, video)
        if no_key:
            await self._notify_plain(item, people)
            return
        if config.DETECT_ENGINE == "claude":
            await self._process_claude(item, video, people)
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
            "people": people,
            "analyzed_at": time.time(),
            "notified": False,
        }
        if found and self.notifier.enabled:
            event["notified"] = await self._notify(camera, found, analysis.snapshot, people)
        self.store.add(event, analysis.snapshot)

    def _claude_analyzer(self):
        key = self.settings.anthropic_api_key()
        if not key:
            return None
        if self._claude is None or self._claude.api_key != key:  # key changed in Settings
            from .claude_vision import ClaudeAnalyzer

            self._claude = ClaudeAnalyzer(key)
        return self._claude

    def faces_active(self) -> bool:
        return available_for(self.service.username) and self.settings.faces_enabled()

    async def _recognize(self, clip_id: str, video: bytes) -> List[str]:
        """Names of enrolled people seen in the clip ([] if face recognition is off or unsure)."""
        if not self.faces_active():
            return []
        try:
            templates = self.faces.templates()
            if not templates:
                return []
            started = time.monotonic()
            faces = await shared_face_engine().faces_in_clip(video)
            matches = match_clip(faces, templates)
            ids = [pid for pid, _ in matches]
            if ids:
                self.faces.mark_seen(ids)
            names = self.faces.names(ids)
            log.info("Clip %s: %d face(s), %d usable, %.1fs -> %s", clip_id, len(faces),
                     sum(f.usable for f in faces), time.monotonic() - started,
                     ", ".join(f"{n} {s:.2f}" for n, (_, s) in zip(names, matches)) or "no one familiar")
            return names
        except Exception as exc:  # noqa: BLE001 - never lose the description over a face problem
            log.exception("Face recognition failed for clip %s", clip_id)
            self.last_error = f"Face recognition failed: {exc}"
            return []

    async def _process_claude(self, item: Dict[str, Any], video: bytes, people: Optional[List[str]] = None) -> None:
        """Describe every clip and push the description to the phone."""
        import anthropic

        clip_id = str(item["id"])
        camera = item.get("camera") or "Camera"
        analyzer = self._claude_analyzer()
        if analyzer is None:  # key removed while the clip downloaded
            await self._notify_plain(item)
            return
        concern = self._concern_context(item) if self.settings.suspicious_enabled() else None
        started = time.monotonic()
        try:
            verdict = await analyzer.analyze_clip(video, camera, people=people or [], concern=concern)
        except anthropic.BadRequestError as exc:
            # Something about this clip the API won't take; record it and move on.
            log.warning("Claude rejected clip %s: %s", clip_id, exc.message)
            self.last_error = f"Claude rejected clip {clip_id}: {exc.message}"
            return
        # Rate limits, outages and auth errors propagate: the clip stays unseen and is retried.
        log.info("Clip %s (%s): %d frames, %.1fs, %d in / %d out tokens [%s] -> %s: %s%s",
                 clip_id, camera, verdict.frames_analyzed, time.monotonic() - started,
                 verdict.input_tokens, verdict.output_tokens, verdict.model,
                 verdict.title, verdict.description,
                 f" [{verdict.concern}: {verdict.concern_evidence}]" if verdict.concern != "none" else "")

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
            "people": people or [],
            "detected": {s: 1 for s in verdict.subjects},
            "tokens": [verdict.input_tokens, verdict.output_tokens],
            "analyzed_at": time.time(),
            "notified": False,
        }
        if verdict.concern_checked:
            event["concern"] = verdict.concern
            event["concern_evidence"] = verdict.concern_evidence
        if self.notifier.enabled:
            title = f"{camera}: {verdict.title or 'Motion'}"
            message = verdict.description
            tags = [EMOJI[s] for s in verdict.subjects if s in EMOJI]
            if concern is None:  # feature off: notifications exactly as before
                priority = 5 if "person" in verdict.subjects else 4
            else:
                # Only flagged clips get the most urgent priority, so the owner can let just those
                # through Do Not Disturb. One urgent push per camera per cooldown; threats always.
                priority = 4
                if verdict.concern != "none":
                    label = "Alert" if verdict.concern == "threat" else "Suspicious"
                    title = f"{label} at {camera}: {verdict.title or 'Motion'}"
                    message = f"{verdict.description}\nFlagged: {verdict.concern_evidence}."
                    tags.insert(0, "rotating_light" if verdict.concern == "threat" else "warning")
                    now = time.time()
                    if (verdict.concern == "threat"
                            or now - self._alerted.get(camera.lower(), 0) >= config.CONCERN_ALERT_COOLDOWN):
                        self._alerted[camera.lower()] = now
                        priority = 5
            try:
                await self.notifier.send(title, message, image=verdict.snapshot, tags=",".join(tags),
                                         priority=priority, clip=clip_id)
                event["notified"] = True
            except Exception as exc:  # noqa: BLE001
                log.warning("Notification failed: %s", exc)
                self.last_error = f"Notification failed: {exc}"
        self.store.add(event, verdict.snapshot)

    def _concern_context(self, item: Dict[str, Any]) -> "ConcernContext":
        """Local time, the owner's note, and captions of recent clips on the same camera.

        History stays at the level of clips: captions only, never images or faces, and nothing
        that links people across clips by how they look."""
        from .claude_vision import ConcernContext

        camera = item.get("camera") or "Camera"
        created = _ts(item.get("created_at")) or time.time()
        context = ConcernContext(normal=self.settings.normal_activity())
        zone = self.settings.time_zone()
        if zone:
            try:
                context.local_time = datetime.fromtimestamp(created, ZoneInfo(zone)).strftime("%a %H:%M")
            except (ZoneInfoNotFoundError, ValueError):
                pass  # unknown zone: better no time than a wrong one
        window = config.CONCERN_HISTORY_MINUTES * 60
        earlier = []
        for event in self.store.events[:200]:  # newest first
            if event.get("camera") != camera or not event.get("title"):
                continue
            ago = created - _ts(event.get("created_at"))
            if not 0 < ago <= window:
                continue
            if event.get("concern_cleared"):
                level = " (owner marked it not suspicious)"
            elif event.get("concern") in ("suspicious", "threat"):
                level = f" ({event['concern']}: {event.get('concern_evidence', '')})"
            else:
                level = ""
            earlier.append(f'{max(1, round(ago / 60))} min earlier "{event["title"]}"{level}')
            if len(earlier) == 5:
                break
        context.earlier = earlier[::-1]  # oldest first, like the frames
        return context

    async def _notify_plain(self, item: Dict[str, Any], people: Optional[List[str]] = None) -> None:
        if not self.notifier.enabled:
            return
        camera = item.get("camera") or "Camera"
        title = f"{camera}: {_and(people)}" if people else f"{camera}: Motion"
        try:
            await self.notifier.send(title, "New motion clip. Add a Claude API key in "
                                     "FreeBlink Settings to get a description.", tags="movie_camera",
                                     clip=str(item["id"]))
        except Exception as exc:  # noqa: BLE001
            log.warning("Notification failed: %s", exc)

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

    async def _notify(self, camera: str, found: Dict[str, float], snapshot: Optional[bytes],
                      people: Optional[List[str]] = None) -> bool:
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
        title = f"{_and(people) if people else _human(labels)} at {camera}"
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


def _and(names: List[str]) -> str:
    """Names as written: "Mom", "Mom and Dad", "Mom, Dad and Sam"."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
