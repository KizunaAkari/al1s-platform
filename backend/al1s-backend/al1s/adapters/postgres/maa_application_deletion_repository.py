"""Fixed query count for classification previews and atomic cascade deletion."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Uuid, case, cast, func, or_, select, union_all, update
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql import Select

from al1s.adapters.postgres.maa_models import (
    MaaApplicationDeviceRow,
    MaaScriptReferenceRow,
    MaaScriptRow,
    MaaScriptVersionRow,
    MaaStrategyModuleRow,
    MaaStrategyRow,
)
from al1s.adapters.postgres.scheduling_models import TaskRequestRow


class PostgresMaaApplicationDeletionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def counts(self, application_id: UUID) -> tuple[int, int, int]:
        queries = [
            select(func.count())
            .select_from(model)
            .where(model.application_id == application_id, model.deleted_at.is_(None))
            .scalar_subquery()
            for model in (MaaScriptRow, MaaStrategyRow, MaaApplicationDeviceRow)
        ]
        row = self._session.execute(select(*queries)).one()
        return int(row[0]), int(row[1]), int(row[2])

    def active_tasks(self, application_id: UUID, limit: int = 51) -> list[UUID]:
        # Prefixes make the two task sets disjoint. Each joins content by UUID PK,
        # avoiding a materialized classification-wide text set behind LIMIT 51.
        tasks = union_all(
            self._content_tasks(application_id, "script", MaaScriptRow),
            self._content_tasks(application_id, "strategy", MaaStrategyRow),
        ).subquery()
        return list(self._session.scalars(select(tasks.c.id).order_by(tasks.c.id).limit(limit)))

    @staticmethod
    def _content_tasks(
        application_id: UUID,
        kind: str,
        model: type[MaaScriptRow] | type[MaaStrategyRow],
    ) -> Select[tuple[UUID]]:
        uuid_pattern = r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
        logical_id = TaskRequestRow.logical_content_id
        # CASE is necessary: SQL optimizers can reorder WHERE predicates. An old
        # malformed ID must never reach CAST, even if another filter excludes it.
        content_id = case(
            (
                logical_id.op("~")(f"^{kind}:{uuid_pattern}$"),
                cast(func.substr(logical_id, len(kind) + 2), Uuid(as_uuid=True)),
            ),
            else_=None,
        )
        return (
            select(TaskRequestRow.id)
            .join(model, model.id == content_id)
            .where(
                TaskRequestRow.source_module == "maa",
                TaskRequestRow.lifecycle_status == "active",
                TaskRequestRow.deleted_at.is_(None),
                model.application_id == application_id,
                model.deleted_at.is_(None),
            )
        )

    def lock_contents(self, application_id: UUID) -> None:
        for model in (MaaScriptRow, MaaStrategyRow):
            locked = (
                select(model.id)
                .where(model.application_id == application_id, model.deleted_at.is_(None))
                .order_by(model.id)
                .with_for_update()
                .subquery()
            )
            # Consume the locking subquery in PostgreSQL, without transferring all IDs.
            self._session.scalar(select(func.count()).select_from(locked))

    def has_external_references(self, application_id: UUID) -> bool:
        source, target = aliased(MaaScriptRow), aliased(MaaScriptRow)
        # Anchor on the indexed projection. Without this boundary PostgreSQL can
        # rescan the whole classification for every source/version when statistics
        # are stale. A classification with no references stops at an empty CTE.
        references = (
            select(MaaScriptReferenceRow.source_version_id)
            .join(target, target.id == MaaScriptReferenceRow.target_script_id)
            .where(target.application_id == application_id, target.deleted_at.is_(None))
            .cte("classification_references")
            .prefix_with("MATERIALIZED", dialect="postgresql")
        )
        strategy_versions = (
            select(MaaStrategyModuleRow.script_version_id)
            .join(
                MaaStrategyRow,
                MaaStrategyRow.current_version_id == MaaStrategyModuleRow.strategy_version_id,
            )
            .where(MaaStrategyRow.deleted_at.is_(None))
        )
        query = (
            select(source.id)
            .select_from(references)
            .join(
                MaaScriptVersionRow,
                MaaScriptVersionRow.id == references.c.source_version_id,
            )
            .join(source, source.id == MaaScriptVersionRow.script_id)
            .where(
                source.application_id != application_id,
                source.deleted_at.is_(None),
                or_(
                    references.c.source_version_id == source.current_version_id,
                    references.c.source_version_id == source.candidate_version_id,
                    references.c.source_version_id.in_(strategy_versions),
                ),
            )
            .limit(1)
        )
        return self._session.scalar(query) is not None

    def soft_delete_contents(self, application_id: UUID, now: datetime) -> None:
        for model in (MaaStrategyRow, MaaScriptRow):
            values: dict[str, object] = {
                "deleted_at": now,
                "updated_at": now,
                "row_version": model.row_version + 1,
            }
            if model is MaaStrategyRow:
                values["status"] = "retired"
            self._session.execute(
                update(model)
                .where(model.application_id == application_id, model.deleted_at.is_(None))
                .values(**values)
                .execution_options(synchronize_session=False)
            )
        self._session.execute(
            update(MaaApplicationDeviceRow)
            .where(
                MaaApplicationDeviceRow.application_id == application_id,
                MaaApplicationDeviceRow.deleted_at.is_(None),
            )
            .values(deleted_at=now)
            .execution_options(synchronize_session=False)
        )
