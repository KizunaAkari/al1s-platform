from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.bot_models import BotConfigVersionRow
from al1s.adapters.postgres.notification_models import EncryptedSecretRow, NotificationChannelRow
from al1s.secrets.types import SecretRecord


class PostgresSecretRepository:
    """Shared encrypted-secret storage with cross-module reference protection."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, record: SecretRecord) -> None:
        self._session.add(
            EncryptedSecretRow(
                id=record.secret_id,
                purpose=record.purpose,
                ciphertext=record.ciphertext,
                key_id=record.key_id,
                row_version=record.row_version,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
        )

    def get(self, secret_id: UUID) -> SecretRecord | None:
        row = self._session.get(EncryptedSecretRow, secret_id)
        if row is None:
            return None
        return SecretRecord(
            secret_id=row.id,
            purpose=row.purpose,
            ciphertext=row.ciphertext,
            key_id=row.key_id,
            row_version=row.row_version,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def delete_if_unreferenced(self, secret_id: UUID) -> bool:
        return self.delete_many_if_unreferenced([secret_id]) == 1

    def delete_many_if_unreferenced(self, secret_ids: list[UUID]) -> int:
        if not secret_ids:
            return 0
        if len(secret_ids) > 100:
            raise ValueError("Secret deletion batch exceeds 100")
        notification_reference = (
            select(NotificationChannelRow.id)
            .where(NotificationChannelRow.secret_id == EncryptedSecretRow.id)
            .exists()
        )
        bot_reference = (
            select(BotConfigVersionRow.id)
            .where(BotConfigVersionRow.secret_id == EncryptedSecretRow.id)
            .exists()
        )
        deleted_ids = self._session.scalars(
            delete(EncryptedSecretRow)
            .where(
                EncryptedSecretRow.id.in_(secret_ids),
                ~or_(notification_reference, bot_reference),
            )
            .returning(EncryptedSecretRow.id)
        ).all()
        return len(deleted_ids)
