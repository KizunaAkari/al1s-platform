import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath
from uuid import UUID

CORE_HASHES = {
    "main.py": "baaa16d69ee78e08c490eeec20bf27c56b746c070f76cc05b4aa13cbc616bcef",
    "server.py": "52618f91c663dc6524a028162212e0434fc9239618ddc0efbf7c605027081c8c",
    "nodes.py": "776f549a05fc068eca20a9edc1602c6f1b80cfbbf5153bc44ba371a55fbb13be",
    "execution.py": "25b12805b420804d634dcb505ff5d2e2c485735557dbcfb3f7f5fd093fff1505",
    "comfy/model_management.py": "63610e41cf06fccb5eeaa153c5fd3799e01a0c645facba844763781d2036989a",
    "comfy_execution/caching.py": "f71d43f6196330e73674e13795a9d392a95750ad55be2c9d96cf54355794b862",
    "comfy_extras/nodes_upscale_model.py": "9c1332b15585c3f88291639bedf32d51c3bf3b2e8d9347018e7df49d92382531",
    "comfy_extras/nodes_flux.py": "e6fe6d10e7b538c6bb8d1edbba6688242e2f9b0cb69b9a06bfbde3c4e51d14e6",
    "comfy_extras/nodes_qwen.py": "61a5e27f90bb786cd4f6e76ee86920582ffef2ee3e65cde7b2278c7171c559e8",
    "comfy_extras/nodes_slg.py": "745270f54ecfe86526727ec204a9955aa2791e9deca2520d7cf8bb83fe6fca50",
    "comfy_extras/nodes_eps.py": "db27a4586856a4f64f221128649f4c9e4094b89e490c25587623086ecb618b9d",
}


def _not_link(path: Path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("resource_output_invalid")
    return info


def check_files(root: Path, expected=CORE_HASHES):
    root = Path(root).resolve(strict=True)
    try:
        for relative, checksum in expected.items():
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts or "\\" in relative:
                raise ValueError("resource_version_unsupported")
            current = root
            for part in path.parts:
                current = current / part
                _not_link(current)
            digest = hashlib.sha256()
            with current.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != checksum:
                raise ValueError("resource_version_unsupported")
    except (OSError, ValueError) as error:
        raise ValueError("resource_version_unsupported") from error


def read_output(root: Path, prompt: str, filename: str, *, max_bytes: int) -> bytes:
    try:
        if (str(UUID(prompt)) != prompt or not re.fullmatch(r"output_[0-9]{5,}_[.]png", filename)
                or type(max_bytes) is not int or not 0 < max_bytes <= 8 * 1024 * 1024):
            raise ValueError("resource_output_invalid")
        root = Path(root).resolve(strict=True)
        current = root
        for part in ("al1s-drawing", prompt):
            current = current / part
            if not stat.S_ISDIR(_not_link(current).st_mode):
                raise ValueError("resource_output_invalid")
        path = current / filename
        before = _not_link(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise ValueError("resource_output_invalid")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or not stat.S_ISREG(opened.st_mode):
                raise ValueError("resource_output_invalid")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                body = stream.read(max_bytes + 1)
        finally:
            os.close(fd)
        if not body or len(body) > max_bytes:
            raise ValueError("resource_output_invalid")
        return body
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise ValueError("resource_output_invalid") from error


def remove_output(root: Path, prompt: str, filename: str) -> None:
    try:
        if str(UUID(prompt)) != prompt or not re.fullmatch(r"output_[0-9]{5,}_[.]png", filename):
            raise ValueError("resource_output_invalid")
        current = Path(root).resolve(strict=True)
        for part in ("al1s-drawing", prompt):
            current = current / part
            if not stat.S_ISDIR(_not_link(current).st_mode):
                raise ValueError("resource_output_invalid")
        path = current / filename
        before = _not_link(path)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("resource_output_invalid")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or not stat.S_ISREG(opened.st_mode):
                raise ValueError("resource_output_invalid")
        finally:
            os.close(fd)
        os.unlink(path)
        try:
            current.rmdir()
        except OSError:
            pass
    except FileNotFoundError:
        return
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise ValueError("resource_output_invalid") from error
