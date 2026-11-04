"""Familiar-face recognition, run on this machine.

Models: InsightFace's buffalo_s pack (SCRFD-500M face detector + ArcFace MobileFaceNet recognizer),
run with onnxruntime. The pack is licensed for non-commercial use; it's downloaded from InsightFace's
own release on first use and never shipped in the image. Frames come from ffmpeg (PPM over a pipe),
and alignment/resizing is plain numpy, so there's no OpenCV dependency.

Only enrolled people are stored: a few 512-number face templates and a small thumbnail per person,
encrypted in the account's folder (faces.enc). Faces of anyone else are compared and dropped.
Research and spike results: docs/research/face-recognition.md.
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import tempfile
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config, crypto
from .media import _ffmpeg

log = logging.getLogger(__name__)

MODEL_NAME = "buffalo_s"
MODEL_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_s.zip"
MODEL_FILES = {  # SHA-256 of the two files we use from the pack
    "det_500m.onnx": "5e4447f50245bbd7966bd6c0fa52938c61474a04ec7def48753668a9d8b4ea3a",
    "w600k_mbf.onnx": "9cc6e4a75f0e2bf0b1aed94578f144d15175f357bdc05e815e5c4a02b319eb4f",
}

# Where ArcFace expects the eyes, nose and mouth corners in its 112x112 input.
_ARCFACE_DST = [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                [41.5493, 92.3655], [70.7299, 92.2041]]
DET_SIZE = 640
MAX_SAMPLES = 20  # templates kept per person
MAX_PEOPLE = 50


@dataclass
class Face:
    frame: int
    box: List[float]          # x1, y1, x2, y2 in frame pixels
    score: float              # detector confidence
    width: float
    gray: bool                # infrared / black-and-white frame
    frontal: bool
    embedding: Any = None     # unit-length float32[512]
    crop: Any = None          # aligned 112x112 RGB face

    @property
    def usable(self) -> bool:
        """Good enough to compare with enrolled faces (or to enroll)."""
        return (self.score >= config.FACES_MIN_SCORE and self.width >= config.FACES_MIN_SIZE
                and self.frontal and not self.gray)


# ---------- images (ffmpeg <-> numpy) ----------

def _parse_ppm(data: bytes):
    """Split ffmpeg's PPM stream (one P6 image per frame) into RGB arrays."""
    import numpy as np

    frames, pos = [], 0
    while pos < len(data):
        fields, cursor = [], pos
        while len(fields) < 4:  # magic, width, height, maxval
            while cursor < len(data) and data[cursor:cursor + 1].isspace():
                cursor += 1
            start = cursor
            while cursor < len(data) and not data[cursor:cursor + 1].isspace():
                cursor += 1
            if start == cursor:
                return frames
            fields.append(data[start:cursor])
        if fields[0] != b"P6":
            break
        w, h = int(fields[1]), int(fields[2])
        cursor += 1  # the single whitespace byte after maxval
        end = cursor + w * h * 3
        if end > len(data):
            break
        frames.append(np.frombuffer(data[cursor:end], dtype=np.uint8).reshape(h, w, 3))
        pos = end
    return frames


async def clip_frames(video: bytes) -> list:
    """Frames sampled from a clip at FACES_FPS, at most FACES_MAX_FRAMES, at most 1280 px wide."""
    with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
        tmp.write(video)
        tmp.flush()
        out = await _ffmpeg("-i", tmp.name, "-vf", f"fps={config.FACES_FPS},scale='min(1280,iw)':-2",
                            "-frames:v", str(config.FACES_MAX_FRAMES), "-f", "image2pipe", "-vcodec", "ppm", "-")
    return _parse_ppm(out)


async def photo_frame(image: bytes) -> list:
    """Decode an uploaded photo (JPEG, PNG, ...), at most 1600 px on its long side."""
    with tempfile.NamedTemporaryFile() as tmp:
        tmp.write(image)
        tmp.flush()
        out = await _ffmpeg("-i", tmp.name, "-frames:v", "1", "-vf",
                            "scale=w='min(1600,iw)':h='min(1600,ih)':force_original_aspect_ratio=decrease",
                            "-f", "image2pipe", "-vcodec", "ppm", "-")
    return _parse_ppm(out)[:1]


