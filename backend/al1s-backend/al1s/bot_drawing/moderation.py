import asyncio
import csv
import hashlib
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from al1s.meme_workshop.images import MAX_BYTES

MODEL_REVISION = "790b0e92cefd2a0221451604e7831fe643ab7c4f"
MODEL_SHA256 = "35f23693620b668f4d53fd3c62bf65e40af739bc52c7eb0fbc49258b58d065b6"
LABELS_SHA256 = "298633d94d0031d2081c0893f29c82eab7f0df00b08483ba8f29d1e979441217"
POLICY_VERSION = "wd14-green-v3"
PREVIEW_ALLOW_POLICIES = frozenset({"wd14-green-v1", "wd14-green-v2", POLICY_VERSION})
_RATINGS = ("general", "sensitive", "questionable", "explicit")


def decide(scores: object) -> str:
    if not isinstance(scores, dict) or set(scores) != set(_RATINGS):
        return "unknown"
    values = list(scores.values())
    if any(type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 1 for value in values):
        return "unknown"
    safe = max(scores["general"], scores["sensitive"])
    if max(scores["questionable"], scores["explicit"]) >= safe:
        return "block"
    return "allow"


@dataclass(frozen=True)
class AuditDecision:
    status: str
    image_sha256: str
    model_revision: str = MODEL_REVISION
    policy_version: str = POLICY_VERSION
    scores: dict | None = None


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class WDAuditor:
    def __init__(self, model_directory: Path, *, timeout: float = 15):
        self.directory = Path(model_directory)
        self.timeout = timeout
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drawing-audit")
        self._pending = None
        self._session = None
        self._rating_indices = None
        self._closed = False

    def _load(self):
        if self._session is not None:
            return
        model, labels = self.directory / "wd-vit-tagger-v3.onnx", self.directory / "wd-vit-tagger-v3.csv"
        if (model.is_symlink() or labels.is_symlink() or _digest(model) != MODEL_SHA256
                or _digest(labels) != LABELS_SHA256):
            raise ValueError("drawing_audit_model_invalid")
        with labels.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        ratings = {row["name"]: index for index, row in enumerate(rows) if row.get("category") == "9"}
        if set(ratings) != set(_RATINGS):
            raise ValueError("drawing_audit_model_invalid")
        import onnxruntime as ort
        session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
        inputs, outputs = session.get_inputs(), session.get_outputs()
        if (len(inputs) != 1 or len(outputs) != 1 or len(inputs[0].shape) != 4
                or inputs[0].shape[1] != inputs[0].shape[2]
                or not isinstance(inputs[0].shape[1], int) or inputs[0].shape[3] != 3
                or outputs[0].shape[-1] != len(rows)):
            raise ValueError("drawing_audit_model_invalid")
        self._rating_indices, self._session = ratings, session

    def _infer(self, body: bytes) -> dict:
        if not isinstance(body, bytes) or not body or len(body) > MAX_BYTES:
            raise ValueError("drawing_image_invalid")
        with Image.open(BytesIO(body), formats=["PNG", "JPEG", "WEBP", "GIF"]) as original:
            width, height = original.size
            frames = getattr(original, "n_frames", 1)
            if ((original.format == "GIF" and not 2 <= frames <= 16)
                    or (original.format != "GIF" and frames != 1) or min(width, height) < 1
                    or max(width, height) > 4096 or width * height > 8_000_000):
                raise ValueError("drawing_image_invalid")
            if original.format == "GIF" and width * height * frames > 16_000_000:
                raise ValueError("drawing_image_invalid")
            self._load()
            model_input = self._session.get_inputs()[0]
            edge = model_input.shape[1]
            selected, selected_rank = None, (-1, float("-inf"))
            for index in range(frames):
                original.seek(index)
                image = ImageOps.exif_transpose(original).convert("RGBA")
                white = Image.new("RGBA", image.size, (255, 255, 255, 255))
                white.alpha_composite(image)
                image = white.convert("RGB")
                ratio = edge / max(image.size)
                size = tuple(max(1, int(value * ratio)) for value in image.size)
                image = image.resize(size, Image.Resampling.LANCZOS)
                square = Image.new("RGB", (edge, edge), (255, 255, 255))
                square.paste(image, ((edge - size[0]) // 2, (edge - size[1]) // 2))
                pixels = np.asarray(square, dtype=np.float32)[:, :, ::-1][None, ...]
                output = self._session.run(None, {model_input.name: pixels})[0]
                if len(output.shape) != 2 or output.shape[0] != 1:
                    raise ValueError("drawing_audit_output_invalid")
                scores = {name: float(output[0, position]) for name, position in self._rating_indices.items()}
                status = decide(scores)
                risk = (max(scores["questionable"], scores["explicit"]) - max(scores["general"], scores["sensitive"])
                        if status != "unknown" else 0)
                rank = ({"allow": 0, "block": 1, "unknown": 2}[status], risk)
                if rank > selected_rank:
                    selected, selected_rank = scores, rank
            return selected

    async def audit(self, body: bytes) -> AuditDecision:
        checksum = hashlib.sha256(body if isinstance(body, bytes) else b"").hexdigest()
        if self._closed or (self._pending is not None and not self._pending.done()):
            return AuditDecision("unknown", checksum)
        loop = asyncio.get_running_loop()
        self._pending = loop.run_in_executor(self._pool, self._infer, body)
        self._pending.add_done_callback(lambda result: None if result.cancelled() else result.exception())
        try:
            timeout = self.timeout * 4 if isinstance(body, bytes) and body[:6] in {b"GIF87a", b"GIF89a"} else self.timeout
            scores = await asyncio.wait_for(asyncio.shield(self._pending), timeout)
            status = decide(scores)
            return AuditDecision(status, checksum, scores=scores if status != "unknown" else None)
        except Exception:
            return AuditDecision("unknown", checksum)

    def close(self):
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
