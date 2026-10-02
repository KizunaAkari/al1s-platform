"""Session-owned debug media; separate retention from formal task history."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, and_, select
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.artifact_models import TerminalArtifactRow as Artifact
from al1s.adapters.postgres.failure_review import PostgresFailureReview
from al1s.adapters.postgres.maa_models import MaaQuickTestSessionRow as TestSession
from al1s.adapters.postgres.models import BlobObjectRow as Blob
from al1s.execution.recording_download import RecordingFile, RecordingPage, RecordingSummary


class PostgresQuickTestMedia:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @staticmethod
    def _query(session_id: UUID, now: datetime) -> Select[tuple[Artifact, Blob]]:
        return (
            select(Artifact, Blob)
            .join(TestSession, Artifact.owner_id == TestSession.id)
            .outerjoin(Blob, Artifact.blob_id == Blob.id)
            .where(
                TestSession.id == session_id,
                Artifact.terminal_id == TestSession.terminal_id,
                Artifact.owner_kind == "quick_test",
                Artifact.artifact_kind == "screenshot",
                TestSession.status == "completed",
                TestSession.completed_at > now - timedelta(hours=24),
            )
        )

    def list_recordings(
        self, session_id: UUID, now: datetime, cursor: UUID | None, limit: int,
    ) -> RecordingPage:
        query = self._query(session_id, now).order_by(Artifact.id).limit(limit + 1)
        if cursor is not None:
            query = query.where(Artifact.id > cursor)
        with self._sessions() as session:
            rows = session.execute(query).all()
            items = tuple(
                RecordingSummary(
                    artifact.id, session_id, artifact.file_name, artifact.status,
                    bool(artifact.status == "ready" and blob and blob.status == "ready"),
                )
                for artifact, blob in rows[:limit]
            )
        return RecordingPage(items, items[-1].artifact_id if len(rows) > limit else None)

    def find_downloadable(
        self, session_id: UUID, artifact_id: UUID, now: datetime,
    ) -> RecordingFile | None:
        with self._sessions() as session:
            row = session.execute(self._query(session_id, now).where(
                Artifact.id == artifact_id, Artifact.status == "ready", Blob.status == "ready",
            )).one_or_none()
            if row is None:
                return None
            artifact, blob = row
            return RecordingFile(artifact.file_name, blob.sha256, blob.size_bytes, blob.object_key)

    def expire(self, now: datetime) -> int:
        with self._sessions.begin() as session:
            rows = session.scalars(
                select(Artifact).join(TestSession, and_(
                    Artifact.owner_id == TestSession.id,
                    Artifact.terminal_id == TestSession.terminal_id,
                )).where(
                    Artifact.owner_kind == "quick_test",
                    Artifact.status != "expired",
                    TestSession.completed_at <= now - timedelta(hours=24),
                ).order_by(Artifact.id).limit(100)
                .with_for_update(of=Artifact, skip_locked=True)
            ).all()
            PostgresFailureReview._revoke_artifacts(session, rows, now)
            return len(rows)
