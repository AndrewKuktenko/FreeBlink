"""Local object detection on Blink clips with a YOLO model (runs on this machine)."""

import asyncio
import logging
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config

log = logging.getLogger(__name__)


@dataclass
class Detection:
    label: str
    confidence: float


@dataclass
class ClipAnalysis:
    # Best confidence seen for each label across all sampled frames.
    labels: Dict[str, float] = field(default_factory=dict)
    # Annotated JPEG of the frame with the most confident wanted object (or a plain frame).
    snapshot: Optional[bytes] = None
    frames_analyzed: int = 0
    # Distinct wanted objects: (label, confidence, [x1, y1, x2, y2] normalized 0-1).
    boxes: List[Tuple[str, float, List[float]]] = field(default_factory=list)

    def wanted(self, classes: List[str], min_conf: float) -> Dict[str, float]:
        return {k: v for k, v in self.labels.items() if k in classes and v >= min_conf}


class Detector:
    def __init__(self) -> None:
        self._model = None
        self._load_lock = threading.Lock()
        self._run_lock = asyncio.Lock()

    def _get_model(self):
        with self._load_lock:
            if self._model is None:
                from ultralytics import YOLO  # heavy import; only when detection is used

                config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
                path = config.MODELS_DIR / config.DETECT_MODEL
                log.info("Loading detection model %s (downloads on first use)", path)
                self._model = YOLO(str(path))
            return self._model

    @property
    def class_names(self) -> List[str]:
        return sorted(self._get_model().names.values())

    async def analyze_clip(self, video: bytes) -> ClipAnalysis:
        async with self._run_lock:  # one clip at a time; keeps CPU/GPU use predictable
            return await asyncio.to_thread(self._analyze_clip, video)

    async def analyze_image(self, image: bytes) -> ClipAnalysis:
        async with self._run_lock:
            return await asyncio.to_thread(self._analyze_image, image)

    # ---------- blocking work (runs in a thread) ----------

    def _analyze_clip(self, video: bytes) -> ClipAnalysis:
        import cv2

        with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
            tmp.write(video)
            tmp.flush()
            cap = cv2.VideoCapture(tmp.name)
            fps = cap.get(cv2.CAP_PROP_FPS) or 15
            step = max(1, int(round(fps / config.DETECT_FPS)))
            frames = []
            index = 0
            while len(frames) < config.DETECT_MAX_FRAMES:
                ok = cap.grab()
                if not ok:
                    break
                if index % step == 0:
                    ok, frame = cap.retrieve()
                    if ok:
                        frames.append(frame)
                index += 1
            cap.release()
        return self._run(frames)

    def _analyze_image(self, image: bytes) -> ClipAnalysis:
        import cv2
        import numpy as np

        frame = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
        return self._run([frame] if frame is not None else [])

    def _run(self, frames) -> ClipAnalysis:
        import cv2

        result = ClipAnalysis(frames_analyzed=len(frames))
        if not frames:
            return result
        model = self._get_model()
        wanted = set(config.DETECT_CLASSES)
        best_score = -1.0
        best_plot = None
        fallback = frames[len(frames) // 2]

        for start in range(0, len(frames), 8):
            batch = frames[start:start + 8]
            for res in model.predict(batch, conf=0.25, verbose=False):
                frame_best = -1.0
                for cls_id, conf, box in zip(res.boxes.cls.tolist(), res.boxes.conf.tolist(),
                                             res.boxes.xyxyn.tolist()):
                    label = model.names[int(cls_id)]
                    result.labels[label] = max(result.labels.get(label, 0.0), round(conf, 3))
                    if label in wanted:
                        frame_best = max(frame_best, conf)
                        if conf >= config.DETECT_MIN_CONFIDENCE:
                            _add_box(result.boxes, label, round(conf, 3), [round(v, 4) for v in box])
                if frame_best > best_score:
                    best_score = frame_best
                    best_plot = res.plot()  # frame with boxes and labels drawn

        image = best_plot if best_plot is not None and best_score >= 0 else fallback
        ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        result.snapshot = jpeg.tobytes() if ok else None
        return result


def iou(a: List[float], b: List[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _add_box(boxes, label: str, conf: float, box: List[float]) -> None:
    """Merge the same object seen in several frames into one entry."""
    for i, (other_label, other_conf, other_box) in enumerate(boxes):
        if other_label == label and iou(box, other_box) >= 0.5:
            if conf > other_conf:
                boxes[i] = (label, conf, box)
            return
    if len(boxes) < 30:
        boxes.append((label, conf, box))