async def to_jpeg(rgb) -> bytes:
    h, w = rgb.shape[:2]
    header = f"P6\n{w} {h}\n255\n".encode()
    return await _ffmpeg("-f", "ppm_pipe", "-i", "-", "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "-",
                         data=header + rgb.tobytes())


# ---------- numpy geometry ----------

def _warp(img, m, out_w: int, out_h: int):
    """Bilinear affine warp; `m` (2x3) maps source pixels to output pixels. Outside is black."""
    import numpy as np

    a = np.vstack([m, [0, 0, 1]])
    inv = np.linalg.inv(a)[:2]
    ys, xs = np.mgrid[0:out_h, 0:out_w].astype(np.float32)
    sx = inv[0, 0] * xs + inv[0, 1] * ys + inv[0, 2]
    sy = inv[1, 0] * xs + inv[1, 1] * ys + inv[1, 2]
    h, w = img.shape[:2]
    inside = (sx >= 0) & (sy >= 0) & (sx <= w - 1) & (sy <= h - 1)
    sx = np.clip(sx, 0, w - 1)
    sy = np.clip(sy, 0, h - 1)
    x0 = np.floor(sx).astype(np.int32)
    y0 = np.floor(sy).astype(np.int32)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = (sx - x0)[..., None]
    fy = (sy - y0)[..., None]
    src = img.astype(np.float32)
    top = src[y0, x0] * (1 - fx) + src[y0, x1] * fx
    bottom = src[y1, x0] * (1 - fx) + src[y1, x1] * fx
    out = top * (1 - fy) + bottom * fy
    out[~inside] = 0
    return out.astype(np.uint8)


