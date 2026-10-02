from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class OfflinePermitStatus(StrEnum):
    ISSUED = "issued"
    CONSUMED = "consumed"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class OfflineStartPermitRecord:
    permit_id: UUID
    attempt_id: UUID
    package_id: UUID
    execution_id: UUID
    terminal_id: UUID
    target_device_id: UUID | None
    package_hash: str
    permit_version: int
    token_digest: str
    status: OfflinePermitStatus
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    consumed_start_report_id: UUID | None
    revoked_at: datetime | None
    row_version: int


@dataclass(frozen=True, slots=True)
class OfflineStartPermitGrant:
    permit_id: UUID
    permit_version: int
    token: str
    issued_at: datetime
    expires_at: datetime


class OfflinePermitSigner:
    """Deterministically sign one persisted permit without storing its plaintext token."""

    def __init__(self, signing_key: str) -> None:
        if len(signing_key.encode("utf-8")) < 32:
            raise ValueError("offline permit signing key must contain at least 32 UTF-8 bytes")
        self._key = signing_key.encode("utf-8")

    def grant(self, permit: OfflineStartPermitRecord) -> OfflineStartPermitGrant:
        token = self._token(permit)
        return OfflineStartPermitGrant(
            permit_id=permit.permit_id,
            permit_version=permit.permit_version,
            token=token,
            issued_at=permit.issued_at,
            expires_at=permit.expires_at,
        )

    def digest(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def verify(self, permit: OfflineStartPermitRecord, token: str) -> bool:
        expected = self._token(permit)
        return hmac.compare_digest(expected, token) and hmac.compare_digest(
            permit.token_digest, self.digest(token)
        )

    def token_digest_for(self, permit: OfflineStartPermitRecord) -> str:
        return self.digest(self._token(permit))

    def _token(self, permit: OfflineStartPermitRecord) -> str:
        target = str(permit.target_device_id) if permit.target_device_id else "-"
        canonical = "\n".join(
            (
                "al1s-offline-start-v1",
                str(permit.permit_id),
                str(permit.permit_version),
                str(permit.attempt_id),
                str(permit.package_id),
                str(permit.execution_id),
                str(permit.terminal_id),
                target,
                permit.package_hash,
                permit.issued_at.isoformat(),
                permit.expires_at.isoformat(),
            )
        ).encode("utf-8")
        signature = hmac.new(self._key, canonical, hashlib.sha256).digest()
        encoded = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
        return f"v1.{permit.permit_id}.{encoded}"
