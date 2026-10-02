"""Persist-time Maa identity protection shared with cascade deletion row locks."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.maa_models import MaaScriptRow, MaaStrategyRow
from al1s.execution.errors import ConflictError


def lock_active_maa_task_content(session: Session, logical_content_id: str) -> None:
    kind, _, identifier = logical_content_id.partition(":")
    try:
        content_id = UUID(identifier)
    except ValueError as exc:
        raise ConflictError("task_content_changed", "任务内容已失效, 请刷新后重新选择") from exc
    if kind not in {"script", "strategy"}:
        raise ConflictError("task_content_changed", "任务内容已失效, 请刷新后重新选择")
    model = MaaScriptRow if kind == "script" else MaaStrategyRow
    current = session.scalar(
        select(model.id)
        .where(
            model.id == content_id,
            model.deleted_at.is_(None),
        )
        .with_for_update(read=True)
    )
    if current is None:
        raise ConflictError("task_content_changed", "任务内容已失效, 请刷新后重新选择")
