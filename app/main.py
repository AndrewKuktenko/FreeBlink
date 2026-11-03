"""HTTP API + static UI."""

import base64
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import re
from typing import Optional

from . import config, pantilt, settings
from .blink_service import READY, BlinkError, BlinkService
from .events import EventWatcher
from .live import LiveManager
from .notifier import Notifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("blink-web")

service = BlinkService()
live = LiveManager()
watcher: EventWatcher = None  # created at startup, needs the HTTP session


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.HOST not in ("127.0.0.1", "localhost", "::1") and not config.APP_PASSWORD:
        raise RuntimeError("Set APP_PASSWORD before exposing the app beyond localhost (HOST=%s)." % config.HOST)
    global watcher
    await service.startup()
    watcher = EventWatcher(service, Notifier(service.session))
    watcher.start()
    yield
    await watcher.stop()
    await live.stop_all()
    await service.shutdown()


app = FastAPI(title="Blink Web", lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if config.APP_PASSWORD:
        header = request.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, config.APP_USERNAME) and secrets.compare_digest(
                    pw, config.APP_PASSWORD
                )
            except Exception:  # noqa: BLE001 - malformed header is just "not authorized"
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Blink Web"'})
    return await call_next(request)


@app.exception_handler(BlinkError)
async def blink_error(_: Request, exc: BlinkError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ---------- auth ----------

class LoginBody(BaseModel):
    username: str
    password: str


class CodeBody(BaseModel):
    code: str


class ToggleBody(BaseModel):
    enabled: bool


@app.get("/api/status")
async def status():
    return {"state": service.state, "error": service.error}


@app.post("/api/login")
async def login(body: LoginBody):
    return {"state": await service.login(body.username, body.password)}


@app.post("/api/2fa")
async def two_factor(body: CodeBody):
    return {"state": await service.verify_2fa(body.code)}


@app.post("/api/logout")
async def logout():
    await live.stop_all()
    await service.logout()
    return {"state": service.state}


# ---------- devices ----------

@app.get("/api/devices")
async def devices():
    return service.devices()


@app.post("/api/refresh")
async def refresh():
    await service.refresh()
    return service.devices()


@app.post("/api/systems/{name}/arm")
async def arm_system(name: str, body: ToggleBody):
    await service.set_system_armed(name, body.enabled)
    return service.devices()


@app.post("/api/cameras/{key}/motion")
async def camera_motion(key: str, body: ToggleBody):
    await service.set_motion(key, body.enabled)
    return service.devices()


@app.post("/api/cameras/{key}/snap")
async def camera_snap(key: str):
    await service.snap(key)
    return service.devices()


@app.get("/api/cameras/{key}/thumbnail")
async def camera_thumbnail(key: str):
    data = await service.thumbnail(key)
    if data is None:
        raise HTTPException(404, "No thumbnail yet")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ---------- clips ----------

@app.get("/api/clips")
async def clips(hours: int = 24):
    items = await service.clips(hours=max(1, min(hours, 24 * 30)))
    analyzed = {e["clip_id"]: e for e in watcher.store.events}
    for clip in items:
        event = analyzed.get(clip["id"])
        if event and event.get("description"):
            clip["ai"] = {"description": event["description"], "title": event.get("title"),
                          "subjects": event.get("subjects") or sorted(event.get("detected") or {})}
    return {"clips": items}


@app.get("/api/clips/{clip_id}/video")
async def clip_video(clip_id: str):
    data = await service.clip_video(clip_id)
    if data is None:
        raise HTTPException(404, "Clip not available")
    return Response(data, media_type="video/mp4", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/clips/{clip_id}/thumbnail")
async def clip_thumbnail(clip_id: str):
    data = await service.clip_thumbnail(clip_id)
    if data is None:
        raise HTTPException(404, "No thumbnail")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ---------- AI detection events ----------

@app.get("/api/events")
async def events(only_detections: bool = False, limit: int = 100):
    items = watcher.store.events
    if only_detections:
        items = [e for e in items if e["detected"]]
    return {"events": items[: max(1, min(limit, 500))], "status": watcher.status()}


@app.get("/api/events/{event_id}/snapshot")
async def event_snapshot(event_id: str):
    data = watcher.store.snapshot(event_id)
    if data is None:
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


@app.post("/api/events/check")
async def events_check():
    """Look for new clips right now instead of waiting for the next poll."""
    if service.state != READY:
        raise BlinkError("Not signed in to Blink.")
    await service.refresh()
    return {"analyzed": await watcher.check_once()}


@app.post("/api/notifications/test")
async def notifications_test():
    if not watcher.notifier.enabled:
        raise BlinkError("Notifications are off. Set NTFY_TOPIC in .env and restart.")
    try:
        await watcher.notifier.send("Blink Web test", "Notifications are working.", tags="white_check_mark")
    except Exception as exc:  # noqa: BLE001
        raise BlinkError(f"Could not send notification: {exc}")
    return {"sent": True}


# ---------- settings (dashboard) ----------

class SettingsBody(BaseModel):
    anthropic_api_key: Optional[str] = None  # "" removes the dashboard key
    ntfy_topic: Optional[str] = None


def _settings_view():
    key = settings.anthropic_api_key()
    return {
        "anthropic_key_set": bool(key),
        "anthropic_key_hint": settings.key_hint(key),
        "anthropic_key_source": settings.anthropic_key_source(),
        "ntfy_server": config.NTFY_SERVER,
        "ntfy_topic": settings.ntfy_topic(),
        "model": config.CLAUDE_MODEL,
        "effort": config.CLAUDE_EFFORT,
        "engine": config.DETECT_ENGINE,
    }


@app.get("/api/settings")
async def get_settings():
    return _settings_view()


@app.post("/api/settings")
async def save_settings(body: SettingsBody):
    import anthropic

    from .claude_vision import check_api_key

    key = body.anthropic_api_key.strip() if body.anthropic_api_key is not None else None
    if key:
        try:
            await check_api_key(key)
        except anthropic.AuthenticationError:
            raise BlinkError("Anthropic rejected this API key. Check that you copied all of it.")
        except anthropic.PermissionDeniedError:
            raise BlinkError("This API key isn't allowed to use the Claude API (check its workspace/permissions).")
        except anthropic.NotFoundError:
            raise BlinkError(f"This key can't access {config.CLAUDE_MODEL}.")
        except (anthropic.APIConnectionError, anthropic.APITimeoutError):
            raise BlinkError("Couldn't reach Anthropic to check the key. Check your internet connection.")
    topic = body.ntfy_topic.strip() if body.ntfy_topic is not None else None
    if topic and not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", topic):
        raise BlinkError("Topic must be 8-64 letters, digits, '-' or '_' (use something hard to guess).")
    settings.update(anthropic_api_key=key, ntfy_topic=topic)
    return _settings_view()


# ---------- live view ----------

@app.post("/api/cameras/{key}/live")
async def live_start(key: str):
    if service.state != READY:
        raise BlinkError("Not signed in to Blink.")
    cam = service.find_camera(key)
    try:
        session = await live.start(key, cam)
    except NotImplementedError as exc:
        raise BlinkError(f"This camera's live view protocol isn't supported: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Live view failed to start")
        raise BlinkError(f"Could not start live view: {exc}")
    if not await session.wait_ready():
        await live.stop(key)
        raise BlinkError("Camera did not start streaming. It may be offline or busy; try again.")
    return {"playlist": f"/live/{key}/index.m3u8"}


class PanTiltBody(BaseModel):
    action: str  # left | right | up | down | home | set_home | overview | stop
    steps: int = 1


@app.get("/api/cameras/{key}/pantilt")
async def pantilt_state(key: str, wait: float = 0):
    """Mount position as reported over the live session. `wait` seconds for a first report."""
    session = live.get(key)
    if not session or not session.stream:
        return {"live": False, "available": False, "position": None}
    pos = session.stream.position
    if pos is None and wait > 0:
        pos = await session.stream.wait_position(min(wait, 15))
    return {"live": True, "available": pos is not None,
            "position": None if pos is None else {"pan": pos.pan, "tilt": pos.tilt, "moving": pos.moving}}


@app.post("/api/cameras/{key}/pantilt")
async def pantilt_command(key: str, body: PanTiltBody):
    session = live.get(key)
    if not session or not session.alive or not session.stream:
        raise BlinkError("Start live view first; the mount is controlled through the live session.")
    stream = session.stream
    session.touch()
    # Mount pan angle grows to the left as seen in the picture.
    deltas = {"left": (config.PAN_STEP, 0), "right": (-config.PAN_STEP, 0),
              "up": (0, config.TILT_STEP), "down": (0, -config.TILT_STEP)}
    if body.action in deltas:
        pos = stream.position or await stream.wait_position(3)
        if pos is None:
            raise BlinkError("No pan-tilt mount position reported for this camera.")
        dp, dt = deltas[body.action]
        n = max(1, min(body.steps, 6))
        frame = pantilt.move(pos.pan + dp * n, pos.tilt + dt * n)
    elif body.action in ("home", "set_home", "overview", "stop"):
        frame = {"home": pantilt.go_home, "set_home": pantilt.set_home,
                 "overview": pantilt.pan_overview, "stop": pantilt.stop}[body.action]()
    else:
        raise BlinkError(f"Unknown pan-tilt action {body.action}")
    stream.position_event = None  # wait for a report that arrives after this command
    await stream.send_command(frame)
    pos = await stream.wait_position(2)
    return {"sent": body.action,
            "position": None if pos is None else {"pan": pos.pan, "tilt": pos.tilt, "moving": pos.moving}}


@app.delete("/api/cameras/{key}/live")
async def live_stop(key: str):
    await live.stop(key)
    return {"stopped": True}


@app.get("/live/{key}/{filename}")
async def live_file(key: str, filename: str):
    session = live.get(key)
    if not session or "/" in filename or not (filename.endswith(".m3u8") or filename.endswith(".ts")):
        raise HTTPException(404)
    path = session.dir / filename
    if not path.exists():
        raise HTTPException(404)
    session.touch()
    media = "application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t"
    return FileResponse(path, media_type=media, headers={"Cache-Control": "no-store"})


# ---------- UI ----------

STATIC = config.ROOT / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")
