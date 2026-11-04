"""Small ffmpeg helpers for clip media."""

import asyncio
import tempfile
from typing import List, Tuple

from . import config


async def _ffmpeg(*args: str, data: bytes = b"") -> bytes:
    proc = await asyncio.create_subprocess_exec(
        config.FFMPEG, "-v", "error", *args,
        stdin=asyncio.subprocess.PIPE if data else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate(data or None)
    return out


async def extract_frames(video: bytes, count: int, max_width: int = 1280) -> List[bytes]:
    """Grab `count` evenly spaced frames as JPEGs (at most `max_width` px wide)."""
    frames, _ = await frames_and_duration(video, count, max_width)
    return frames


async def frames_and_duration(video: bytes, count: int, max_width: int = 1280) -> Tuple[List[bytes], float]:
    """Like extract_frames, plus the clip length in seconds (0 if unknown)."""
    with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
        tmp.write(video)
        tmp.flush()
        probe = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", tmp.name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await probe.communicate()
        try:
            duration = float(out.decode().strip())
        except ValueError:
            duration = 0.0
        times = [duration * (i + 0.5) / count for i in range(count)] if duration > 0 else [0.0]

        frames = []
        for t in times:
            jpeg = await _ffmpeg("-ss", f"{t:.2f}", "-i", tmp.name, "-frames:v", "1",
                                 "-vf", f"scale='min({max_width},iw)':-2", "-q:v", "3",
                                 "-f", "image2pipe", "-vcodec", "mjpeg", "-")
            if jpeg:
                frames.append(jpeg)
        return frames, duration


async def downscale(jpeg: bytes, width: int) -> bytes:
    """Re-encode a JPEG at `width` px wide (keeps aspect ratio). Returns the input on failure."""
    out = await _ffmpeg("-f", "image2pipe", "-i", "-", "-vf", f"scale='min({width},iw)':-2",
                        "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "-", data=jpeg)
    return out or jpeg
