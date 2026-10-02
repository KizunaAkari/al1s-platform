"""Bounded, indexed reverse-dependency queries for script deletion."""

from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.maa_models import (
    MaaScriptReferenceRow,
    MaaScriptRow,
    MaaScriptVersionRow,
    MaaStrategyModuleRow,
    MaaStrategyRow,
)

# Projection maintained by migration triggers, not an independently mutable aggregate.
references = MaaScriptReferenceRow.__table__


def script_referrers(
    session: Session, target_id: UUID, *, after_id: UUID | None, limit: int
) -> list[dict[str, object]]:
    r = references.c
    in_strategy = (
        select(MaaStrategyModuleRow.script_version_id)
        .join(
            MaaStrategyRow,
            MaaStrategyRow.current_version_id == MaaStrategyModuleRow.strategy_version_id,
        )
        .where(MaaStrategyRow.deleted_at.is_(None))
    )
    query = (
        select(
            MaaScriptRow.id,
            MaaScriptRow.name,
            func.array_agg(r.step_index.distinct()).label("steps"),
        )
        .join(
            MaaScriptVersionRow,
            MaaScriptVersionRow.script_id == MaaScriptRow.id,
        )
        .join(references, r.source_version_id == MaaScriptVersionRow.id)
        .where(
            or_(
                r.source_version_id == MaaScriptRow.current_version_id,
                r.source_version_id == MaaScriptRow.candidate_version_id,
                r.source_version_id.in_(in_strategy),
            ),
        )
        .where(r.target_script_id == target_id, MaaScriptRow.deleted_at.is_(None))
    )
    if after_id is not None:
        query = query.where(MaaScriptRow.id > after_id)
    rows = session.execute(
        query.group_by(MaaScriptRow.id, MaaScriptRow.name).order_by(MaaScriptRow.id).limit(limit)
    ).all()
    return [
        {"script_id": str(row.id), "name": row.name, "step_indices": sorted(row.steps)}
        for row in rows
    ]


def strategy_referrers(session: Session, target_id: UUID) -> bool:
    query = (
        select(MaaStrategyRow.id)
        .join(
            MaaStrategyModuleRow,
            MaaStrategyModuleRow.strategy_version_id == MaaStrategyRow.current_version_id,
        )
        .join(MaaScriptVersionRow, MaaScriptVersionRow.id == MaaStrategyModuleRow.script_version_id)
        .where(
            MaaStrategyRow.deleted_at.is_(None),
            MaaScriptVersionRow.script_id == target_id,
        )
        .limit(1)
    )
    return session.execute(query).first() is not None