def _shrink(img, scale: float):
    """Fast downscale for the detector: average k x k blocks when the factor is a whole number
    (1280 -> 640, 1920 -> 640), otherwise nearest pixel. Returns the image and the scale used."""
    import numpy as np

    h, w = img.shape[:2]
    k = round(1 / scale)
    if k >= 1 and abs(1 / scale - k) < 1e-6:
        if k == 1:
            return img, 1.0
        hh, ww = h // k * k, w // k * k
        small = img[:hh, :ww].reshape(hh // k, k, ww // k, k, 3).mean(axis=(1, 3)).astype(np.uint8)
        return small, 1.0 / k
    nh, nw = max(1, int(h * scale)), max(1, int(w * scale))
    ys = np.minimum((np.arange(nh) / scale).astype(np.int32), h - 1)
    xs = np.minimum((np.arange(nw) / scale).astype(np.int32), w - 1)
    return img[ys[:, None], xs[None, :]], scale


def _similarity(src, dst):
    """Least-squares rotation + uniform scale + translation taking src points onto dst (Umeyama)."""
    import numpy as np

    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    mu_s, mu_d = src.mean(0), dst.mean(0)
    s, d = src - mu_s, dst - mu_d
    cov = d.T @ s / len(src)
    u, sig, vt = np.linalg.svd(cov)
    sign = np.ones(2)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        sign[-1] = -1
    r = u @ np.diag(sign) @ vt
    scale = (sig * sign).sum() / s.var(0).sum()
    t = mu_d - scale * r @ mu_s
    return np.hstack([scale * r, t[:, None]]).astype(np.float32)


def _nms(boxes, scores, threshold: float):
    import numpy as np

    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
        yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
        yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        area = lambda b: (b[..., 2] - b[..., 0]) * (b[..., 3] - b[..., 1])
        iou = inter / (area(boxes[i]) + area(boxes[order[1:]]) - inter + 1e-9)
        order = order[1:][iou <= threshold]
    return keep


def _is_gray(img) -> bool:
    import numpy as np

    small = img[::8, ::8].astype(np.int16)
    spread = np.abs(small[..., 0] - small[..., 1]) + np.abs(small[..., 1] - small[..., 2])
    return float(spread.mean()) < 4.0


def _frontal(kps) -> bool:
    """Nose roughly between the eyes: not a profile."""
    left, right = sorted([kps[0][0], kps[1][0]])
    if right - left < 1:
        return False
    position = (kps[2][0] - left) / (right - left)
    return 0.2 <= position <= 0.8


# ---------- the models ----------

class FaceEngine:
    """One copy of the models for all accounts; loads (and downloads) on first use."""

    def __init__(self) -> None:
        self._det = None
        self._rec = None
        self._centers: Dict[Tuple[int, int], Any] = {}
        self._load_lock = threading.Lock()
        self._run_lock = asyncio.Lock()

    @property
    def model_dir(self) -> Path:
        return config.MODELS_DIR / MODEL_NAME

    def _download(self) -> None:
        folder = self.model_dir
        folder.mkdir(parents=True, exist_ok=True)
        log.info("Downloading face models from %s (about 130 MB, once)", MODEL_URL)
        with tempfile.NamedTemporaryFile(suffix=".zip", dir=folder) as tmp:
            with urllib.request.urlopen(MODEL_URL, timeout=60) as resp:
                while chunk := resp.read(1 << 20):
                    tmp.write(chunk)
            tmp.flush()
            with zipfile.ZipFile(tmp.name) as pack:
                for name, digest in MODEL_FILES.items():
                    data = pack.read(name)
                    if hashlib.sha256(data).hexdigest() != digest:
                        raise RuntimeError(f"Face model {name} failed its checksum")
                    part = folder / (name + ".part")
                    part.write_bytes(data)
                    part.replace(folder / name)

    def _load(self) -> None:
        with self._load_lock:
            if self._rec is not None:
                return
            import onnxruntime as ort

            if not all((self.model_dir / name).exists() for name in MODEL_FILES):
                self._download()
            options = ort.SessionOptions()
            options.intra_op_num_threads = config.FACES_THREADS
            options.inter_op_num_threads = 1
            options.enable_cpu_mem_arena = False  # keeps memory flat on small servers
            providers = ["CPUExecutionProvider"]
            self._det = ort.InferenceSession(str(self.model_dir / "det_500m.onnx"), options, providers=providers)
            self._rec = ort.InferenceSession(str(self.model_dir / "w600k_mbf.onnx"), options, providers=providers)
            log.info("Face models loaded (%s)", MODEL_NAME)

    async def faces_in_clip(self, video: bytes) -> List[Face]:
        frames = await clip_frames(video)
        return await self._run(frames)

    async def faces_in_photo(self, image: bytes) -> List[Face]:
        frames = await photo_frame(image)
        return await self._run(frames)

    async def _run(self, frames) -> List[Face]:
        if not frames:
            return []
        async with self._run_lock:  # one at a time: predictable CPU on a small server
            return await asyncio.to_thread(self._faces, frames)

    # ----- blocking work (runs in a thread) -----

    def _faces(self, frames) -> List[Face]:
        self._load()
        faces = []
        for index, img in enumerate(frames):
            gray = _is_gray(img)
            for box, score, kps in self._detect(img):
                face = Face(frame=index, box=[round(float(v), 1) for v in box], score=round(float(score), 3),
                            width=float(box[2] - box[0]), gray=gray, frontal=_frontal(kps))
                if face.usable:  # skip the work for faces we'd never compare
                    face.crop = _warp(img, _similarity(kps, _ARCFACE_DST), 112, 112)
                    face.embedding = self._embed(face.crop)
                faces.append(face)
        return faces

    def _detect(self, img, threshold: float = 0.5):
        import numpy as np

        h, w = img.shape[:2]
        small, scale = _shrink(img, min(1.0, DET_SIZE / max(h, w)))
        canvas = np.zeros((DET_SIZE, DET_SIZE, 3), dtype=np.uint8)  # letterbox: image top-left
        canvas[:small.shape[0], :small.shape[1]] = small
        blob = ((canvas.astype(np.float32) - 127.5) / 128.0).transpose(2, 0, 1)[None]
        outs = self._det.run(None, {self._det.get_inputs()[0].name: blob})
        boxes, scores, points = [], [], []
        for i, stride in enumerate((8, 16, 32)):
            cells = DET_SIZE // stride
            if (cells, stride) not in self._centers:
                grid = np.stack(np.mgrid[:cells, :cells][::-1], axis=-1).astype(np.float32)
                grid = (grid * stride).reshape(-1, 2)
                self._centers[(cells, stride)] = np.repeat(grid, 2, axis=0)  # 2 anchors per cell
            centers = self._centers[(cells, stride)]
            s = outs[i].reshape(-1)
            keep = np.where(s >= threshold)[0]
            if not keep.size:
                continue
            c = centers[keep]
            d = outs[i + 3].reshape(-1, 4)[keep] * stride
            k = outs[i + 6].reshape(-1, 10)[keep] * stride
            boxes.append(np.stack([c[:, 0] - d[:, 0], c[:, 1] - d[:, 1], c[:, 0] + d[:, 2], c[:, 1] + d[:, 3]], 1))
            points.append(np.stack([c[:, :1] + k[:, 0::2], c[:, 1:] + k[:, 1::2]], -1))
            scores.append(s[keep])
        if not boxes:
            return []
        boxes = np.concatenate(boxes) / scale
        points = np.concatenate(points) / scale
        scores = np.concatenate(scores)
        return [(boxes[i], scores[i], points[i]) for i in _nms(boxes, scores, 0.4)]

    def _embed(self, crop):
        import numpy as np

        blob = ((crop.astype(np.float32) - 127.5) / 127.5).transpose(2, 0, 1)[None]
        vec = self._rec.run(None, {self._rec.get_inputs()[0].name: blob})[0][0]
        return (vec / np.linalg.norm(vec)).astype(np.float32)


_engine: Optional[FaceEngine] = None


def shared_face_engine() -> FaceEngine:
    global _engine
    if _engine is None:
        _engine = FaceEngine()
    return _engine


def available_for(username: Optional[str]) -> bool:
    """The server allows face recognition for this Blink account (FACES_ENABLED / FACES_ACCOUNTS)."""
    if not config.FACES_ENABLED:
        return False
    return not config.FACES_ACCOUNTS or (username or "").strip().lower() in config.FACES_ACCOUNTS


# ---------- matching ----------

def match_clip(faces: List[Face], people: Dict[str, Any]) -> List[Tuple[str, float]]:
    """Enrolled people seen in a clip: [(person_id, best similarity)], most confident first.

    A person is named only if a usable face matches them (above FACES_THRESHOLD and clearly better
    than the next person) in at least two frames, or once with a very high score. Anything less
    stays "a person": a wrong name is worse than none.
    """
    import numpy as np

    if not people:
        return []
    ids = list(people)
    hits: Dict[str, List[float]] = {}
    for face in faces:
        if not face.usable or face.embedding is None:
            continue
        sims = np.array([float((people[pid] @ face.embedding).max()) for pid in ids])
        order = sims.argsort()[::-1]
        best = sims[order[0]]
        runner_up = sims[order[1]] if len(order) > 1 else -1.0
        if best >= config.FACES_THRESHOLD and best - runner_up >= config.FACES_MARGIN:
            hits.setdefault(ids[order[0]], []).append(best)
    named = [(pid, max(scores)) for pid, scores in hits.items()
             if len(scores) >= 2 or max(scores) >= config.FACES_THRESHOLD + 0.1]
    return sorted(((pid, float(score)) for pid, score in named), key=lambda m: m[1], reverse=True)


def group_faces(faces: List[Face], threshold: float = 0.35) -> List[List[Face]]:
    """Group a clip's usable faces into one group per person (greedy, by similarity)."""
    groups: List[List[Face]] = []
    for face in sorted((f for f in faces if f.usable and f.embedding is not None),
                       key=lambda f: f.score * f.width, reverse=True):
        for group in groups:
            if float(group[0].embedding @ face.embedding) >= threshold:
                group.append(face)
                break
        else:
            groups.append([face])
    return groups


# ---------- per-account storage ----------

def _vec_to_text(vec) -> str:
    return base64.b64encode(vec.astype("<f4").tobytes()).decode()


def _text_to_vec(text: str):
    import numpy as np

    return np.frombuffer(base64.b64decode(text), dtype="<f4")


@dataclass
class Candidate:
    """A face found in a clip or photo, offered for enrollment. Lives in memory for a few minutes."""
    embeddings: List[Any]
    thumb: bytes
    created: float = field(default_factory=time.time)


class FaceAccount:
    """One Blink account's enrolled people (encrypted at rest) and pending candidates."""

    CANDIDATE_TTL = 15 * 60

    def __init__(self, folder: Path) -> None:
        self.file = folder / "faces.enc"
        self._candidates: Dict[str, Candidate] = {}

    # ----- storage -----

    def _load(self) -> Dict[str, Any]:
        if not self.file.exists():
            return {"version": 1, "model": MODEL_NAME, "people": []}
        return json.loads(crypto.read_secret(self.file))

    def _save(self, data: Dict[str, Any]) -> None:
        crypto.write_secret(self.file, json.dumps(data))

    def people(self) -> List[Dict[str, Any]]:
        return self._load()["people"]

    def templates(self) -> Dict[str, Any]:
        """{person_id: float32[n, 512]} for matching."""
        import numpy as np

        data = self._load()
        if data.get("model") != MODEL_NAME:
            return {}  # templates from another model aren't comparable
        return {p["id"]: np.stack([_text_to_vec(t) for t in p["embeddings"]])
                for p in data["people"] if p["embeddings"]}

    def names(self, ids: List[str]) -> List[str]:
        by_id = {p["id"]: p["name"] for p in self.people()}
        return [by_id[i] for i in ids if i in by_id]

    def mark_seen(self, ids: List[str]) -> None:
        data = self._load()
        now = time.time()
        for person in data["people"]:
            if person["id"] in ids:
                person["last_seen"] = now
        self._save(data)

    def add(self, name: str, token: str) -> Dict[str, Any]:
        """Enroll a candidate under `name`: a new person, or more samples for an existing one."""
        name = _clean_name(name)
        cand = self._take(token)
        data = self._load()
        person = next((p for p in data["people"] if p["name"].lower() == name.lower()), None)
        if person is None:
            if len(data["people"]) >= MAX_PEOPLE:
                raise ValueError(f"You can add up to {MAX_PEOPLE} people.")
            person = {"id": "p_" + secrets.token_hex(4), "name": name, "created_at": time.time(),
                      "embeddings": [], "thumb": base64.b64encode(cand.thumb).decode()}
            data["people"].append(person)
        person["embeddings"] = (person["embeddings"] + [_vec_to_text(v) for v in cand.embeddings])[-MAX_SAMPLES:]
        data["model"] = MODEL_NAME
        self._save(data)
        return person

    def remove(self, person_id: str) -> None:
        data = self._load()
        data["people"] = [p for p in data["people"] if p["id"] != person_id]
        if data["people"]:
            self._save(data)
        else:
            self.forget_all()

    def forget_all(self) -> None:
        self.file.unlink(missing_ok=True)
        self._candidates.clear()

    # ----- candidates -----

    async def offer(self, groups: List[List[Face]]) -> List[Dict[str, Any]]:
        """Turn groups of faces into short-lived candidates the owner can name."""
        now = time.time()
        self._candidates = {t: c for t, c in self._candidates.items() if now - c.created < self.CANDIDATE_TTL}
        templates = self.templates()
        names = {p["id"]: p["name"] for p in self.people()}
        offered = []
        for group in groups[:8]:
            best = group[0]
            thumb = await to_jpeg(best.crop)
            token = secrets.token_urlsafe(12)
            self._candidates[token] = Candidate(embeddings=[f.embedding for f in group[:5]], thumb=thumb)
            matches = match_clip(group, templates)
            offered.append({"token": token, "image": "data:image/jpeg;base64," + base64.b64encode(thumb).decode(),
                            "match": names.get(matches[0][0]) if matches else None, "frames": len(group)})
        while len(self._candidates) > 40:
            self._candidates.pop(next(iter(self._candidates)))
        return offered

    def _take(self, token: str) -> Candidate:
        cand = self._candidates.pop(token, None)
        if cand is None or time.time() - cand.created > self.CANDIDATE_TTL:
            raise ValueError("That face expired. Look for faces again.")
        return cand


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "")).strip()
    if not 1 <= len(name) <= 40:
        raise ValueError("Enter a name (up to 40 characters).")
    return name


def person_view(person: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": person["id"], "name": person["name"], "samples": len(person["embeddings"]),
            "image": "data:image/jpeg;base64," + person["thumb"] if person.get("thumb") else None,
            "last_seen": person.get("last_seen")}
