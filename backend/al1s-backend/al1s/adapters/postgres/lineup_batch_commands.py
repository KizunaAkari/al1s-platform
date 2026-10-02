"""One atomic parent task and bounded ordered image occurrences."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.lineup_models import LineupRecognitionRow as Record
from al1s.adapters.postgres.lineup_record_reads import refresh_attention
from al1s.adapters.postgres.lineup_workspace_models import LineupBatchRow as Batch
from al1s.adapters.postgres.lineup_workspace_models import LineupRequestRow as Receipt
from al1s.adapters.postgres.scheduling_models import PlanOccurrenceRow as Occurrence
from al1s.adapters.postgres.scheduling_models import TaskRequestRow as Task
from al1s.execution.errors import ConflictError, InvalidRequestError, NotFoundError
from al1s.lineup.catalog import MODEL_VERSION, catalog
from al1s.lineup.task_presentation import lineup_task_name


class LineupBatchCommands:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def create(
        self,
        ids: list[UUID],
        terminal_id: UUID,
        options: dict[str, Any],
        key: str,
        *,
        retry_source: UUID | None = None,
        validate_terminal: Callable[[], None] | None = None,
    ) -> UUID:
        digest = hashlib.sha256(
            json.dumps(
                dict(
                    ids=[str(i) for i in ids],
                    terminal=str(terminal_id),
                    options=options,
                    retry=str(retry_source),
                ),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self.sessions.begin() as session:
            lock = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], signed=True)
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
            previous = session.get(Receipt, key)
            if previous:
                if previous.request_hash != digest:
                    raise ConflictError("idempotency_key_reused", "提交标识已用于其他参数")
                return previous.task_id
            records = self._records(session, ids, retry_source)
            if isinstance(records, UUID):
                session.add(Receipt(key=key, request_hash=digest, task_id=records))
                return records
            if validate_terminal is not None:
                validate_terminal()
            if any(r.catalog_version != catalog()["version"] for r in records):
                raise ConflictError("lineup_catalog_changed", "资料版本变化, 请重新上传或核对")
            task_id = self._persist(
                session, records, terminal_id, options, key, digest, retry_source
            )
            session.add(Receipt(key=key, request_hash=digest, task_id=task_id))
            return task_id

    @staticmethod
    def _records(
        session: Session, ids: list[UUID], retry_source: UUID | None
    ) -> list[Record] | UUID:
        if retry_source:
            parent = session.scalar(
                select(Task)
                .where(Task.id == retry_source, Task.deleted_at.is_(None))
                .with_for_update()
            )
            if parent is None or parent.source_module not in {"lineup", "lineup_batch"}:
                raise NotFoundError("lineup_task")
            if parent.lifecycle_status == "active":
                raise ConflictError("lineup_task_active", "请等待当前任务全部结束再重试")
            active = session.scalar(
                select(Task.id)
                .join(Batch, Batch.task_id == Task.id)
                .where(
                    Batch.retry_source_task_id == retry_source, Task.lifecycle_status == "active"
                )
                .order_by(Task.created_at.desc())
                .limit(1)
            )
            if active:
                return active
            sources = list(
                session.scalars(
                    select(Record)
                    .outerjoin(Occurrence, Occurrence.id == Record.occurrence_id)
                    .where(Record.task_id == retry_source)
                    .order_by(Occurrence.ordinal.nulls_first(), Record.id)
                    .with_for_update(of=Record)
                )
            )
            refresh_attention(session, sources)
            return [
                Record(
                    id=uuid4(),
                    blob_id=r.blob_id,
                    name=r.name,
                    width=r.width,
                    height=r.height,
                    catalog_version=r.catalog_version,
                    created_at=datetime.now(UTC),
                    retry_source_record_id=r.id,
                    row_version=1,
                )
                for r in sources
                if r.needs_attention
            ]
        rows = list(
            session.scalars(
                select(Record).where(Record.id.in_(ids)).order_by(Record.id).with_for_update()
            )
        )
        if len(rows) != len(ids):
            raise NotFoundError("lineup")
        if any(r.task_id for r in rows):
            raise ConflictError("lineup_already_submitted", "所选图片已下发, 请打开原任务")
        by_id = {r.id: r for r in rows}
        return [by_id[i] for i in ids]

    @staticmethod
    def _persist(
        session: Session,
        records: list[Record],
        terminal_id: UUID,
        options: dict[str, Any],
        key: str,
        digest: str,
        retry_source: UUID | None,
    ) -> UUID:
        if not 1 <= len(records) <= 200:
            raise InvalidRequestError("lineup_retry_empty", "没有需要重试的失败或待核对图片")
        task_id, now = uuid4(), datetime.now(UTC)
        task = Task(
            id=task_id,
            idempotency_key=key,
            request_hash=digest,
            name=lineup_task_name(len(records)),
            task_type="batch",
            lifecycle_status="active",
            source_module="lineup_batch",
            logical_content_id=str(task_id),
            parameters=options,
            requested_terminal_id=terminal_id,
            timeout_seconds=180,
            max_retries=0,
            record_video=False,
            created_at=now,
            row_version=1,
        )
        session.add(task)
        session.flush()
        session.add(
            Batch(
                task_id=task_id,
                retry_source_task_id=retry_source,
                item_count=len(records),
                model_version=MODEL_VERSION,
                options=options,
            )
        )
        occurrences: list[dict[str, Any]] = [
            dict(
                id=uuid4(),
                task_request_id=task_id,
                ordinal=i + 1,
                scheduled_for=now,
                status="planned",
                created_at=now,
                row_version=1,
            )
            for i in range(len(records))
        ]
        session.execute(insert(Occurrence), occurrences)
        for record, occurrence in zip(records, occurrences, strict=True):
            record.task_id, record.occurrence_id, record.terminal_id = (
                task_id,
                occurrence["id"],
                terminal_id,
            )
            record.row_version += 1
        session.add_all(records)
        _record_creation(session, task_id, len(records), now)
        return task_id


def _record_creation(session: Session, task_id: UUID, count: int, now: datetime) -> None:
    from al1s.adapters.postgres.repositories import (
        PostgresAuditRepository,
        PostgresOutboxRepository,
    )
    from al1s.kernel.types import NewAuditEntry, NewOutboxEvent

    correlation = uuid4()
    payload = {"task_id": str(task_id), "task_type": "batch", "occurrence_count": count}
    PostgresOutboxRepository(session, owner_module="maa").add(
        NewOutboxEvent(
            event_id=uuid4(),
            event_type="execution.task_created.v1",
            schema_version=1,
            aggregate_type="task_request",
            aggregate_id=task_id,
            correlation_id=correlation,
            occurred_at=now,
            payload=payload,
        )
    )
    PostgresAuditRepository(session, owner_module="maa").add(
        NewAuditEntry(
            audit_id=uuid4(),
            actor_type="operator",
            actor_id=None,
            action="execution.task.create",
            target_type="task_request",
            target_id=task_id,
            correlation_id=correlation,
            details=payload,
            summary="lineup_batch_created",
        )
    )
