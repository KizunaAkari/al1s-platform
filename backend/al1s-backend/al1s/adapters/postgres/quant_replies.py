"""Persistent QQ-only reply queue with one global send gate."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.bot_models import BotServiceRow
from al1s.adapters.postgres.meme_base import fingerprint
from al1s.adapters.postgres.quant_models import QuantReplyRow, QuantSendGateRow, QuantSettingsRow
from al1s.meme_workshop.contracts import CommandSource
from al1s.quant.commands import QuantCommand


@dataclass(frozen=True, slots=True)
class Reply:
    id: UUID
    service_id: UUID
    source: CommandSource
    content: str
    status: str
    image: bytes | None = None
    mention_id: str | None = None
    quote_message: bool = True
    expires_at: datetime | None = None
    action: str = "help"


def _reply(row: QuantReplyRow) -> Reply:
    return Reply(
        row.id,
        row.service_id,
        CommandSource(
            scope_kind=row.scope_kind,
            target_id=row.target_id,
            sender_id=row.sender_id,
            message_id=row.message_id,
        ),
        row.content,
        row.status,
        row.image,
        row.mention_id,
        row.quote_message,
        row.expires_at,
        row.action,
    )


class QuantReplies:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def enqueue(
        self,
        service_id: UUID,
        command: QuantCommand,
        content: str,
        interval_seconds: int,
        *,
        image: bytes | None = None,
    ) -> tuple[Reply, bool]:
        if (
            not isinstance(content, str)
            or not 1 <= len(content) <= 4096
            or type(interval_seconds) is not int
            or not 1 <= interval_seconds <= 3600
            or command.source.scope_kind not in {"qq_group", "qq_private"}
        ):
            raise ValueError("quant_reply_invalid")
        if image is not None and (
            not isinstance(image, bytes)
            or not image.startswith(b"\x89PNG\r\n\x1a\n")
            or len(image) > 524288
        ):
            raise ValueError("quant_reply_image_invalid")
        source = command.source
        digest = fingerprint(
            {
                "action": command.action,
                "arguments": command.arguments,
                "mapping_version": command.mapping_version,
            }
        )
        now = datetime.now(UTC)
        key_columns = [
            QuantReplyRow.service_id,
            QuantReplyRow.scope_kind,
            QuantReplyRow.target_id,
            QuantReplyRow.sender_id,
            QuantReplyRow.message_id,
            QuantReplyRow.event_time,
        ]
        with self.sessions.begin() as session:
            inserted = session.scalar(
                insert(QuantReplyRow)
                .values(
                    id=uuid4(),
                    service_id=service_id,
                    scope_kind=source.scope_kind,
                    target_id=source.target_id,
                    sender_id=source.sender_id,
                    message_id=source.message_id,
                    event_time=command.event_timestamp,
                    request_sha256=digest,
                    action=command.action,
                    mapping_version=command.mapping_version,
                    content=content,
                    image=image,
                    expires_at=now + timedelta(minutes=15) if image is not None else None,
                    interval_seconds=interval_seconds,
                    status="queued",
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=key_columns)
                .returning(QuantReplyRow.id)
            )
            row = session.scalar(
                select(QuantReplyRow).where(
                    QuantReplyRow.service_id == service_id,
                    QuantReplyRow.scope_kind == source.scope_kind,
                    QuantReplyRow.target_id == source.target_id,
                    QuantReplyRow.sender_id == source.sender_id,
                    QuantReplyRow.message_id == source.message_id,
                    QuantReplyRow.event_time == command.event_timestamp,
                )
            )
            if row is None:
                raise RuntimeError("quant_reply_unavailable")
            if row.request_sha256 != digest:
                raise ValueError("quant_event_conflict")
            return _reply(row), inserted is not None

    def claim_next(
        self, now: datetime, *, eligible_services: set[UUID] | None = None
    ) -> Reply | None:
        if now.tzinfo is None:
            raise ValueError("quant_reply_time_invalid")
        with self.sessions.begin() as session:
            expired = (
                select(QuantReplyRow.id)
                .where(QuantReplyRow.status == "queued", QuantReplyRow.expires_at <= now)
                .order_by(QuantReplyRow.expires_at, QuantReplyRow.id)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
            session.execute(
                update(QuantReplyRow)
                .where(QuantReplyRow.id.in_(expired))
                .values(status="unknown", image=None, updated_at=now)
            )
            if eligible_services is not None and not eligible_services:
                return None
            session.execute(
                insert(QuantSendGateRow)
                .values(platform="qq", next_allowed_at=None)
                .on_conflict_do_nothing(index_elements=[QuantSendGateRow.platform])
            )
            gate = session.get(QuantSendGateRow, "qq", with_for_update=True)
            if gate is None:
                raise RuntimeError("quant_gate_unavailable")
            configured = (
                session.scalar(
                    select(func.max(QuantSettingsRow.reply_interval_seconds))
                    .join(BotServiceRow, BotServiceRow.id == QuantSettingsRow.service_id)
                    .where(
                        QuantSettingsRow.enabled,
                        BotServiceRow.enabled,
                        BotServiceRow.deleted_at.is_(None),
                    )
                )
                or 0
            )
            next_at = gate.next_allowed_at
            if next_at is not None:
                if next_at.tzinfo is None:
                    next_at = next_at.replace(tzinfo=UTC)
                if now < next_at:
                    return None
            last_started = gate.last_started_at
            if last_started is not None:
                if last_started.tzinfo is None:
                    last_started = last_started.replace(tzinfo=UTC)
                if now < last_started + timedelta(seconds=configured):
                    return None
            query = (
                select(QuantReplyRow)
                .join(BotServiceRow, BotServiceRow.id == QuantReplyRow.service_id)
                .where(
                    QuantReplyRow.status == "queued",
                    or_(QuantReplyRow.expires_at.is_(None), QuantReplyRow.expires_at > now),
                    BotServiceRow.enabled,
                    BotServiceRow.deleted_at.is_(None),
                )
            )
            if eligible_services is not None:
                query = query.where(QuantReplyRow.service_id.in_(eligible_services))
            row = session.scalar(
                query.order_by(QuantReplyRow.created_at, QuantReplyRow.id)
                .limit(1)
                .with_for_update(skip_locked=True, of=QuantReplyRow)
            )
            if row is None:
                return None
            row.status, row.updated_at = "sending", now
            gate.next_allowed_at = now + timedelta(
                seconds=max(row.interval_seconds, configured or 0)
            )
            gate.last_started_at = now
            return _reply(row)

    def settle(self, reply_id: UUID, status: str, now: datetime) -> None:
        if status not in {"sent", "unknown"} or now.tzinfo is None:
            raise ValueError("quant_reply_state_invalid")
        with self.sessions.begin() as session:
            row = session.get(QuantReplyRow, reply_id, with_for_update=True)
            if row is None or (row.status != "sending" and row.status != status):
                raise ValueError("quant_reply_state_conflict")
            row.status, row.updated_at = status, now
            row.image = None

    def recover_sending(self, now: datetime) -> int:
        if now.tzinfo is None:
            raise ValueError("quant_reply_time_invalid")
        with self.sessions.begin() as session:
            result = session.execute(
                update(QuantReplyRow)
                .where(QuantReplyRow.status == "sending")
                .values(status="unknown", image=None, updated_at=now)
            )
            return result.rowcount

    def can_send(self, reply: Reply) -> bool:
        if reply.action not in {"f_exit", "chart", "f_chart"}:
            return True
        from al1s.adapters.postgres.freqtrade_models import FreqtradeSettingsRow

        with self.sessions() as session:
            settings = session.get(FreqtradeSettingsRow, reply.service_id)
            return bool(
                settings
                and settings.enabled
                and (reply.action == "chart" or settings.futures_enabled)
                and (
                    (
                        reply.source.scope_kind == "qq_group"
                        and reply.source.target_id in settings.groups
                    )
                    or (reply.source.scope_kind == "qq_private" and settings.private_enabled)
                )
            )
