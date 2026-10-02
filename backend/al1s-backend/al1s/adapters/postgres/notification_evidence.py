from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.artifact_models import TerminalArtifactRow as Artifact
from al1s.adapters.postgres.delivery_models import TerminalReportRow as Report
from al1s.adapters.postgres.models import BlobObjectRow as Blob
from al1s.adapters.postgres.scheduling_models import ExecutionAttemptRow as Attempt
from al1s.adapters.postgres.scheduling_models import ExecutionRow as Execution
from al1s.adapters.postgres.scheduling_models import TaskRequestRow as Task
from al1s.execution.notification_evidence import NotificationEvidence


class PostgresNotificationEvidence:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions, self._now = sessions, now

    def read(
        self, task: UUID, execution: UUID, attempt: UUID, terminal: UUID, capture: UUID
    ) -> NotificationEvidence:
        with self._sessions() as session:
            valid = session.scalar(
                select(Report.report_id)
                .join(Attempt, Report.attempt_id == Attempt.id)
                .join(Execution, Attempt.execution_id == Execution.id)
                .join(Task, Execution.task_request_id == Task.id)
                .where(
                    Task.id == task,
                    Task.deleted_at.is_(None),
                    Execution.id == execution,
                    Attempt.id == attempt,
                    Attempt.ended_at > self._now() - timedelta(days=30),
                    Report.terminal_id == terminal,
                    Report.report_kind == "attempt_result",
                    Report.disposition == "accepted",
                    Report.detail_confirmed_at.is_(None),
                )
                .limit(1)
            )
            if valid is None:
                return NotificationEvidence("unavailable")
            row = session.execute(
                select(
                    Artifact.status,
                    Artifact.expires_at,
                    Blob.object_key,
                    Blob.size_bytes,
                    Blob.sha256,
                    Blob.status.label("blob_status"),
                    Blob.media_type,
                )
                .outerjoin(Blob, Artifact.blob_id == Blob.id)
                .where(
                    Artifact.terminal_id == terminal,
                    Artifact.owner_id == attempt,
                    Artifact.owner_kind == "formal_attempt",
                    Artifact.artifact_kind == "screenshot",
                    or_(Artifact.id == capture, Artifact.idempotency_key == str(capture)),
                )
                .limit(1)
            ).one_or_none()
            if row is None:
                return NotificationEvidence("pending")
            if row.status == "pending":
                return NotificationEvidence(
                    "pending" if row.expires_at > self._now() else "unavailable"
                )
            if row.status != "ready" or row.blob_status != "ready" or row.media_type != "image/png":
                return NotificationEvidence("unavailable")
            return NotificationEvidence("ready", row.object_key, row.size_bytes, row.sha256)
