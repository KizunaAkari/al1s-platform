"""Task-level labels, distinct from the latest per-image execution."""

from al1s.execution.scheduling_types import (
    BatchHistorySummary,
    ExecutionResult,
    ExecutionStatus,
)


def lineup_task_name(image_count: int) -> str:
    return f"阵容识别 · {image_count} 张"


def batch_history_summary(
    lifecycle: str, total: int, success: int, running: int, queued: int
) -> BatchHistorySummary:
    result = None
    if lifecycle == "active":
        status = (
            ExecutionStatus.RUNNING
            if running
            else ExecutionStatus.QUEUED
            if queued
            else ExecutionStatus.WAITING
        )
    elif lifecycle == "cancelled":
        status = ExecutionStatus.CANCELLED
    else:
        status = ExecutionStatus.ENDED
        result = (
            ExecutionResult.SUCCESS if total > 0 and success == total else ExecutionResult.FAILURE
        )
    return BatchHistorySummary(total, status, result)
