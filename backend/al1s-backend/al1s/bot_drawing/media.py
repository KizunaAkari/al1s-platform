import base64
import binascii
import hashlib
import os
import re
import stat
import threading
import time
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from PIL import Image

from al1s.meme_workshop.images import MAX_BYTES, MAX_PIXELS

MAX_IMAGE_DATA = 12 * 1024 * 1024
_IMAGE_DATA = re.compile(r"^data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/]+={0,2})$")
_FORMATS = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}
_REFERENCE = re.compile(r"^al1s-drawing/([0-9a-f-]{36})/(input\.(?:png|jpeg|webp)|output\.(?:png|gif))$")
_OWNED_FILE = re.compile(r"^(?:input\.(?:png|jpeg|webp)|output\.(?:png|gif)|\.(?:input\.(?:png|jpeg|webp)|output\.(?:png|gif))\.[0-9a-f]{32}\.tmp)$")
_MARKER = '.al1s-owner'


def validate_image_data(value: str) -> None:
    if not isinstance(value, str) or len(value) > MAX_IMAGE_DATA:
        raise ValueError("drawing_image_invalid")
    match = _IMAGE_DATA.fullmatch(value)
    if not match:
        raise ValueError("drawing_image_invalid")
    try:
        body = base64.b64decode(match.group(2), validate=True)
        if not body or len(body) > MAX_BYTES:
            raise ValueError("drawing_image_invalid")
        with Image.open(BytesIO(body), formats=["PNG", "JPEG", "WEBP"]) as image:
            width, height = image.size
            if (image.format != _FORMATS[match.group(1)] or getattr(image, "n_frames", 1) != 1
                    or min(width, height) < 1 or max(width, height) > 4096 or width * height > MAX_PIXELS):
                raise ValueError("drawing_image_invalid")
            image.load()
    except (binascii.Error, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise ValueError("drawing_image_invalid") from None


def _image_format(body: bytes, *, allow_gif: bool = False) -> str:
    if not isinstance(body, bytes) or not body or len(body) > MAX_BYTES:
        raise ValueError("drawing_image_invalid")
    try:
        formats = ["PNG", "JPEG", "WEBP"] + (["GIF"] if allow_gif else [])
        with Image.open(BytesIO(body), formats=formats) as image:
            width, height = image.size
            frames = getattr(image, "n_frames", 1)
            if ((image.format == "GIF" and not 2 <= frames <= 16)
                    or (image.format != "GIF" and frames != 1) or min(width, height) < 1
                    or max(width, height) > 4096 or width * height > MAX_PIXELS):
                raise ValueError("drawing_image_invalid")
            if image.format == "GIF" and width * height * frames > 16_000_000:
                raise ValueError("drawing_image_invalid")
            for index in range(frames):
                image.seek(index)
                image.load()
            return {"PNG": "png", "JPEG": "jpeg", "WEBP": "webp", "GIF": "gif"}[image.format]
    except (OSError, EOFError, SyntaxError, KeyError, Image.DecompressionBombError):
        raise ValueError("drawing_image_invalid") from None


class DrawingMediaStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("drawing_media_path_invalid")
        self._staging_lock = threading.RLock()
        self._scan = None

    def staging(self):
        return self._staging_lock

    @contextmanager
    def _directory(self, task_id: UUID, *, create: bool):
        if not isinstance(task_id, UUID):
            raise ValueError("drawing_media_reference_invalid")
        path = self.root
        handles = []
        use_dir_fd = os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd
        try:
            if use_dir_fd:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                handles.append(os.open(self.root, flags))
            for name in ("al1s-drawing", str(task_id)):
                parent = handles[-1] if handles else None
                path = path / name
                created = False
                if create:
                    try:
                        if parent is None:
                            path.mkdir(mode=0o700)
                        else:
                            os.mkdir(name, mode=0o700, dir_fd=parent)
                        created = True
                    except FileExistsError:
                        pass
                info = os.lstat(name, dir_fd=parent) if parent is not None else path.lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise ValueError("drawing_media_path_invalid")
                if parent is not None:
                    handles.append(os.open(name, flags, dir_fd=parent))
                if name == str(task_id):
                    directory_fd = handles[-1] if handles else None
                    marker = ('al1s-drawing-v1:' + str(task_id)).encode('ascii')
                    if created:
                        self._write_atomic(path, directory_fd, _MARKER, marker)
                    if self._read(path, directory_fd, _MARKER) != marker:
                        raise ValueError("drawing_media_path_invalid")
            yield path, handles[-1] if handles else None
        except FileNotFoundError:
            raise
        except (OSError, RuntimeError) as error:
            raise ValueError("drawing_media_path_invalid") from error
        finally:
            for handle in reversed(handles):
                os.close(handle)

    @staticmethod
    def _read(directory: Path, directory_fd: int | None, name: str) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name if directory_fd is not None else directory / name, flags, dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("drawing_media_path_invalid")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                body = stream.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("drawing_media_changed")
            return body
        finally:
            os.close(fd)

    def put(self, task_id: UUID, kind: str, body: bytes) -> tuple[str, str]:
        if kind not in {"input", "output"}:
            raise ValueError("drawing_media_reference_invalid")
        fmt = _image_format(body, allow_gif=kind == "output")
        if kind == "output" and fmt not in {"png", "gif"}:
            raise ValueError("drawing_image_invalid")
        name, digest = f"{kind}.{fmt}", hashlib.sha256(body).hexdigest()
        with self._staging_lock, self._directory(task_id, create=True) as (directory, directory_fd):
            if kind in {"input", "output"}:
                extensions = {"png", "jpeg", "webp"} if kind == "input" else {"png", "gif"}
                for extension in extensions - {fmt}:
                    try:
                        self._read(directory, directory_fd, f"{kind}.{extension}")
                    except FileNotFoundError:
                        continue
                    raise ValueError("drawing_media_conflict")
            try:
                existing = self._read(directory, directory_fd, name)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if hashlib.sha256(existing).hexdigest() != digest:
                    raise ValueError("drawing_media_conflict")
            else:
                self._write_atomic(directory, directory_fd, name, body)
        return f"al1s-drawing/{task_id}/{name}", digest

    @staticmethod
    def _write_atomic(directory: Path, directory_fd: int | None, name: str, body: bytes) -> None:
        temporary = f".{name}.{uuid4().hex}.tmp"
        path = temporary if directory_fd is not None else directory / temporary
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            if directory_fd is not None:
                os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
                os.fsync(directory_fd)
            else:
                os.link(directory / temporary, directory / name)
        except FileExistsError:
            if DrawingMediaStore._read(directory, directory_fd, name) != body:
                raise ValueError("drawing_media_conflict") from None
        finally:
            os.unlink(path, dir_fd=directory_fd)

    @staticmethod
    def _parse(reference: str) -> tuple[UUID, str]:
        match = _REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
        if not match:
            raise ValueError("drawing_media_reference_invalid")
        try:
            task_id = UUID(match.group(1))
        except ValueError:
            raise ValueError("drawing_media_reference_invalid") from None
        if str(task_id) != match.group(1):
            raise ValueError("drawing_media_reference_invalid")
        return task_id, match.group(2)

    def read(self, reference: str, *, expected_sha256: str) -> bytes:
        task_id, name = self._parse(reference)
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("drawing_media_reference_invalid")
        with self._directory(task_id, create=False) as (directory, directory_fd):
            body = self._read(directory, directory_fd, name)
        if hashlib.sha256(body).hexdigest() != expected_sha256:
            raise ValueError("drawing_media_changed")
        return body

    def delete(self, reference: str, *, expected_sha256: str) -> None:
        task_id, name = self._parse(reference)
        with self._directory(task_id, create=False) as (directory, directory_fd):
            body = self._read(directory, directory_fd, name)
            if hashlib.sha256(body).hexdigest() != expected_sha256:
                raise ValueError("drawing_media_changed")
            os.unlink(name if directory_fd is not None else directory / name, dir_fd=directory_fd)

    def delete_if_present(self, reference: str, *, expected_sha256: str) -> bool:
        try:
            self.delete(reference, expected_sha256=expected_sha256)
            return True
        except FileNotFoundError:
            return False

    def remove_empty(self, task_id: UUID) -> None:
        try:
            with self._staging_lock, self._directory(task_id, create=False) as (directory, directory_fd):
                names = os.listdir(directory_fd if directory_fd is not None else directory)
                if names != [_MARKER]:
                    return
                os.unlink(_MARKER if directory_fd is not None else directory / _MARKER, dir_fd=directory_fd)
            directory.rmdir()
        except (FileNotFoundError, OSError):
            pass

    def _orphan_files(self, task_id: UUID, cutoff: float) -> list[str] | None:
        try:
            with self._directory(task_id, create=False) as (directory, directory_fd):
                if directory.stat().st_mtime > cutoff:
                    return None
                names = []
                with os.scandir(directory_fd if directory_fd is not None else directory) as entries:
                    for entry in entries:
                        if len(names) >= 10 or entry.name != _MARKER and not _OWNED_FILE.fullmatch(entry.name):
                            return None
                        info = entry.stat(follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode) or info.st_mtime > cutoff or info.st_size > MAX_BYTES:
                            return None
                        names.append(entry.name)
                return names if _MARKER in names else None
        except (FileNotFoundError, OSError, ValueError):
            return None

    def orphan_candidates(self, *, max_scan: int = 100, limit: int = 5, age_seconds: int = 3600):
        root = self.root / 'al1s-drawing'
        if self._scan is None:
            try:
                info = root.lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    return []
                self._scan = os.scandir(root)
            except FileNotFoundError:
                return []
        cutoff, result = time.time() - age_seconds, []
        for _ in range(max_scan):
            try:
                entry = next(self._scan)
            except StopIteration:
                self._scan.close()
                self._scan = None
                break
            try:
                task_id = UUID(entry.name)
                if str(task_id) == entry.name and entry.is_dir(follow_symlinks=False):
                    if self._orphan_files(task_id, cutoff) is not None:
                        result.append(task_id)
                        if len(result) >= limit:
                            break
            except (ValueError, OSError):
                continue
        return result

    def delete_orphan(self, task_id: UUID, *, age_seconds: int = 3600) -> bool:
        names = self._orphan_files(task_id, time.time() - age_seconds)
        if names is None:
            return False
        with self._directory(task_id, create=False) as (directory, directory_fd):
            for name in names:
                if name != _MARKER:
                    os.unlink(name if directory_fd is not None else directory / name, dir_fd=directory_fd)
            os.unlink(_MARKER if directory_fd is not None else directory / _MARKER, dir_fd=directory_fd)
        directory.rmdir()
        return True
