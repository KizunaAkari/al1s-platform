"""Transactional attention projection for accepted reports and runtime failures."""

from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.lineup_models import LineupRecognitionRow as Record
from al1s.adapters.postgres.lineup_record_reads import refresh_attention
from al1s.adapters.postgres.scheduling_models import ExecutionAttemptRow as Attempt
from al1s.adapters.postgres.scheduling_models import ExecutionRow as Execution


def refresh_for_attempt(session: Session, attempt_id: UUID | None) -> None:
    records = list(
        session.scalars(
            select(Record)
            .join(
                Execution,
                or_(
                    Execution.occurrence_id == Record.occurrence_id,
                    and_(
                        Record.occurrence_id.is_(None), Execution.task_request_id == Record.task_id
                    ),
                ),
            )
            .join(Attempt, Attempt.execution_id == Execution.id)
            .where(Attempt.id == attempt_id)
            .with_for_update(of=Record)
        )
    )
    if records:
        refresh_attention(session, records)
