from al1s.kernel.ports import KernelMaintenanceReader
from al1s.kernel.types import KernelMaintenanceSnapshot


class KernelMaintenanceService:
    """Read-only application service exposed to the system-maintenance API."""

    def __init__(self, reader: KernelMaintenanceReader) -> None:
        self._reader = reader

    def snapshot(self) -> KernelMaintenanceSnapshot:
        return self._reader.snapshot()
