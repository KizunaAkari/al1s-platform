"""Task-owned diagnostic reads and failure review transactions; no network I/O."""

from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.artifact_models import TerminalArtifactRow as Artifact
from al1s.adapters.postgres.capture_projection import expired_capture_references
from al1s.adapters.postgres.delivery_models import TerminalReportRow as Report
from al1s.adapters.postgres.lineup_models import LineupRecognitionRow as Lineup
from al1s.adapters.postgres.models import BlobObjectRow as Blob
from al1s.adapters.postgres.models import GcJobRow
from al1s.adapters.postgres.scheduling_models import ExecutionAttemptRow as Attempt
from al1s.adapters.postgres.scheduling_models import ExecutionRow as Execution
from al1s.adapters.postgres.scheduling_models import TaskRequestRow as Task
from al1s.execution.errors import ConflictError, NotFoundError
from al1s.execution.failure_details import (
    definitely_no_screenshot,
    failure_details,
    screenshot_ids,
)
from al1s.execution.recording_download import RecordingFile
from al1s.execution.task_captures import task_captures


def _attempt(task_id: UUID, attempt_id: UUID) -> Select[tuple[Attempt]]:
    return (
        select(Attempt)
        .join(Execution, Attempt.execution_id == Execution.id)
        .join(Task, Execution.task_request_id == Task.id)
        .where(Task.id == task_id, Task.deleted_at.is_(None), Attempt.id == attempt_id)
    )


def _report(attempt_id: UUID) -> Select[tuple[Report]]:
    return (
        select(Report)
        .where(
            Report.attempt_id == attempt_id,
            Report.report_kind == "attempt_result",
            Report.disposition == "accepted",
        )
        .order_by(Report.received_at.desc(), Report.report_id)
        .limit(1)
    )


