from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.scheduling_models import (
    StateTransitionRow,
)
from al1s.adapters.postgres.scheduling_repository_records import (
    MAX_PAGE_SIZE,
    _transition_record,
)
from al1s.execution.scheduling_types import (
    StateTransitionRecord,
)


class PostgresTransitionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_many(self, transitions: Sequence[StateTransitionRecord]) -> None:
        self._session.add_all(
            [
                StateTransitionRow(
                    id=item.transition_id,
                    aggregate_type=item.aggregate_type,
                    aggregate_id=item.aggregate_id,
                    from_status=item.from_status,
                    to_status=item.to_status,
                    actor_type=item.actor_type,
                    actor_id=item.actor_id,
                    reason_code=item.reason_code,
                    correlation_id=item.correlation_id,
                    occurred_at=item.occurred_at,
                )
                for item in transitions
            ]
        )

    def list_for_execution(
        self,
        execution_id: UUID,
        attempt_ids: Sequence[UUID],
        *,
        limit: int,
    ) -> list[StateTransitionRecord]:
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        predicates = [
            and_(
                StateTransitionRow.aggregate_type == "execution",
                StateTransitionRow.aggregate_id == execution_id,
            )
        ]
        if attempt_ids:
            predicates.append(
                and_(
                    StateTransitionRow.aggregate_type == "execution_attempt",
                    StateTransitionRow.aggregate_id.in_(attempt_ids),
                )
            )
        rows = self._session.scalars(
            select(StateTransitionRow)
            .where(or_(*predicates))
            .order_by(StateTransitionRow.occurred_at, StateTransitionRow.id)
            .limit(limit)
        ).all()
        return [_transition_record(row) for row in rows]
