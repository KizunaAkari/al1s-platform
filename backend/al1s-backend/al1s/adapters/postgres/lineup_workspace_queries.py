"""Task pages aggregate in PostgreSQL; image details are read in bounded batches."""

from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.lineup_models import LineupRecognitionRow as Record
from al1s.adapters.postgres.lineup_record_reads import enrich_records
from al1s.adapters.postgres.lineup_repository import _dict
from al1s.adapters.postgres.lineup_workspace_models import LineupAnnotationRow as Annotation
from al1s.adapters.postgres.lineup_workspace_models import LineupBatchRow as Batch
from al1s.adapters.postgres.scheduling_models import ExecutionRow as Execution
from al1s.adapters.postgres.scheduling_models import PlanOccurrenceRow as Occurrence
from al1s.adapters.postgres.scheduling_models import TaskRequestRow as Task
from al1s.execution.errors import InvalidRequestError, NotFoundError
from al1s.lineup.task_presentation import lineup_task_name


def _execution_join() -> Any:
    return or_(
        Execution.occurrence_id == Record.occurrence_id,
        and_(Record.occurrence_id.is_(None), Execution.task_request_id == Record.task_id),
    )


def _usable() -> Any:
    return and_(
        Record.needs_attention.is_(False),
        or_(
            Execution.result == "success",
            exists(
                select(Annotation.record_id).where(
                    Annotation.record_id == Record.id, Annotation.state == "confirmed"
                )
            ),
        ),
    )


def _summary_query() -> Any:
    finished = or_(
        Execution.status.in_(("ended", "cancelled", "timed_out")),
        Occurrence.status == "cancelled",
        Task.lifecycle_status == "cancelled",
    )
    return (
        select(
            Task,
            Batch.retry_source_task_id,
            func.count(Record.id),
            func.count().filter(finished),
            func.count().filter(_usable()),
            func.count().filter(Record.needs_attention),
            func.count().filter(
                or_(Execution.result == "failure", Execution.status == "timed_out")
            ),
            func.count().filter(Execution.status == "running"),
        )
        .join(Record, Record.task_id == Task.id)
        .outerjoin(Batch, Batch.task_id == Task.id)
        .outerjoin(Occurrence, Occurrence.id == Record.occurrence_id)
        .outerjoin(Execution, _execution_join())
        .where(Task.deleted_at.is_(None), Task.source_module.in_(("lineup", "lineup_batch")))
        .group_by(Task.id, Batch.task_id)
    )


def _summary(row: Any) -> dict[str, Any]:
    task, source, total, finished, usable, attention, failure, running = row
    return dict(
        task_id=task.id,
        name=lineup_task_name(total),
        lifecycle_status=task.lifecycle_status,
        created_at=task.created_at,
        row_version=task.row_version,
        retry_source_task_id=source,
        terminal_id=task.requested_terminal_id,
        options=task.parameters,
        summary=dict(
            total=total,
            finished=finished,
            usable=usable,
            needs_attention=attention,
            failure=failure,
            running=running,
        ),
    )


class LineupWorkspaceQueries:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def task(self, task_id: UUID, *, after: int = 0, category: str = "all") -> dict[str, Any]:
        filters = {
            "all": True,
            "usable": _usable(),
            "attention": Record.needs_attention,
            "failure": or_(Execution.result == "failure", Execution.status == "timed_out"),
        }
        if category not in filters or not 0 <= after <= 200:
            raise InvalidRequestError("lineup_page_invalid", "图片筛选或分页参数无效")
        with self.sessions() as session:
            row = session.execute(_summary_query().where(Task.id == task_id)).first()
            if row is None:
                raise NotFoundError("lineup_task")
            result = _summary(row)
            ordinal = func.coalesce(Occurrence.ordinal, 1)
            rows = session.execute(
                select(Record, ordinal)
                .outerjoin(Occurrence, Occurrence.id == Record.occurrence_id)
                .outerjoin(Execution, _execution_join())
                .where(Record.task_id == task_id, ordinal > after, filters[category])
                .order_by(ordinal, Record.id)
                .limit(21)
            ).all()
            details = enrich_records(session, [_dict(record) for record, _ in rows[:20]])
            for detail, (_, order) in zip(details, rows[:20], strict=True):
                detail["ordinal"] = order
            result.update(items=details, next_cursor=rows[19][1] if len(rows) > 20 else None)
            return result

    def annotation_tasks(self, before: UUID | None = None) -> dict[str, Any]:
        with self.sessions() as session:
            statement = _summary_query().having(func.count().filter(Record.needs_attention) > 0)
            if before:
                # UUID cursor plus its timestamp are read as one scalar subquery.
                stamp = select(Task.created_at).where(Task.id == before).scalar_subquery()
                statement = statement.where(
                    or_(Task.created_at < stamp, and_(Task.created_at == stamp, Task.id < before))
                )
            rows = session.execute(
                statement.order_by(Task.created_at.desc(), Task.id.desc()).limit(21)
            ).all()
            return dict(
                items=[_summary(r) for r in rows[:20]],
                next_cursor=rows[19][0].id if len(rows) > 20 else None,
            )
