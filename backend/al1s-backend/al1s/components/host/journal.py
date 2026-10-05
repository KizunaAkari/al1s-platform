import importlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from al1s.components.host.contracts import JsonObject


class HostJournal:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, identity: UUID) -> Path:
        return self.directory / f"{UUID(str(identity))}.json"

    def load(self, identity: UUID) -> JsonObject | None:
        path = self.path(identity)
        if not path.exists():
            return None
        if path.stat().st_size > 1024**2:
            raise ValueError("component_journal_oversized")
        return cast(JsonObject, json.loads(path.read_text(encoding="utf-8")))

    def save(self, identity: UUID, data: JsonObject) -> None:
        destination = self.path(identity)
        temporary = self.directory / f".{uuid4()}.tmp"
        with temporary.open("x", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            json.dump(data, output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)

    @contextmanager
    def exclusive(self) -> Any:
        with (self.directory / "maintenance.lock").open("a+b") as lock:
            lock.seek(0)
            if lock.read(1) == b"":
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)
