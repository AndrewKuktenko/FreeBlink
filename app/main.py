"""HTTP API + static UI. Every /api route acts on the Blink account of the signed-in browser."""

import hashlib
import base64
import logging
import re
import secrets
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, pantilt
from .blink_service import NEEDS_2FA, READY, BlinkError
from .faces import available_for, group_faces, person_view, shared_face_engine
from .live import LiveLimitError
from .settings import key_hint
from .tenants import Tenant, TenantManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("freeblink")

SESSION_COOKIE = "fb_session"
PENDING_COOKIE = "fb_pending"

manager = TenantManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.startup()
    yield
    await manager.shutdown()


app = FastAPI(title="FreeBlink", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    # Optional site-wide password (private deployments).
    if config.APP_PASSWORD:
        header = request.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, config.APP_USERNAME) and secrets.compare_digest(
                    pw, config.APP_PASSWORD)
            except Exception:  # noqa: BLE001 - malformed header is just "not authorized"
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="FreeBlink"'})
    # Cross-site request protection: state-changing calls must come from this site.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse(status_code=403, content={"detail": "Cross-site request blocked."})
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


@app.exception_handler(BlinkError)
async def blink_error(_: Request, exc: BlinkError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(LiveLimitError)
async def live_limit_error(_: Request, exc: LiveLimitError):
    return JSONResponse(status_code=429, content={"detail": str(exc)})


def _set_cookie(request: Request, response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def current(request: Request) -> Tenant:
    tenant = await manager.for_session(request.cookies.get(SESSION_COOKIE))
    if tenant is None:
        raise HTTPException(401, "Not signed in.")
    tenant.settings.remember_site_url(str(request.base_url))
    return tenant


# ---------- auth ----------

class LoginBody(BaseModel):
    username: str
    password: str


class CodeBody(BaseModel):
    code: str


class ToggleBody(BaseModel):
    enabled: bool


def _mask(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return None
    name, _, domain = email.partition("@")
    return f"{name[:2]}{'•' * max(1, len(name) - 2)}@{domain}"


@app.get("/api/status")
async def status(request: Request):
    tenant = await manager.for_session(request.cookies.get(SESSION_COOKIE))
    if tenant:
        return {"state": READY, "account": _mask(tenant.service.username)}
    if manager.pending_for(request.cookies.get(PENDING_COOKIE)):
        return {"state": NEEDS_2FA}
    return {"state": "logged_out"}


@app.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response):
    if not body.username.strip() or not body.password:
        raise BlinkError("Enter your Blink email and password.")
    await manager.cancel_pending(request.cookies.get(PENDING_COOKIE))
    state, token = await manager.login(_client_ip(request), body.username.strip(), body.password)
    if state == NEEDS_2FA:
        _set_cookie(request, response, PENDING_COOKIE, token, 600)
    else:
        _set_cookie(request, response, SESSION_COOKIE, token, config.SESSION_DAYS * 86400)
    return {"state": state}


@app.post("/api/2fa")
async def two_factor(body: CodeBody, request: Request, response: Response):
    token = await manager.verify_2fa(_client_ip(request), request.cookies.get(PENDING_COOKIE), body.code)
    response.delete_cookie(PENDING_COOKIE, path="/")
    _set_cookie(request, response, SESSION_COOKIE, token, config.SESSION_DAYS * 86400)
    return {"state": READY}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    manager.sign_out(request.cookies.get(SESSION_COOKIE))
    await manager.cancel_pending(request.cookies.get(PENDING_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(PENDING_COOKIE, path="/")
    return {"state": "logged_out"}


@app.post("/api/account/delete")
async def delete_account(response: Response, tenant: Tenant = Depends(current)):
    await manager.delete_account(tenant.id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"state": "logged_out"}


# ---------- devices ----------

@app.get("/api/devices")
async def devices(tenant: Tenant = Depends(current)):
    return tenant.service.devices()


@app.post("/api/refresh")
async def refresh(tenant: Tenant = Depends(current)):
    await tenant.service.refresh()
    return tenant.service.devices()


@app.post("/api/systems/{name}/arm")
async def arm_system(name: str, body: ToggleBody, tenant: Tenant = Depends(current)):
    await tenant.service.set_system_armed(name, body.enabled)
    return tenant.service.devices()


@app.post("/api/cameras/{key}/motion")
async def camera_motion(key: str, body: ToggleBody, tenant: Tenant = Depends(current)):
    await tenant.service.set_motion(key, body.enabled)
    return tenant.service.devices()


@app.post("/api/cameras/{key}/snap")
async def camera_snap(key: str, tenant: Tenant = Depends(current)):
    await tenant.service.snap(key)
    return tenant.service.devices()


@app.get("/api/cameras/{key}/thumbnail")
async def camera_thumbnail(key: str, tenant: Tenant = Depends(current)):
    data = await tenant.service.thumbnail(key)
    if data is None:
        raise HTTPException(404, "No thumbnail yet")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ---------- clips ----------

@app.get("/api/clips")
async def clips(hours: int = 24, tenant: Tenant = Depends(current)):
    items = await tenant.service.clips(hours=max(1, min(hours, 24 * 30)))
    analyzed = {e["clip_id"]: e for e in tenant.watcher.store.events}
    for clip in items:
        event = analyzed.get(clip["id"])
        if event and event.get("description"):
            clip["ai"] = {"description": event["description"], "title": event.get("title"),
                          "subjects": event.get("subjects") or sorted(event.get("detected") or {})}
        if event and event.get("people"):
            clip["people"] = event["people"]
    return {"clips": items, "ai_enabled": bool(tenant.settings.anthropic_api_key())}


@app.get("/api/clips/{clip_id}/video")
async def clip_video(clip_id: str, tenant: Tenant = Depends(current)):
    data = await tenant.service.clip_video(clip_id)
    if data is None:
        raise HTTPException(404, "Clip not available")
    return Response(data, media_type="video/mp4", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/clips/{clip_id}/thumbnail")
async def clip_thumbnail(clip_id: str, tenant: Tenant = Depends(current)):
    data = await tenant.service.clip_thumbnail(clip_id)
    if data is None:
        raise HTTPException(404, "No thumbnail")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ---------- AI detection events ----------

@app.get("/api/events")
async def events(only_detections: bool = False, limit: int = 100, tenant: Tenant = Depends(current)):
    items = tenant.watcher.store.events
    if only_detections:
        items = [e for e in items if e["detected"]]
    return {"events": items[: max(1, min(limit, 500))], "status": tenant.watcher.status()}


@app.get("/api/events/{event_id}/snapshot")
async def event_snapshot(event_id: str, tenant: Tenant = Depends(current)):
    data = tenant.watcher.store.snapshot(event_id)
    if data is None:
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


@app.post("/api/notifications/test")
async def notifications_test(tenant: Tenant = Depends(current)):
    try:
        await tenant.watcher.notifier.send("FreeBlink test", "Notifications are working.", tags="white_check_mark")
    except Exception as exc:  # noqa: BLE001
        raise BlinkError(f"Could not send notification: {exc}")
    return {"sent": True}


# ---------- settings ----------

class SettingsBody(BaseModel):
    anthropic_api_key: Optional[str] = None  # "" removes the key
    ntfy_topic: Optional[str] = None


def _settings_view(tenant: Tenant):
    key = tenant.settings.anthropic_api_key()
    return {
        "anthropic_key_set": bool(key),
        "anthropic_key_hint": key_hint(key),
        "ntfy_server": config.NTFY_SERVER,
        "ntfy_topic": tenant.settings.ntfy_topic(),
        "model": config.CLAUDE_MODEL,
        "effort": config.CLAUDE_EFFORT,
        "engine": config.DETECT_ENGINE,
        "account": _mask(tenant.service.username),
    }


@app.get("/api/settings")
async def get_settings(tenant: Tenant = Depends(current)):
    return _settings_view(tenant)


@app.post("/api/settings")
async def save_settings(body: SettingsBody, tenant: Tenant = Depends(current)):
    import anthropic

    from .claude_vision import check_api_key

    if body.anthropic_api_key is not None:
        key = body.anthropic_api_key.strip()
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
                raise BlinkError("Couldn't reach Anthropic to check the key. Try again.")
        tenant.settings.set_anthropic_api_key(key or None)
    if body.ntfy_topic is not None:
        topic = body.ntfy_topic.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", topic):
            raise BlinkError("Topic must be 8-64 letters, digits, '-' or '_' (use something hard to guess).")
        tenant.settings.set_ntfy_topic(topic)
    return _settings_view(tenant)


# ---------- familiar faces ----------

class FacesToggleBody(BaseModel):
    enabled: bool
    consent: bool = False  # the owner confirms the people they add agreed to be recognized


class EnrollBody(BaseModel):
    token: str
    name: str


def _faces_view(tenant: Tenant):
    available = available_for(tenant.service.username)
    return {
        "available": available,
        "enabled": available and tenant.settings.faces_enabled(),
        "people": [person_view(p) for p in tenant.faces.people()] if available else [],
    }


def _require_faces(tenant: Tenant) -> None:
    if not available_for(tenant.service.username):
        raise HTTPException(404, "Familiar faces aren't available on this server.")
    if not tenant.settings.faces_enabled():
        raise BlinkError("Turn on familiar faces in Settings first.")


async def _offer_faces(tenant: Tenant, find) -> dict:
    try:
        faces = await find(shared_face_engine())
    except Exception as exc:  # noqa: BLE001 - model download or decode problem
        log.exception("Face search failed")
        raise BlinkError(f"Couldn't look for faces: {exc}")
    groups = group_faces(faces)
    return {"faces": await tenant.faces.offer(groups), "seen": len(faces)}


@app.get("/api/faces")
async def faces_get(tenant: Tenant = Depends(current)):
    return _faces_view(tenant)


@app.post("/api/faces/settings")
async def faces_settings(body: FacesToggleBody, tenant: Tenant = Depends(current)):
    if not available_for(tenant.service.username):
        raise HTTPException(404, "Familiar faces aren't available on this server.")
    if body.enabled and not body.consent:
        raise BlinkError("Confirm that the people you add agreed to be recognized.")
    tenant.settings.set_faces_enabled(body.enabled)
    return _faces_view(tenant)


@app.get("/api/clips/{clip_id}/faces")
async def clip_faces(clip_id: str, tenant: Tenant = Depends(current)):
    _require_faces(tenant)
    video = await tenant.service.clip_video(clip_id)
    if video is None:
        raise HTTPException(404, "Clip not available")
    return await _offer_faces(tenant, lambda engine: engine.faces_in_clip(video))


@app.post("/api/faces/photo")
async def photo_faces(request: Request, tenant: Tenant = Depends(current)):
    _require_faces(tenant)
    if int(request.headers.get("content-length") or 0) > 15 * 1024 * 1024:
        raise BlinkError("That photo is too large (15 MB at most).")
    image = await request.body()
    if not image:
        raise BlinkError("Choose a photo first.")
    if len(image) > 15 * 1024 * 1024:
        raise BlinkError("That photo is too large (15 MB at most).")
    return await _offer_faces(tenant, lambda engine: engine.faces_in_photo(image))


@app.post("/api/faces/people")
async def faces_enroll(body: EnrollBody, tenant: Tenant = Depends(current)):
    _require_faces(tenant)
    try:
        tenant.faces.add(body.name, body.token)
    except ValueError as exc:
        raise BlinkError(str(exc))
    return _faces_view(tenant)


@app.delete("/api/faces/people/{person_id}")
async def faces_remove(person_id: str, tenant: Tenant = Depends(current)):
    if not available_for(tenant.service.username):
        raise HTTPException(404)
    tenant.faces.remove(person_id)  # allowed while switched off: deleting is always possible
    return _faces_view(tenant)


# ---------- live view ----------

@app.post("/api/cameras/{key}/live")
async def live_start(key: str, tenant: Tenant = Depends(current)):
    cam = tenant.service.find_camera(key)
    try:
        session = await tenant.live.start(key, cam)
    except LiveLimitError:
        raise
    except NotImplementedError as exc:
        raise BlinkError(f"This camera's live view protocol isn't supported: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Live view failed to start")
        raise BlinkError(f"Could not start live view: {exc}")
    if not await session.wait_ready():
        await tenant.live.stop(key)
        raise BlinkError("Camera did not start streaming. It may be offline or busy; try again.")
    return {"playlist": f"/live/{key}/index.m3u8"}


class PanTiltBody(BaseModel):
    action: str  # left | right | up | down | home | set_home | overview | stop
    steps: int = 1


@app.get("/api/cameras/{key}/pantilt")
async def pantilt_state(key: str, wait: float = 0, tenant: Tenant = Depends(current)):
    """Mount position as reported over the live session. `wait` seconds for a first report."""
    session = tenant.live.get(key)
    if not session or not session.stream:
        return {"live": False, "available": False, "position": None}
    pos = session.stream.position
    if pos is None and wait > 0:
        pos = await session.stream.wait_position(min(wait, 15))
    return {"live": True, "available": pos is not None,
            "position": None if pos is None else {"pan": pos.pan, "tilt": pos.tilt, "moving": pos.moving}}


@app.post("/api/cameras/{key}/pantilt")
async def pantilt_command(key: str, body: PanTiltBody, tenant: Tenant = Depends(current)):
    session = tenant.live.get(key)
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
async def live_stop(key: str, tenant: Tenant = Depends(current)):
    await tenant.live.stop(key)
    return {"stopped": True}


@app.get("/live/{key}/{filename}")
async def live_file(key: str, filename: str, tenant: Tenant = Depends(current)):
    session = tenant.live.get(key)
    if not session or not re.fullmatch(r"(index\.m3u8|seg_\d+\.ts)", filename):
        raise HTTPException(404)
    path = session.dir / filename
    if not path.exists():
        raise HTTPException(404)
    session.touch()
    media = "application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t"
    return FileResponse(path, media_type=media, headers={"Cache-Control": "no-store"})


# ---------- UI ----------

STATIC = config.ROOT / "static"


class RevalidatedStatic(StaticFiles):
    """Browsers must check for a newer app.js/style.css on every load (a cheap 304 when unchanged),
    or phones keep running an old version after a deploy."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatedStatic(directory=STATIC), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC / "favicon.ico", headers={"Cache-Control": "public, max-age=86400"})


def _versioned_index() -> str:
    """index.html with a content hash on each /static/ URL, so a deploy never runs against
    an old app.js still held by Cloudflare or the browser."""

    def stamp(match: re.Match) -> str:
        path = STATIC / match.group(1)
        if not path.is_file():
            return match.group(0)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
        return f'"/static/{match.group(1)}?v={digest}"'

    return re.sub(r'"/static/([^"?]+)"', stamp, (STATIC / "index.html").read_text())


INDEX_HTML = _versioned_index()


@app.get("/")
async def index():
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})
