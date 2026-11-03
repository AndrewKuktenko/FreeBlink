"""Live view: Blink "immis" stream -> local MPEG-TS socket -> ffmpeg -> HLS files.

blinkpy's BlinkLiveStream speaks Blink's proprietary protocol and re-serves the
raw MPEG-TS on a local TCP port. ffmpeg connects to that port and writes a
rolling HLS playlist the browser can play with hls.js (or natively in Safari).
"""

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Dict, Optional

from blinkpy import api
from blinkpy.camera import BlinkCamera
from blinkpy.livestream import BlinkLiveStream

from . import config, pantilt

log = logging.getLogger(__name__)


class ReliableLiveStream(BlinkLiveStream):
    """BlinkLiveStream with a fixed receive loop.

    blinkpy 0.25.9 reads each immis packet with ``StreamReader.read(n)``, which
    returns *up to* n bytes. When a packet arrives split across TLS records it
    gets a short read, logs "Insufficient data for payload", and kills the
    stream. ``readexactly`` waits for the whole header/payload instead.

    It also reads Pan-Tilt Mount position reports from the same socket and can
    send mount commands (see pantilt.py).
    """

    position: Optional[pantilt.Position] = None
    position_event: Optional[asyncio.Event] = None

    def _position_event(self) -> asyncio.Event:
        if self.position_event is None:
            self.position_event = asyncio.Event()
        return self.position_event

    async def send_command(self, frame: bytes) -> None:
        if self.target_writer is None or self.target_writer.is_closing():
            raise RuntimeError("Live session is not connected")
        self.target_writer.write(frame)
        await self.target_writer.drain()

    async def wait_position(self, timeout: float) -> Optional[pantilt.Position]:
        try:
            await asyncio.wait_for(self._position_event().wait(), timeout)
        except asyncio.TimeoutError:
            pass
        return self.position

    async def recv(self):
        try:
            while True:
                header = await self.target_reader.readexactly(9)
                msgtype = header[0]
                length = int.from_bytes(header[5:9], "big")
                if length <= 0:
                    continue
                payload = await self.target_reader.readexactly(length)
                if msgtype == pantilt.ACCESSORY_MESSAGE:
                    ident = int.from_bytes(header[1:5], "big")
                    pos = pantilt.parse_position(ident, payload)
                    if pos is not None:
                        self.position = pos
                        self._position_event().set()
                    continue
                # 0x00 = video/audio MPEG-TS; others are control/keep-alive messages.
                if msgtype != 0x00 or payload[0] != 0x47:
                    continue
                for writer in list(self.clients):
                    if not writer.is_closing():
                        writer.write(payload)
                        await writer.drain()
        except asyncio.IncompleteReadError:
            log.info("Blink closed the live stream for %s", self.camera.name)
        except Exception:  # noqa: BLE001
            log.exception("Error while receiving live stream for %s", self.camera.name)
        finally:
            self.target_writer.close()


async def open_livestream(camera: BlinkCamera) -> ReliableLiveStream:
    """Same request as BlinkCamera.init_livestream, but with the fixed stream class."""
    response = await api.request_camera_liveview(
        camera.sync.blink, camera.sync.network_id, camera.camera_id, camera_type=camera.camera_type
    )
    if not response or "server" not in response:
        raise RuntimeError(f"Blink refused live view: {response}")
    if not response["server"].startswith("immis://"):
        raise NotImplementedError(response["server"])
    return ReliableLiveStream(camera, response)


