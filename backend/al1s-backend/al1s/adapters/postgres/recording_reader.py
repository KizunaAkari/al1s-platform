from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, and_, select
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.artifact_models import TerminalArtifactRow as Artifact
from al1s.adapters.postgres.models import BlobObjectRow as Blob
from al1s.adapters.postgres.scheduling_models import (
    ExecutionAttemptRow as Attempt,
)
from al1s.adapters.postgres.scheduling_models import (
    ExecutionRow as Execution,
)
from al1s.adapters.postgres.scheduling_models import (
    TaskRequestRow as Task,
)
from al1s.execution.recording_download import RecordingFile, RecordingPage, RecordingSummary


def downloadable_recording_query(
    task_id: UUID, artifact_id: UUID, now: datetime
) -> Select[tuple[str, str, int, str]]:
    return (
        select(Artifact.file_name, Blob.sha256, Blob.size_bytes, Blob.object_key)
        .join(Attempt, Artifact.owner_id == Attempt.id)
        .join(Execution, Attempt.execution_id == Execution.id)
        .join(Task, Execution.task_request_id == Task.id)
        .join(Blob, Artifact.blob_id == Blob.id)
        .where(
            Task.id == task_id,
            Task.deleted_at.is_(None),
            Task.record_video.is_(True),
            Artifact.id == artifact_id,
            Artifact.owner_kind == "formal_attempt",
            Artifact.artifact_kind == "video",
            Artifact.status == "ready",
            Blob.status == "ready",
            Blob.media_type == "video/mp4",
            Attempt.status.in_(("ended", "cancelled", "timed_out")),
            Attempt.ended_at.is_not(None),
            Attempt.ended_at > now - timedelta(days=30),
        )
        .limit(1)
    )


class PostgresRecordingReader:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def list_recordings(
        self, task_id: UUID, now: datetime, cursor: UUID | None, limit: int
    ) -> RecordingPage:
        downloadable = and_(
            Artifact.status == "ready",
            Blob.status == "ready",
            Blob.media_type == "video/mp4",
            Attempt.status.in_(("ended", "cancelled", "timed_out")),
            Attempt.ended_at > now - timedelta(days=30),
        )
        query = (
            select(
                Artifact.id,
                Artifact.owner_id,
                Artifact.file_name,
                Artifact.status,
                downloadable.label("downloadable"),
            )
            .join(Attempt, Artifact.owner_id == Attempt.id)
            .join(Execution, Attempt.execution_id == Execution.id)
            .join(Task, Execution.task_request_id == Task.id)
            .outerjoin(Blob, Artifact.blob_id == Blob.id)
            .where(
                Task.id == task_id,
                Task.deleted_at.is_(None),
                Task.record_video.is_(True),
                Artifact.owner_kind == "formal_attempt",
                Artifact.artifact_kind == "video",
            )
            .order_by(Artifact.id)
            .limit(limit + 1)
        )
        if cursor is not None:
            query = query.where(Artifact.id > cursor)
        with self._sessions() as session:
            rows = session.execute(query).all()
        items = tuple(
            RecordingSummary(
                row.id, row.owner_id, row.file_name, row.status, bool(row.downloadable)
            )
            for row in rows[:limit]
        )
        return RecordingPage(items, items[-1].artifact_id if len(rows) > limit else None)

    def find_downloadable(
        self, task_id: UUID, artifact_id: UUID, now: datetime
    ) -> RecordingFile | None:
        with self._sessions() as session:
            row = session.execute(
                downloadable_recording_query(task_id, artifact_id, now)
            ).one_or_none()
        if row is None:
            return None
        return RecordingFile(row.file_name, row.sha256, row.size_bytes, row.object_key)
