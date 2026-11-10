import base64
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from uuid import UUID

from al1s.meme_workshop.images import MAX_BYTES, normalized_png
from al1s.meme_workshop.media_fetch import permitted_media_url, read_media
from al1s.bot_ai.managed.vision_images import prepare_images


def audio_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
             "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, check=True, timeout=10,
        )
        return float(result.stdout.decode("ascii").strip())
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as error:
        raise ValueError("ai_audio_probe_failed") from error


def normalize_audio(source: Path, target: Path) -> None:
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-threads", "1",
                        "-protocol_whitelist", "file,pipe", "-i", str(source),
                        "-ac", "1", "-ar", "16000", "-f", "wav", str(target)],
                       capture_output=True, check=True, timeout=10)
        if not target.is_file() or target.stat().st_size > MAX_BYTES:
            raise ValueError("ai_audio_size_invalid")
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("ai_audio_conversion_failed") from error


class MediaManager:
    def __init__(self, root: Path, *, fetch=read_media, probe=audio_duration,
                 llbot_temp=Path("/app/llbot/data/temp"), convert=normalize_audio) -> None:
        self.root = Path(root).resolve()
        self.llbot_temp = Path(llbot_temp).resolve()
        self.fetch, self.probe, self.convert = fetch, probe, convert

    def _llbot_record(self, client, reference: str) -> bytes:
        if client is None or not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", reference) or reference in {".", ".."}:
            raise ValueError("ai_audio_reference_invalid")
        result = client.post("get_record", {"file": reference, "out_format": "wav"})
        value = result.get("file") if isinstance(result, dict) else None
        if not isinstance(value, str):
            raise ValueError("ai_audio_result_invalid")
        path = Path(value)
        if (path.is_symlink() or path.resolve().parent != self.llbot_temp
                or not re.fullmatch(r"[0-9a-fA-F-]{36}(?:\.silk)?\.wav", path.name)):
            raise ValueError("ai_audio_result_invalid")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ValueError("ai_media_size_invalid")
            return file.read(MAX_BYTES + 1)

    def directory(self, request_id: UUID) -> Path:
        candidate = self.root / str(request_id)
        if candidate.is_symlink():
            raise ValueError("ai_media_directory_invalid")
        target = candidate.resolve()
        if target.parent != self.root:
            raise ValueError("ai_media_directory_invalid")
        return target

    def prepare(self, request_id: UUID, parts: list[dict], client=None, *, observations=()) -> list[dict]:
        if (len(parts) > 6 or sum(p.get("type") == "image" for p in parts) > 4
                or sum(p.get("type") == "record" for p in parts) > 1):
            raise ValueError("ai_media_count_invalid")
        directory = self.directory(request_id)
        directory.mkdir(parents=True, exist_ok=False, mode=0o770)
        os.chmod(directory, 0o2770)
        output, image_bytes = [], 0
        try:
            current_refs = {p.get("reference") for p in parts}
            remaining = min(6 - len(parts), 4 - sum(p.get("type") == "image" for p in parts))
            observed = list(dict.fromkeys(ref for ref in observations if ref not in current_refs))[:remaining]
            parts = [*parts, *({"type": "image", "reference": ref, "observation_only": True} for ref in observed)]
            for index, part in enumerate(parts):
                kind = part.get("type")
                if kind == "text" and isinstance(part.get("text"), str):
                    output.append({"type": "text", "text": part["text"]})
                    continue
                reference = part.get("reference")
                if kind not in {"image", "record"} or not isinstance(reference, str):
                    raise ValueError("ai_media_invalid")
                image_meta = {}
                try:
                    if permitted_media_url(reference):
                        body = self.fetch(reference)
                    elif kind == "record":
                        body = self._llbot_record(client, reference)
                    else:
                        raise ValueError("ai_media_source_invalid")
                    if not isinstance(body, bytes) or not body or len(body) > MAX_BYTES:
                        raise ValueError("ai_media_size_invalid")
                    if kind == "image":
                        image_bytes += len(body)
                        if image_bytes > 16 * 1024 * 1024:
                            raise ValueError("ai_image_total_invalid")
                        encoded, hint = prepare_images(["base64://" + base64.b64encode(body).decode()], ())
                        original = directory / f"original-{index}.image"
                        original.write_bytes(body)
                        os.chmod(original, 0o640)
                        image_meta = {"original_ref": str(original), "caption_hint": hint,
                                      "observation_only": part.get("observation_only", False)}
                        body, _, _ = normalized_png(base64.b64decode(encoded.split(",", 1)[1]))
                except (OSError, ValueError) as error:
                    if part.get("observation_only"):
                        continue
                    raise ValueError("ai_media_invalid") from error
                target = directory / f"input-{index}.{'png' if kind == 'image' else 'source'}"
                descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
                with os.fdopen(descriptor, "wb") as file:
                    file.write(body)
                if kind == "record":
                    duration = self.probe(target)
                    if not 0 < duration <= 120:
                        raise ValueError("ai_audio_duration_invalid")
                    normalized = directory / f"input-{index}.wav"
                    self.convert(target, normalized)
                    target.unlink()
                    target = normalized
                output.append({"type": kind, "media_ref": str(target), **image_meta})
            return output
        except Exception:
            self.cleanup(request_id)
            raise

    def cleanup(self, request_id: UUID) -> None:
        target = self.directory(request_id)
        if target.is_symlink():
            raise ValueError("ai_media_directory_invalid")
        if target.exists():
            shutil.rmtree(target)
