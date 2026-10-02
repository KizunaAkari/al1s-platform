from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class SecretRecord:
    secret_id: UUID
    purpose: str
    ciphertext: str
    key_id: str
    row_version: int
    created_at: datetime
    updated_at: datetime