class LiveSession:
    def __init__(self, key: str, camera: BlinkCamera, folder: Path):
        self.key = key
        self.camera = camera
        self.dir = folder / "live" / key
        self.stills_dir = folder / "stills"
        self.stream = None
        self.ffmpeg: Optional[asyncio.subprocess.Process] = None
        self.feed_task: Optional[asyncio.Task] = None
        self.watch_task: Optional[asyncio.Task] = None
        self.last_access = time.monotonic()
        self.started_at = time.time()
        self.stopped = False

    @property
    def playlist(self) -> Path:
        return self.dir / "index.m3u8"

    @property
    def alive(self) -> bool:
        return not self.stopped and self.ffmpeg is not None and self.ffmpeg.returncode is None

    def touch(self) -> None:
        self.last_access = time.monotonic()

    async def start(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)

        self.stream = await open_livestream(self.camera)
        await self.stream.start(host="127.0.0.1", port=0)
        log.info("Live view for %s relaying on %s", self.camera.name, self.stream.url)

        self.ffmpeg = await asyncio.create_subprocess_exec(
            config.FFMPEG,
            "-hide_banner", "-loglevel", "warning",
            "-fflags", "+genpts+nobuffer",
            "-i", self.stream.url,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "64k",
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "6",
            "-hls_flags", "delete_segments+independent_segments+omit_endlist",
            "-hls_segment_filename", str(self.dir / "seg_%05d.ts"),
            str(self.playlist),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self.feed_task = asyncio.create_task(self.stream.feed())
        self.watch_task = asyncio.create_task(self._watch())

    async def wait_ready(self, timeout: float = 40.0) -> bool:
        """Wait until ffmpeg has written a playlist that lists at least one segment.

        Battery cameras and doorbells need 10-20 s to wake up, so the idle
        watchdog is held off (touch) while we wait.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.touch()
            if not self.alive:
                return False
            if self.playlist.exists() and ".ts" in self.playlist.read_text(errors="ignore"):
                asyncio.create_task(self._save_still())
                return True
            await asyncio.sleep(0.5)
        return False

    async def _save_still(self) -> None:
        """Keep the latest live frame as this camera's picture (used when Blink has none)."""
        await asyncio.sleep(3)
        segments = sorted(self.dir.glob("seg_*.ts"))
        if not segments:
            return
        self.stills_dir.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            config.FFMPEG, "-v", "error", "-y", "-i", str(segments[-1]), "-frames:v", "1", "-q:v", "3",
            str(self.stills_dir / f"{self.key}.jpg"),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    async def _watch(self) -> None:
        try:
            while not self.stopped:
                await asyncio.sleep(2)
                if self.ffmpeg.returncode is not None:
                    err = await self.ffmpeg.stderr.read()
                    log.info("ffmpeg for %s exited (%s): %s",
                             self.camera.name, self.ffmpeg.returncode, err.decode(errors="ignore")[-500:])
                    break
                if self.feed_task.done():
                    log.info("Blink ended the live view for %s", self.camera.name)
                    break
                if time.monotonic() - self.last_access > config.LIVE_IDLE_TIMEOUT:
                    log.info("Live view for %s idle, stopping", self.camera.name)
                    break
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        if self.stream is not None:
            self.stream.stop()
        if self.ffmpeg is not None and self.ffmpeg.returncode is None:
            self.ffmpeg.terminate()
            try:
                await asyncio.wait_for(self.ffmpeg.wait(), 5)
            except asyncio.TimeoutError:
                self.ffmpeg.kill()
        if self.feed_task is not None and not self.feed_task.done():
            self.feed_task.cancel()
        shutil.rmtree(self.dir, ignore_errors=True)


class LiveLimitError(Exception):
    pass


class LiveManager:
    """Live sessions of one Blink account. All managers share the server-wide stream limit."""

    _all: "set[LiveManager]" = set()

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.sessions: Dict[str, LiveSession] = {}
        self._lock = asyncio.Lock()
        LiveManager._all.add(self)

    def active(self) -> int:
        return sum(1 for s in self.sessions.values() if not s.stopped)

    @classmethod
    def active_total(cls) -> int:
        return sum(m.active() for m in cls._all)

    async def start(self, key: str, camera: BlinkCamera) -> LiveSession:
        async with self._lock:
            existing = self.sessions.get(key)
            if existing and existing.alive:
                existing.touch()
                return existing
            if existing:
                await existing.stop()
                self.sessions.pop(key, None)
            if self.active() >= config.MAX_LIVE_PER_USER:
                raise LiveLimitError(f"You can watch up to {config.MAX_LIVE_PER_USER} cameras live at once. "
                                     "Close another live view first.")
            if LiveManager.active_total() >= config.MAX_LIVE_TOTAL:
                raise LiveLimitError("The server is busy with other live views. Try again in a minute.")
            session = LiveSession(key, camera, self.folder)
            self.sessions[key] = session
            try:
                await session.start()
            except Exception:
                await session.stop()
                self.sessions.pop(key, None)
                raise
            return session

    def get(self, key: str) -> Optional[LiveSession]:
        session = self.sessions.get(key)
        return session if session and not session.stopped else None

    async def stop(self, key: str) -> None:
        session = self.sessions.pop(key, None)
        if session:
            await session.stop()

    async def stop_all(self) -> None:
        for key in list(self.sessions):
            await self.stop(key)

    def close(self) -> None:
        LiveManager._all.discard(self)
