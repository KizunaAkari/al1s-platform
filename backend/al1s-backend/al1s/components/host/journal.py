import importlib
import json
import os
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from al1s.components.host.contracts import JsonObject


class HostJournal:
    @staticmethod
    def scope(payload: JsonObject) -> str:
        target = payload.get("operation", {}).get("target", {})
        if target.get("kind") == "bot":
            alias = target.get("bot_alias")
            if alias not in {"qq", "discord"}:
                raise ValueError("invalid_bot_scope")
            return "bot:" + str(alias)
        return "platform"

    @staticmethod
    def scope_file(scope: str) -> str:
        files = {
            "platform": "maintenance.lock",
            "bot:qq": "maintenance-qq.lock",
            "bot:discord": "maintenance-discord.lock",
        }
        if scope not in files:
            raise ValueError("unregistered_maintenance_scope")
        return files[scope]

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
        final = data.get("state") in {"succeeded", "failed_safe"}
        if final:
            self.write(self.path(identity), data)
        payload = data.get("payload", {})
        if isinstance(payload, dict) and isinstance(payload.get("operation"), dict):
            scope = self.scope(payload)
            index = self.directory / (self.scope_file(scope) + ".json")
            prior = self.scope_record(scope)
            if (
                prior
                and prior.get("operation_id") != str(identity)
                and prior.get("state") not in {"succeeded", "failed_safe"}
            ):
                if data.get("state") not in {"succeeded", "failed_safe"}:
                    raise ValueError("component_scope_unresolved")
            else:
                # Non-final lock evidence precedes physical work; final proof precedes unlock.
                self.write(
                    index, {"operation_id": str(identity), "state": data.get("state", "unknown")}
                )
        if not final:
            self.write(self.path(identity), data)

    def write(self, destination: Path, data: JsonObject) -> None:
        temporary = self.directory / f".{uuid4()}.tmp"
        with temporary.open("x", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            json.dump(data, output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)

    def scope_record(self, scope: str) -> JsonObject | None:
        path = self.directory / (self.scope_file(scope) + ".json")
        if not path.exists():
            return None
        if path.stat().st_size > 16_384:
            raise ValueError("component_scope_evidence_invalid")
        return cast(JsonObject, json.loads(path.read_text(encoding="utf-8")))

    @contextmanager
    def exclusive(self, scope: str = "platform") -> Any:
        scopes = ("platform", "bot:qq", "bot:discord") if scope == "platform" else (scope,)
        with ExitStack() as locks:
            for item in scopes:
                locks.enter_context(self._file_lock(item))
            yield

    def assert_available(self, scope: str, identity: UUID) -> None:
        scopes = (
            ("platform", "bot:qq", "bot:discord") if scope == "platform" else ("platform", scope)
        )
        for item in scopes:
            path = self.directory / (self.scope_file(item) + ".json")
            if not path.exists():
                continue
            if path.stat().st_size > 16_384:
                raise ValueError("component_scope_evidence_invalid")
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("operation_id") != str(identity) and value.get("state") not in {
                "succeeded",
                "failed_safe",
            }:
                raise ValueError("component_scope_unresolved")

    @contextmanager
    def _file_lock(self, scope: str) -> Any:
        with (self.directory / self.scope_file(scope)).open("a+b") as lock:
            lock.seek(0)
            if lock.read(1) == b"":
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            if os.name == "nt":
                msvcrt = importlib.import_module("msvcrt")

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