class PostgresFailureReview:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def list_attempts(
        self,
        task_id: UUID,
        cursor: UUID | None,
        now: datetime,
    ) -> dict[str, Any]:
        query = (
            select(Attempt, Lineup.id, Task.source_module)
            .select_from(Task)
            .outerjoin(Execution, Execution.task_request_id == Task.id)
            .outerjoin(Attempt, Attempt.execution_id == Execution.id)
            .outerjoin(Lineup, (Lineup.task_id == Task.id) & (Task.source_module == "lineup"))
            .where(Task.id == task_id, Task.deleted_at.is_(None))
            .order_by(Attempt.id)
            .limit(21)
        )
        if cursor:
            query = query.where(Attempt.id > cursor)
        with self._sessions() as session:
            rows = session.execute(query).all()
            attempts = [row[0] for row in rows if row[0] is not None]
            source_module = rows[0][2] if rows else None
            lineup_record_id = next((row[1] for row in rows if row[1] is not None), None)
            ids = [item.id for item in attempts[:20]]
            reports = (
                session.scalars(
                    select(Report)
                    .where(
                        Report.attempt_id.in_(ids),
                        Report.report_kind == "attempt_result",
                        Report.disposition == "accepted",
                    )
                    .distinct(Report.attempt_id)
                    .order_by(
                        Report.attempt_id,
                        Report.received_at.desc(),
                        Report.report_id,
                    )
                ).all()
                if ids
                else []
            )
            by_id = {item.attempt_id: item for item in reports}
            expired_images = expired_capture_references(session, reports)
            items = []
            for attempt in attempts[:20]:
                report = by_id.get(attempt.id)
                expired = bool(attempt.ended_at and attempt.ended_at <= now - timedelta(days=30))
                diagnostic = (
                    report.result_diagnostic
                    if report and not expired and not (report.detail_confirmed_at)
                    else None
                )
                unavailable = expired_images.get(attempt.id, set())
                details = [
                    replace(item, screenshot_id=None, screenshot_error="截图上传已失效或原图已过期")
                    if item.screenshot_id in unavailable
                    else item
                    for item in failure_details(diagnostic)
                ]
                items.append(
                    {
                        "attempt_id": attempt.id,
                        "execution_id": attempt.execution_id,
                        "attempt_no": attempt.attempt_no,
                        "status": attempt.status,
                        "result": attempt.result,
                        "error_code": attempt.error_code,
                        "failure_phase": attempt.failure_phase,
                        "confirmed": bool(report and report.detail_confirmed_at),
                        "expired": expired,
                        "details": [asdict(item) for item in details],
                        "screenshots": [
                            asdict(item)
                            for item in task_captures(diagnostic)
                            if item.artifact_id not in unavailable
                        ],
                        "no_screenshot": bool(details)
                        and all(
                            item.screenshot_id is None and bool(item.screenshot_error)
                            for item in details
                        ),
                    }
                )
        return {
            "items": items,
            "next_cursor": attempts[19].id if len(attempts) > 20 else None,
            "lineup_record_id": lineup_record_id,
            "source_module": source_module,
        }

    def _load(
        self,
        session: Session,
        task: UUID,
        attempt: UUID,
        now: datetime,
        *,
        failure_only: bool = True,
    ) -> Report:
        owner = session.scalar(_attempt(task, attempt))
        if owner is None:
            raise NotFoundError("task_attempt")
        report = session.scalar(_report(attempt).with_for_update())
        if report is None or (failure_only and owner.result != "failure"):
            raise NotFoundError("failure_detail")
        if owner.ended_at is None or owner.ended_at <= now - timedelta(days=30):
            raise NotFoundError("failure_detail_expired")
        return report

    def metadata(
        self,
        task: UUID,
        attempt: UUID,
        artifact: UUID,
        now: datetime,
    ) -> RecordingFile:
        with self._sessions() as session:
            report = self._load(session, task, attempt, now, failure_only=False)
            if report.detail_confirmed_at or artifact not in {
                item.artifact_id for item in task_captures(report.result_diagnostic)
            }:
                raise NotFoundError("failure_screenshot")
            row = session.execute(
                select(Artifact.file_name, Blob.sha256, Blob.size_bytes, Blob.object_key)
                .join(Blob, Artifact.blob_id == Blob.id)
                .where(
                    or_(Artifact.id == artifact, Artifact.idempotency_key == str(artifact)),
                    Artifact.terminal_id == report.terminal_id,
                    Artifact.owner_id == attempt,
                    Artifact.owner_kind == "formal_attempt",
                    Artifact.artifact_kind == "screenshot",
                    Artifact.status == "ready",
                    Blob.status == "ready",
                )
            ).one_or_none()
            if row is None:
                raise NotFoundError("failure_screenshot_not_ready")
            return RecordingFile(row.file_name, row.sha256, row.size_bytes, row.object_key)

    def downloaded(self, task: UUID, attempt: UUID, artifact: UUID, now: datetime) -> None:
        with self._sessions.begin() as session:
            report = self._load(session, task, attempt, now, failure_only=False)
            if report.detail_confirmed_at or artifact not in {
                item.artifact_id for item in task_captures(report.result_diagnostic)
            }:
                raise NotFoundError("failure_screenshot")
            session.execute(
                update(Artifact)
                .where(
                    or_(Artifact.id == artifact, Artifact.idempotency_key == str(artifact)),
                    Artifact.terminal_id == report.terminal_id,
                    Artifact.artifact_kind == "screenshot",
                    Artifact.owner_id == attempt,
                    Artifact.owner_kind == "formal_attempt",
                    Artifact.status == "ready",
                )
                .values(downloaded_at=now)
            )

    def confirm(self, task: UUID, attempt: UUID, now: datetime) -> None:
        with self._sessions.begin() as session:
            if session.scalar(select(Lineup.id).where(Lineup.task_id == task).limit(1)):
                raise ConflictError(
                    "lineup_evidence_retained", "阵容原始结果保留, 请使用纠错与标注处理"
                )
            report = self._load(session, task, attempt, now)
            if report.detail_confirmed_at:
                return
            ids = screenshot_ids(report.result_diagnostic)
            all_ids = {item.artifact_id for item in task_captures(report.result_diagnostic)}
            if any(
                not item.screenshot_id and not item.screenshot_error
                for item in failure_details(report.result_diagnostic)
            ):
                raise ConflictError("screenshot_state_unknown", "Screenshot state is not confirmed")
            artifacts = (
                session.scalars(
                    select(Artifact)
                    .where(
                        or_(
                            Artifact.id.in_(all_ids),
                            Artifact.idempotency_key.in_([str(i) for i in all_ids]),
                        ),
                        Artifact.terminal_id == report.terminal_id,
                        Artifact.owner_id == attempt,
                        Artifact.owner_kind == "formal_attempt",
                        Artifact.artifact_kind == "screenshot",
                    )
                    .with_for_update()
                ).all()
                if all_ids
                else []
            )
            required = [
                item
                for item in artifacts
                if item.id in ids or item.idempotency_key in {str(i) for i in ids}
            ]
            if ids and (
                len(required) != len(ids)
                or any(
                    item.status != "expired"
                    and (item.status != "ready" or item.downloaded_at is None)
                    for item in required
                )
            ):
                raise ConflictError(
                    "screenshot_download_required", "Download all failure screenshots first"
                )
            if not ids and not definitely_no_screenshot(report.result_diagnostic):
                raise ConflictError("screenshot_state_unknown", "Screenshot state is not confirmed")
            self._revoke(session, report, artifacts, now)

    def expire(self, now: datetime) -> int:
        """Bounded retention sweep; expired/deleted details never imply user confirmation."""
        with self._sessions.begin() as session:
            reports = session.scalars(
                select(Report)
                .join(Attempt, Report.attempt_id == Attempt.id)
                .join(Execution, Attempt.execution_id == Execution.id)
                .join(Task, Execution.task_request_id == Task.id)
                .where(
                    Report.result_diagnostic.is_not(None),
                    Report.result_diagnostic != JSONB.NULL,
                    Report.report_kind == "attempt_result",
                    (Attempt.ended_at <= now - timedelta(days=30)) | Task.deleted_at.is_not(None),
                    ~select(Lineup.id).where(Lineup.task_id == Task.id).exists(),
                )
                .order_by(Report.received_at)
                .limit(20)
                .with_for_update(
                    of=Report,
                    skip_locked=True,
                )
            ).all()
            allowed = {
                report.attempt_id: {
                    item.artifact_id for item in task_captures(report.result_diagnostic)
                }
                for report in reports
            }
            artifact_ids = set().union(*allowed.values())
            artifacts = (
                session.scalars(
                    select(Artifact)
                    .where(
                        or_(
                            Artifact.id.in_(artifact_ids),
                            Artifact.idempotency_key.in_([str(i) for i in artifact_ids]),
                        ),
                        Artifact.owner_id.in_([r.attempt_id for r in reports]),
                        Artifact.owner_kind == "formal_attempt",
                        Artifact.artifact_kind == "screenshot",
                    )
                    .with_for_update()
                ).all()
                if artifact_ids
                else []
            )
            owners = {report.attempt_id: report.terminal_id for report in reports}
            valid = [
                item
                for item in artifacts
                if item.terminal_id == owners.get(item.owner_id)
                and (
                    item.id in allowed.get(item.owner_id, set())
                    or item.idempotency_key in {str(i) for i in allowed.get(item.owner_id, set())}
                )
            ]
            for report in reports:
                report.result_diagnostic = None
            self._revoke_artifacts(session, valid, now)
            return len(reports)

    @staticmethod
    def _revoke(
        session: Session,
        report: Report,
        artifacts: Sequence[Artifact],
        now: datetime,
        *,
        confirmed: bool = True,
    ) -> None:
        report.result_diagnostic = None
        if confirmed:
            report.detail_confirmed_at = now
        PostgresFailureReview._revoke_artifacts(session, artifacts, now)

    @staticmethod
    def _revoke_artifacts(session: Session, artifacts: Sequence[Artifact], now: datetime) -> None:
        blob_ids = {item.blob_id for item in artifacts if item.blob_id}
        for item in artifacts:
            item.status, item.blob_id, item.completed_at = "expired", None, now
            item.staging_cleanup_at = max(item.expires_at, now) + timedelta(hours=2)
            item.row_version += 1
        session.flush()
        if blob_ids:
            session.execute(
                insert(GcJobRow)
                .values(
                    [
                        {"blob_id": blob_id, "status": "pending", "available_at": now}
                        for blob_id in blob_ids
                    ]
                )
                .on_conflict_do_nothing()
            )
