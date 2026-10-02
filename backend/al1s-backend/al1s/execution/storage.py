"""Latest terminal filesystem observation and hysteresis; no filesystem access here."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StorageObservation:
    directory: str
    total_bytes: int
    used_bytes: int
    available_bytes: int

    def __post_init__(self) -> None:
        values = (self.total_bytes, self.used_bytes, self.available_bytes)
        if (not self.directory or len(self.directory) > 1024
                or any(type(v) is not int or not 0 <= v < 2**63 for v in values)
                or self.total_bytes == 0
                or self.used_bytes + self.available_bytes > self.total_bytes):
            raise ValueError("Invalid filesystem capacity observation")

    def alert_state(self, previously_low: bool) -> bool:
        if self.available_bytes * 100 >= self.total_bytes * 25:
            return False
        return previously_low or self.available_bytes * 100 < self.total_bytes * 20
