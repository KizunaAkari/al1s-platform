"""Read only the fixed retained-log collector through the existing Docker control host."""

import subprocess
from pathlib import Path

from al1s.bots.container_control import Alias, ControlError
from al1s.bots.log_contracts import LogLevel, LogPage, LogSource
from al1s.components.host.commands import CommandRunner


class BotLogReader:
    def __init__(self, runner: CommandRunner | None = None):
        self.runner = runner or CommandRunner()

    def read(self, alias: Alias, source: LogSource, level: LogLevel) -> dict:
        if alias not in {"qq", "discord"} or source not in {"all", "bot", "backend", "gateway", "ops"} or level not in {"all", "important"}:
            raise ValueError("invalid_log_filter")
        try:
            reader = Path(__file__).with_name("retained_logs.py").read_text(encoding="utf-8")
            output = self.runner.run([
                "docker", "exec", "al1s-platform-log-collector", "python", "-c", reader,
                alias, source, level,
            ], timeout=20)
            return LogPage.model_validate_json(output).model_dump(mode="json")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise ControlError("bot_logs_unavailable", 503) from error
