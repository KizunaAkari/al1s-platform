"""Bounded lineup reads; raw inference remains in accepted terminal reports."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.execution_models import TerminalCapabilityProfileRow as Profile
from al1s.adapters.postgres.execution_models import TerminalRow as Terminal
from al1s.adapters.postgres.lineup_models import LineupRecognitionRow as Record
from al1s.execution.errors import ConflictError, NotFoundError
from al1s.lineup.catalog import CAPABILITY


def _dict(row: Record) -> dict[str, Any]:
    return {
        name: getattr(row, name)
        for name in (
            "id",
            "blob_id",
            "terminal_id",
            "task_id",
            "occurrence_id",
            "retry_source_record_id",
            "name",
            "width",
            "height",
            "catalog_version",
            "created_at",
            "review",
            "row_version",
        )
    }


class LineupRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def create(self, **values: Any) -> UUID:
        with self.sessions.begin() as session:
            row = Record(**values, created_at=datetime.now(UTC), row_version=1)
            session.add(row)
            return row.id

    def get(self, record_id: UUID) -> dict[str, Any]:
        with self.sessions() as session:
            row = session.get(Record, record_id)
            if row is None:
                raise NotFoundError("lineup")
            return _dict(row)

    def page(self, cursor: UUID | None) -> list[dict[str, Any]]:
        query = select(Record).order_by(Record.created_at.desc(), Record.id.desc()).limit(21)
        with self.sessions() as session:
            if cursor:
                anchor = session.get(Record, cursor)
                if anchor is None:
                    raise NotFoundError("lineup_cursor")
                query = query.where(
                    tuple_(Record.created_at, Record.id) < (anchor.created_at, anchor.id)
                )
            return [_dict(row) for row in session.scalars(query)]

    def get_many(self, record_ids: list[UUID]) -> list[dict[str, Any]]:
        if not record_ids:
            return []
        if len(record_ids) > 100:
            raise ValueError("At most 100 lineup records may be read together")
        with self.sessions() as session:
            rows = {
                row.id: _dict(row)
                for row in session.scalars(select(Record).where(Record.id.in_(record_ids)))
            }
        return [rows[key] for key in dict.fromkeys(record_ids) if key in rows]

    def occurrence_records(self, ids: list[UUID]) -> list[dict[str, Any]]:
        from al1s.adapters.postgres.lineup_workspace_models import LineupBatchRow

        with self.sessions() as session:
            rows = session.execute(
                select(Record, LineupBatchRow)
                .join(LineupBatchRow, LineupBatchRow.task_id == Record.task_id)
                .where(Record.occurrence_id.in_(ids))
            )
            return [
                {**_dict(record), "options": batch.options, "model_version": batch.model_version}
                for record, batch in rows
            ]

    def attach_task(self, record_id: UUID, task_id: UUID, terminal_id: UUID) -> None:
        with self.sessions.begin() as session:
            row = session.scalar(select(Record).where(Record.id == record_id).with_for_update())
            if row is None:
                raise NotFoundError("lineup")
            if row.task_id not in (None, task_id):
                raise ConflictError("lineup_already_submitted", "此图片已经提交识别")
            if row.task_id is None:
                row.task_id, row.terminal_id = task_id, terminal_id
                row.row_version += 1

    def save_review(self, record_id: UUID, version: int, ids: list[int | None]) -> None:
        with self.sessions.begin() as session:
            row = session.scalar(select(Record).where(Record.id == record_id).with_for_update())
            if row is None:
                raise NotFoundError("lineup")
            if row.row_version != version:
                raise ConflictError("lineup_version_conflict", "记录已变化。请刷新后重新核对")
            row.review = ids
            row.row_version += 1
            from al1s.adapters.postgres.lineup_record_reads import refresh_attention

            refresh_attention(session, [row])

    def enrich(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from al1s.adapters.postgres.lineup_record_reads import enrich_records

        with self.sessions() as session:
            return enrich_records(session, records)

    def terminals(self) -> list[dict[str, Any]]:
        query = (
            select(Terminal, Profile)
            .outerjoin(Profile, Profile.id == Terminal.current_capability_profile_id)
            .where(Terminal.deleted_at.is_(None), Terminal.terminal_type == "linux")
            .order_by(Terminal.display_name, Terminal.id)
            .limit(100)
        )
        with self.sessions() as session:
            result = []
            for terminal, profile in session.execute(query):
                reason = None
                if terminal.service_status != "online":
                    reason = "终端离线"
                elif terminal.acceptance_status != "accepting":
                    reason = "终端暂停接收任务"
                elif profile is None or CAPABILITY not in profile.provider_keys:
                    reason = "终端尚未安装阵容识别模型与资料"
                result.append(
                    dict(
                        terminal_id=terminal.id,
                        display_name=terminal.display_name,
                        available=reason is None,
                        reason=reason,
                    )
                )
            return result
