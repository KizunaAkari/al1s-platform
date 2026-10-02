from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from al1s.adapters.postgres.notification_models import NotificationDeliveryRow
from al1s.adapters.postgres.review_models import MessageReviewRow


class PostgresReviewRepository:
    def __init__(self, session: Session):
        self._session = session

    def get(self, review_id: UUID) -> MessageReviewRow | None:
        return self._session.scalar(
            select(MessageReviewRow).where(MessageReviewRow.id == review_id).with_for_update()
        )

    def by_source(self, service_id: UUID, message_id: str) -> MessageReviewRow | None:
        return self._session.scalar(
            select(MessageReviewRow)
            .where(
                MessageReviewRow.service_id == service_id,
                MessageReviewRow.message_id == message_id,
            )
            .with_for_update()
        )

    def add(self, values: dict[str, object]) -> bool:
        statement = (
            insert(MessageReviewRow)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(MessageReviewRow.id)
        )
        return self._session.scalar(statement) is not None

    def add_many(self, rows: list[dict[str, object]]) -> set[UUID]:
        if not rows:
            return set()
        statement = (
            insert(MessageReviewRow)
            .values(rows)
            .on_conflict_do_nothing()
            .returning(
                MessageReviewRow.id,
            )
        )
        return set(self._session.scalars(statement))

    def by_sources(self, service_id: UUID, message_ids: list[str]) -> list[MessageReviewRow]:
        if not message_ids:
            return []
        return list(
            self._session.scalars(
                select(MessageReviewRow)
                .where(
                    MessageReviewRow.service_id == service_id,
                    MessageReviewRow.message_id.in_(message_ids),
                )
                .with_for_update()
            )
        )

    def page(
        self, state: str | None, before: datetime | None, before_id: UUID | None, limit: int
    ) -> list[MessageReviewRow]:
        from sqlalchemy import tuple_

        statement = select(MessageReviewRow).where(MessageReviewRow.deleted_at.is_(None))
        if state:
            statement = statement.where(MessageReviewRow.state == state)
        if before is not None and before_id is not None:
            statement = statement.where(
                tuple_(MessageReviewRow.received_at, MessageReviewRow.id) < (before, before_id),
            )
        return list(
            self._session.scalars(
                statement.order_by(
                    MessageReviewRow.received_at.desc(),
                    MessageReviewRow.id.desc(),
                ).limit(min(max(limit, 1), 50))
            )
        )

    def batch(self, ids: list[UUID]) -> list[MessageReviewRow]:
        return list(
            self._session.scalars(
                select(MessageReviewRow)
                .where(
                    MessageReviewRow.id.in_(ids),
                )
                .order_by(MessageReviewRow.id)
                .with_for_update()
            )
        )

    def maintenance(self, limit: int = 50) -> list[MessageReviewRow]:
        return list(
            self._session.scalars(
                select(MessageReviewRow)
                .where(
                    MessageReviewRow.deleted_at.is_(None),
                )
                .order_by(MessageReviewRow.checked_at, MessageReviewRow.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )

    def unsettled(self, ids: list[UUID]) -> set[UUID]:
        return set(
            self._session.scalars(
                select(NotificationDeliveryRow.intent_id)
                .where(
                    NotificationDeliveryRow.intent_id.in_(ids),
                    NotificationDeliveryRow.status.in_(["pending", "processing"]),
                )
                .distinct()
            )
        )

    def cancel_unsettled(self, ids: list[UUID], now: datetime) -> None:
        if not ids:
            return
        self._session.execute(
            update(NotificationDeliveryRow)
            .where(
                NotificationDeliveryRow.intent_id.in_(ids),
                NotificationDeliveryRow.status.in_(["pending", "processing"]),
            )
            .values(
                status="cancelled",
                cancelled_at=now,
                locked_by=None,
                locked_until=None,
                row_version=NotificationDeliveryRow.row_version + 1,
                last_error_code="review_expired",
                last_error_type="review_policy",
            )
        )
