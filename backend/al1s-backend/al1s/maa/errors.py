from __future__ import annotations

from typing import Any

from al1s.execution.errors import ExecutionDomainError


class MaaDomainError(ExecutionDomainError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code, message, status_code)
        self.context = context or {}


class ArchiveValidationError(MaaDomainError):
    def __init__(self, code: str, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(code, message, 422, context=context)
