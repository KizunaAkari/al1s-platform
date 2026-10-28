from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.component_models import ComponentReleaseRow as Row
from al1s.adapters.postgres.component_records import release_record
from al1s.adapters.postgres.models import BlobObjectRow
from al1s.adapters.postgres.release_models import LinuxReleaseRow
from al1s.adapters.postgres.release_repository import retire
from al1s.adapters.postgres.repositories import PostgresBlobCatalogRepository
from al1s.components.contracts import ComponentRelease
from al1s.components.types import ComponentVerificationJob
from al1s.execution.errors import ConflictError, NotFoundError
from al1s.kernel.types import NewBlob


class ComponentReleaseRepository:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def enqueue_release(self, identity: UUID, expected: int) -> ComponentRelease:
        with self.sessions.begin() as session:
            row = session.get(Row, identity, with_for_update=True)
            if row is None:
                raise NotFoundError("component_release")
            if row.state in {"queued", "verifying", "published"}:
                return release_record(row)
            if row.row_version != expected:
                raise ConflictError(
                    "component_release_changed", "Refresh the version before publishing"
                )
            row.state, row.error_code, row.row_version = "queued", None, row.row_version + 1
            return release_record(row)

    def claim_release(self, now: datetime) -> ComponentVerificationJob | None:
        with self.sessions.begin() as session:
            row = session.scalar(
                select(Row)
                .where(
                    or_(
                        Row.state == "queued", (Row.state == "verifying") & (Row.lease_until <= now)
                    )
                )
                .order_by(Row.created_at, Row.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            if row.state == "verifying":
                self._fail(session, row, "component_verification_interrupted", now)
                return None
            if row.source_kind == "registry":
                row.state, row.lease_until = "verifying", now + timedelta(minutes=30)
                row.row_version += 1
                session.flush()
                return ComponentVerificationJob(release_record(row), None, None)
            assert row.sha256 is not None and row.size_bytes is not None
            identity = uuid4()
            key = f"component-releases/{row.id}/{identity}"
            PostgresBlobCatalogRepository(session).add_pending(
                NewBlob(identity, row.sha256, row.size_bytes, "application/x-tar", key)
            )
            session.flush()
            row.blob_id, row.state = identity, "verifying"
            row.lease_until, row.row_version = now + timedelta(minutes=30), row.row_version + 1
            session.flush()
            return ComponentVerificationJob(release_record(row), identity, key)

    def finish_release(
        self, job: ComponentVerificationJob, now: datetime, error: str | None
    ) -> bool:
        with self.sessions.begin() as session:
            row = session.get(Row, job.release.release_id, with_for_update=True)
            if (
                row is None
                or row.state != "verifying"
                or row.row_version != job.release.row_version
            ):
                return False
            if error or row.lease_until is None or row.lease_until <= now:
                self._fail(session, row, error or "component_verification_timeout", now)
                return False
            if row.source_kind == "registry":
                self._fail(session, row, "registry_candidate_proof_required", now)
                return False
            assert row.sha256 is not None and row.size_bytes is not None
            assert job.candidate_blob_id is not None
            if row.component == "linux-terminal":
                collision = session.scalar(
                    select(LinuxReleaseRow.id).where(
                        LinuxReleaseRow.version == row.version,
                        LinuxReleaseRow.architecture == row.architecture,
                        LinuxReleaseRow.id != row.id,
                    )
                )
                if collision is not None:
                    self._fail(session, row, "linux_release_version_conflict", now)
                    return False
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": f"artifact-blob:{row.sha256}"},
            )
            blobs = PostgresBlobCatalogRepository(session)
            ready = blobs.find_ready_by_sha256(row.sha256)
            if ready and (
                ready.size_bytes != row.size_bytes or ready.media_type != "application/x-tar"
            ):
                self._fail(session, row, "component_blob_conflict", now)
                return False
            if ready:
                row.blob_id = ready.blob_id
                session.flush()
                retire(session, job.candidate_blob_id, now)
            elif not blobs.mark_ready(job.candidate_blob_id, 1, now):
                raise ConflictError("component_blob_changed", "Version blob changed")
            if row.component == "linux-terminal":
                self._mirror_linux(session, row, now)
            row.state, row.published_at, row.lease_until = "published", now, None
            row.row_version += 1
            return True

    @staticmethod
    def _mirror_linux(session: Session, row: Row, now: datetime) -> None:
        existing = session.get(LinuxReleaseRow, row.id)
        if existing is not None:
            if existing.sha256 != row.sha256 or existing.expected_image_id != row.expected_image_id:
                raise ConflictError(
                    "linux_release_identity_conflict", "Linux release identity changed"
                )
            return
        session.add(
            LinuxReleaseRow(
                id=row.id,
                version=row.version,
                architecture=row.architecture,
                size_bytes=row.size_bytes,
                sha256=row.sha256,
                candidate_image=row.candidate_image,
                expected_image_id=row.expected_image_id,
                state="published",
                created_at=row.created_at,
                published_at=now,
                blob_id=row.blob_id,
                row_version=1,
            )
        )
        session.flush()

    @staticmethod
    def _fail(session: Session, row: Row, error: str, now: datetime) -> None:
        candidate = row.blob_id
        row.state, row.error_code, row.blob_id, row.lease_until = "failed", error, None, None
        row.row_version += 1
        session.flush()
        retire(session, candidate, now)

    def release_object_key(
        self, identity: UUID, internal: bool = False
    ) -> tuple[ComponentRelease, str]:
        with self.sessions() as session:
            row = self._available_release(session, identity, internal)
            blob = self._available_blob(session, row, internal)
            return release_record(row), blob.object_key

    def host_manifest(self, identity: UUID) -> ComponentRelease:
        with self.sessions() as session:
            row = self._available_release(session, identity, True)
            if row.source_kind == "archive":
                self._available_blob(session, row, True)
            return release_record(row)

    @staticmethod
    def _available_release(session: Session, identity: UUID, internal: bool) -> Row:
        row = session.get(Row, identity)
        if row is None:
            raise NotFoundError("component_release")
        if row.state != "published" and not (internal and row.state == "verifying"):
            raise ConflictError("component_version_unpublished", "Version is not published")
        if row.state == "verifying" and (
            row.lease_until is None or row.lease_until <= datetime.now(UTC)
        ):
            raise ConflictError("component_verification_expired", "Verification lease expired")
        return row

    @staticmethod
    def _available_blob(session: Session, row: Row, internal: bool) -> BlobObjectRow:
        if row.blob_id is None:
            raise ConflictError("component_version_missing", "Version blob is unavailable")
        blob = session.get(BlobObjectRow, row.blob_id)
        if blob is None or (blob.status != "ready" and not internal):
            raise ConflictError("component_version_unavailable", "Version object is not ready")
        return blob
