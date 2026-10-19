import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from al1s.components.bot_contracts import BotAlias
from al1s.components.host.journal import HostJournal


class BotMaintenanceGuard:
    def __init__(self, directory: Path):
        self.journal = HostJournal(directory)

    def assert_command_allowed(self, alias: BotAlias) -> None:
        for scope in ("platform", "bot:" + alias):
            path = self.journal.directory / (self.journal.scope_file(scope) + ".json")
            if not path.exists():
                continue
            if path.stat().st_size > 16_384:
                raise ValueError("bot_maintenance_evidence_invalid")
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("state") not in {"succeeded", "failed_safe"}:
                raise ValueError("bot_maintenance_unresolved")

    @contextmanager
    def exclusive(self, alias: BotAlias) -> Any:
        with self.journal.exclusive("bot:" + alias):
            self.assert_command_allowed(alias)
            yield
