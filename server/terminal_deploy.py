from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,239}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TerminalArtifactStore:
    """Persistent platform-side cache for terminal deployment archives."""

    def __init__(self, root: str | Path, max_bytes: int = 8 * 1024**3):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    @staticmethod
    def validate_name(name: str) -> str:
        value = Path(name or "").name
        if value != name or not ARTIFACT_NAME.fullmatch(value):
            raise ValueError("invalid terminal artifact name")
        if not value.endswith((".tar", ".tar.gz")):
            raise ValueError("terminal artifact must be a .tar or .tar.gz file")
        return value

    def path(self, name: str) -> Path:
        return self.root / self.validate_name(name)

    def manifest_path(self, name: str) -> Path:
        return self.root / f".{self.validate_name(name)}.json"

    def list(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for manifest in sorted(self.root.glob(".*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                value = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            try:
                artifact = self.path(str(value.get("name") or ""))
            except ValueError:
                continue
            if artifact.is_file():
                result.append(value)
        return result

    def metadata(self, name: str) -> dict[str, Any] | None:
        try:
            manifest = self.manifest_path(name)
            artifact = self.path(name)
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not artifact.is_file():
            return None
        return value

    def save_stream(self, name: str, stream) -> dict[str, Any]:
        name = self.validate_name(name)
        temporary = self.root / f".{name}.{uuid.uuid4().hex}.upload"
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("wb") as target:
                while True:
                    chunk = stream.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ValueError("terminal artifact is too large")
                    digest.update(chunk)
                    target.write(chunk)
            final_path = self.path(name)
            temporary.replace(final_path)
            value = {
                "name": name,
                "size_bytes": size,
                "sha256": digest.hexdigest(),
                "created_at": utc_now(),
            }
            temporary_manifest = self.root / f".{name}.{uuid.uuid4().hex}.json.upload"
            temporary_manifest.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary_manifest.replace(self.manifest_path(name))
            return value
        finally:
            temporary.unlink(missing_ok=True)
