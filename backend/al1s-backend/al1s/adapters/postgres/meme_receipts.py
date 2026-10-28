from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from al1s.adapters.postgres.meme_models import (
    MemeMutationRow,
    MemeReceiptRow,
    MemeSendGateRow,
    MemeTemplateRow,
    MemeVersionRow,
)
from al1s.adapters.postgres.meme_rate_limit import finish_send, reserve_send
from al1s.adapters.postgres.meme_settings import MemeSettingsRepository
from al1s.meme_workshop.contracts import CommandSource


class MemeReceiptRepository(MemeSettingsRepository):
    def receipt_available(self, bot_id: UUID, receipt_id: UUID) -> bool:
        with self.sessions() as session:
            return (
                session.scalar(
                    select(MemeReceiptRow.id).where(
                        MemeReceiptRow.id == receipt_id,
                        MemeReceiptRow.bot_service_id == bot_id,
                        MemeReceiptRow.status == "claimed",
                        MemeReceiptRow.claimed_at > datetime.now(UTC) - timedelta(seconds=15),
                    )
                )
                is not None
            )

    def claim(self, bot_id: UUID, source: CommandSource) -> UUID | None:
        now, identity = datetime.now(UTC), uuid4()
        with self.sessions.begin() as session:
            if not self.allowed_in(session, bot_id, source, lock=True):
                return None
            platform = "qq" if source.scope_kind.startswith("qq_") else "discord"
            gate = session.get(MemeSendGateRow, platform)
            blocked = gate is not None and gate.available_at > now
            result = session.scalar(
                insert(MemeReceiptRow)
                .values(
                    id=identity,
                    bot_service_id=bot_id,
                    source_message_id=source.message_id,
                    scope_kind=source.scope_kind,
                    target_id=source.target_id,
                    sender_id=source.sender_id,
                    guild_id=source.guild_id,
                    status="failed" if blocked else "claimed",
                    error_code="meme_rate_limited" if blocked else None,
                    claimed_at=now,
                    expires_at=now + timedelta(hours=24),
                )
                .on_conflict_do_nothing(
                    constraint="uq_meme_source_message",
                )
                .returning(MemeReceiptRow.id)
            )
            return None if blocked else result

    def bind_template(self, bot_id: UUID, receipt_id: UUID, version_id: UUID) -> bool:
        with self.sessions.begin() as session:
            return (
                session.scalar(
                    update(MemeReceiptRow)
                    .where(
                        MemeReceiptRow.id == receipt_id,
                        MemeReceiptRow.bot_service_id == bot_id,
                        MemeReceiptRow.status == "claimed",
                        MemeReceiptRow.claimed_at > datetime.now(UTC) - timedelta(seconds=15),
                    )
                    .values(template_version_id=version_id)
                    .returning(MemeReceiptRow.id)
                )
                is not None
            )

    def start_send(self, bot_id: UUID, receipt_id: UUID, error_code: str | None = None) -> bool:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(MemeReceiptRow, receipt_id, with_for_update=True)
            if (
                row is None
                or row.bot_service_id != bot_id
                or row.status != "claimed"
                or row.claimed_at <= now - timedelta(seconds=15)
            ):
                return False
            source = CommandSource(
                message_id=row.source_message_id,
                scope_kind=row.scope_kind,
                target_id=row.target_id,
                sender_id=row.sender_id,
                guild_id=row.guild_id,
            )
            if not self.allowed_in(session, bot_id, source, lock=True):
                row.status = "failed"
                return False
            if row.template_version_id:
                template = session.scalar(
                    select(MemeTemplateRow)
                    .join(
                        MemeVersionRow,
                        MemeVersionRow.template_id == MemeTemplateRow.id,
                    )
                    .where(MemeVersionRow.id == row.template_version_id)
                    .with_for_update(
                        read=True,
                        of=MemeTemplateRow,
                    )
                )
                if template is None or template.deleted_at or not template.enabled:
                    row.status = "failed"
                    return False
            platform = "qq" if row.scope_kind.startswith("qq_") else "discord"
            if not reserve_send(session, platform, receipt_id, now):
                row.status, row.error_code = "failed", "meme_rate_limited"
                return False
            row.status = "send_started"
            row.error_code = error_code
            return True

    def settle(
        self, bot_id: UUID, receipt_id: UUID, status: str, error_code: str | None = None
    ) -> None:
        if status not in {"sent", "failed", "unknown"}:
            raise ValueError("invalid_receipt_status")
        with self.sessions.begin() as session:
            existing = session.get(MemeReceiptRow, receipt_id, with_for_update=True)
            if existing is None or existing.bot_service_id != bot_id:
                return
            if existing.status == "send_started":
                finish_send(session, receipt_id)
            session.execute(
                update(MemeReceiptRow)
                .where(
                    MemeReceiptRow.id == receipt_id,
                    MemeReceiptRow.bot_service_id == bot_id,
                    MemeReceiptRow.status.in_(["claimed", "send_started"]),
                )
                .values(status=status, error_code=error_code)
            )

    def cleanup(self, now: datetime, *, limit: int = 50) -> None:
        if not 1 <= limit <= 50:
            raise ValueError("invalid_cleanup_limit")
        with self.sessions.begin() as session:
            for model, key in (
                (MemeReceiptRow, MemeReceiptRow.id),
                (MemeMutationRow, MemeMutationRow.key),
            ):
                expired = (
                    select(key)
                    .where(model.expires_at <= now)
                    .order_by(
                        model.expires_at,
                        key,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
                session.execute(delete(model).where(key.in_(expired)))
            self._cleanup_versions(session, now, limit)
            self._cleanup_templates(session, now, limit)

    @staticmethod
    def _cleanup_templates(session: Session, now: datetime, limit: int) -> None:
        unused = (
            select(MemeTemplateRow.id)
            .where(
                MemeTemplateRow.deleted_at <= now - timedelta(hours=24),
                ~exists(
                    select(MemeVersionRow.id).where(
                        MemeVersionRow.template_id == MemeTemplateRow.id,
                    )
                ),
                ~exists(
                    select(MemeMutationRow.key).where(
                        MemeMutationRow.template_id == MemeTemplateRow.id,
                    )
                ),
            )
            .order_by(MemeTemplateRow.deleted_at, MemeTemplateRow.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        session.execute(delete(MemeTemplateRow).where(MemeTemplateRow.id.in_(unused)))

    @staticmethod
    def _cleanup_versions(session: Session, now: datetime, limit: int) -> None:
        protected = (
            exists(
                select(MemeTemplateRow.id).where(
                    MemeTemplateRow.current_version_id == MemeVersionRow.id,
                )
            )
            | exists(
                select(MemeReceiptRow.id).where(
                    MemeReceiptRow.template_version_id == MemeVersionRow.id,
                )
            )
            | exists(
                select(MemeMutationRow.key).where(
                    MemeMutationRow.version_id == MemeVersionRow.id,
                )
            )
        )
        obsolete = (
            select(MemeVersionRow.id)
            .where(
                ~protected,
                MemeVersionRow.created_at <= now - timedelta(hours=24),
                or_(MemeVersionRow.lease_until.is_(None), MemeVersionRow.lease_until <= now),
            )
            .order_by(MemeVersionRow.created_at, MemeVersionRow.id)
            .limit(limit)
            .with_for_update(
                skip_locked=True,
            )
        )
        session.execute(delete(MemeVersionRow).where(MemeVersionRow.id.in_(obsolete)))

    def lease_version(self, version_id: UUID) -> bool:
        with self.sessions.begin() as session:
            until = datetime.now(UTC) + timedelta(seconds=60)
            return (
                session.scalar(
                    update(MemeVersionRow)
                    .where(MemeVersionRow.id == version_id)
                    .values(
                        lease_until=func.greatest(
                            func.coalesce(MemeVersionRow.lease_until, until), until
                        ),
                    )
                    .returning(MemeVersionRow.id)
                )
                is not None
            )
