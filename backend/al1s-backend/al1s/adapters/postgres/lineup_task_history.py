"""One page-scoped aggregate query; never read individual reports for task badges."""

from dataclasses import replace

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.lineup_workspace_models import LineupBatchRow as Batch
from al1s.adapters.postgres.scheduling_models import ExecutionRow as Execution
from al1s.execution.scheduling_types import TaskHistorySummary, TaskType
from al1s.lineup.task_presentation import batch_history_summary, lineup_task_name


def project_lineup_history(
    session: Session, items: list[TaskHistorySummary]
) -> list[TaskHistorySummary]:
    ids = [item.task_id for item in items if item.task_type is TaskType.BATCH]
    counts = {}
    if ids:
        rows = session.execute(
            select(
                Batch.task_id,
                Batch.item_count,
                func.count(Execution.id).filter(
                    and_(Execution.status == "ended", Execution.result == "success")
                ),
                func.count(Execution.id).filter(Execution.status == "running"),
                func.count(Execution.id).filter(Execution.status == "queued"),
            )
            .outerjoin(Execution, Execution.task_request_id == Batch.task_id)
            .where(Batch.task_id.in_(ids))
            .group_by(Batch.task_id)
        ).all()
        counts = {row[0]: row[1:] for row in rows}
    result = []
    for item in items:
        if item.task_id in counts:
            total, success, running, queued = counts[item.task_id]
            item = replace(
                item,
                name=lineup_task_name(total),
                batch_summary=batch_history_summary(
                    item.lifecycle_status.value, total, success, running, queued
                ),
            )
        elif item.source_module == "lineup":
            item = replace(item, name=lineup_task_name(1))
        result.append(item)
    return result
