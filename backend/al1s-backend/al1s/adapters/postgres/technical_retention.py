"""Remove settled debug copies, never reliable execution state or dedupe IDs."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.elements import ColumnElement

from al1s.adapters.postgres.maa_models import MaaQuickTestEventRow
from al1s.adapters.postgres.models import AuditLogRow, OutboxEventRow

MODULE_OWNERS = ("platform", "maa", "information")


class PostgresTechnicalRetention:
    def __init__(self, session: Session, *, owner_module: str) -> None:
        if owner_module not in MODULE_OWNERS:
            raise ValueError("unknown module owner")
        self._session = session
        self._owner = owner_module

    def redact(self, now: datetime, *, limit: int = 50) -> tuple[int, int]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        cutoff = now - timedelta(days=7)
        outbox = self._redact(
            OutboxEventRow, OutboxEventRow.published_at, cutoff, now, limit,
            {"payload": {}, "last_error_type": None,
             "row_version": OutboxEventRow.row_version + 1},
            OutboxEventRow.status == "published",
        )
        audit = self._redact(
            AuditLogRow, AuditLogRow.created_at, cutoff, now, limit, {"details": {}},
        )
        if self._owner == "maa":
            self._delete_quick_test_events(cutoff, limit)
        return outbox, audit

    def _delete_quick_test_events(self, cutoff: datetime, limit: int) -> None:
        candidates = (
            select(MaaQuickTestEventRow.session_id, MaaQuickTestEventRow.sequence)
            .where(MaaQuickTestEventRow.created_at <= cutoff)
            .order_by(MaaQuickTestEventRow.created_at,
                      MaaQuickTestEventRow.session_id,
                      MaaQuickTestEventRow.sequence)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte()
        )
        self._session.execute(
            delete(MaaQuickTestEventRow).where(
                tuple_(MaaQuickTestEventRow.session_id, MaaQuickTestEventRow.sequence)
                .in_(select(candidates.c.session_id, candidates.c.sequence))
            )
        )

    def _redact(
        self,
        model: type[OutboxEventRow] | type[AuditLogRow],
        timestamp: InstrumentedAttribute[datetime] | InstrumentedAttribute[datetime | None],
        cutoff: datetime,
        now: datetime,
        limit: int,
        values: dict[str, Any],
        *conditions: ColumnElement[bool],
    ) -> int:
        candidates = (
            select(model.id)
            .where(model.owner_module == self._owner,
                   model.details_purged_at.is_(None), timestamp <= cutoff, *conditions)
            .order_by(timestamp, model.id).limit(limit)
            .with_for_update(skip_locked=True).cte()
        )
        statement = (
            update(model).where(model.id.in_(select(candidates.c.id)))
            .values(**values, details_purged_at=now).returning(model.id)
            .execution_options(synchronize_session=False)
        )
        return len(self._session.scalars(statement).all())
