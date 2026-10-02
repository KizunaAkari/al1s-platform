from __future__ import annotations

from typing import Protocol
from uuid import UUID

from al1s.secrets.types import SecretRecord


class SecretRepository(Protocol):
    def add(self, record: SecretRecord) -> None: ...

    def get(self, secret_id: UUID) -> SecretRecord | None: ...

    def delete_if_unreferenced(self, secret_id: UUID) -> bool: ...

    def delete_many_if_unreferenced(self, secret_ids: list[UUID]) -> int: ...


class SecretCipher(Protocol):
    key_id: str

    def encrypt(self, plaintext: str) -> str: ...

    def decrypt(self, ciphertext: str, key_id: str) -> str: ...


class SecretFingerprinter(Protocol):
    def fingerprint(self, plaintext: str) -> str: ...
