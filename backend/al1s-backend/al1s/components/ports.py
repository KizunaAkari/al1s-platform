from typing import Protocol
from uuid import UUID

from al1s.components.types import NativeComponent, TerminalProfile


class InventoryReader(Protocol):
    def get_linux_profile(self, identity: UUID) -> TerminalProfile | None: ...
    def list_linux_profiles(
        self, limit: int, cursor: UUID | None
    ) -> tuple[list[TerminalProfile], UUID | None]: ...


class ComponentHost(Protocol):
    def inventory(self) -> list[NativeComponent]: ...
