from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.models import BlobObjectRow
from al1s.adapters.postgres.release_models import LinuxReleaseRow as Row
from al1s.adapters.postgres.repositories import (
    PostgresAuditRepository,
    PostgresBlobCatalogRepository,
    PostgresGcJobRepository,
)
from al1s.execution.errors import ConflictError, NotFoundError
from al1s.kernel.types import NewAuditEntry, NewBlob
from al1s.releases.contracts import MEDIA_TYPE, Release, ReleaseInput, VerificationJob


def record(row: Row) -> Release:
    return Release.model_validate(
        {
            "release_id": row.id,
            **{key: getattr(row, key) for key in Release.model_fields if key != "release_id"},
        }
    )


def retire(session: Session, identity: UUID | None, now: datetime) -> None:
    if identity and PostgresBlobCatalogRepository(session).mark_quarantined(identity, 1):
        PostgresGcJobRepository(session).schedule(uuid4(), identity, now + timedelta(hours=1))


def audit(session: Session, row: Row) -> None:
    PostgresAuditRepository(session, owner_module="platform").add(
        NewAuditEntry(
            audit_id=uuid4(),
            actor_type="system",
            actor_id=None,
            action="linux.release." + row.state,
            target_type="linux_release",
            target_id=row.id,
            correlation_id=row.id,
            details={"version": row.version, "state": row.state},
            summary="Linux release state recorded",
        )
    )


class ReleaseRepository:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def get(self, identity: UUID) -> Release:
        with self.sessions() as session:
            row = session.get(Row, identity)
            if row is None:
                raise NotFoundError("linux_release")
            return record(row)

    def create(self, body: ReleaseInput, now: datetime) -> Release:
        try:
            with self.sessions.begin() as session:
                row = Row(
                    id=body.release_id,
                    **body.model_dump(exclude={"release_id"}),
                    state="draft",
                    created_at=now,
                    row_version=1,
                )
                session.add(row)
                session.flush()
                audit(session, row)
                return record(row)
        except IntegrityError:
            with self.sessions() as session:
                previous = session.get(Row, body.release_id)
                if (
                    previous
                    and ReleaseInput.model_validate(
                        record(previous).model_dump(include=set(ReleaseInput.model_fields))
                    )
                    == body
                ):
                    return record(previous)
            raise ConflictError(
                "release_identity_conflict", "Release identity or version exists"
            ) from None

    def list(self, offset: int, limit: int) -> list[Release]:
        with self.sessions() as session:
            return [
                record(row)
                for row in session.scalars(
                    select(Row)
                    .order_by(Row.created_at.desc(), Row.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ]

    def enqueue(self, identity: UUID, expected: int) -> Release:
        with self.sessions.begin() as session:
            row = session.get(Row, identity, with_for_update=True)
            if row is None:
                raise NotFoundError("linux_release")
            if row.state in {"queued", "verifying", "published"}:
                return record(row)
            if row.row_version != expected:
                raise ConflictError("release_changed", "Refresh the release before publishing")
            row.state, row.error_code = "queued", None
            row.row_version += 1
            audit(session, row)
            return record(row)

    def claim(self, now: datetime) -> VerificationJob | None:
        with self.sessions.begin() as session:
            row = session.scalar(
                select(Row)
                .where(
                    or_(
                        Row.state == "queued",
                        (Row.state == "verifying") & (Row.lease_until <= now),
                    )
                )
                .order_by(Row.created_at, Row.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            if row.state == "verifying":
                self._fail(session, row, "verification_interrupted", now)
                return None  # Explicit republish is allowed; old worker is fenced.
            identity = uuid4()
            key = f"linux-releases/{row.id}/{identity}"
            PostgresBlobCatalogRepository(session).add_pending(
                NewBlob(
                    identity,
                    row.sha256,
                    row.size_bytes,
                    MEDIA_TYPE,
                    key,
                )
            )
            session.flush()
            row.blob_id, row.state = identity, "verifying"
            row.lease_until, row.row_version = now + timedelta(minutes=30), row.row_version + 1
            session.flush()
            return VerificationJob(
                release=record(row), candidate_blob_id=identity, destination_key=key
            )

    def finish(self, job: VerificationJob, now: datetime, error: str | None = None) -> bool:
        with self.sessions.begin() as session:
            row = session.get(Row, job.release.release_id, with_for_update=True)
            if (
                row is None
                or row.state != "verifying"
                or row.row_version != job.release.row_version
            ):
                return False
            if error or row.lease_until is None or row.lease_until <= now:
                self._fail(session, row, error or "verification_timeout", now)
                return False
            # Same hash lock namespace as artifact finalization; no object I/O in transaction.
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"artifact-blob:{row.sha256}"},
            )
            blobs = PostgresBlobCatalogRepository(session)
            ready = blobs.find_ready_by_sha256(row.sha256)
            if ready and (ready.size_bytes != row.size_bytes or ready.media_type != MEDIA_TYPE):
                self._fail(session, row, "release_blob_conflict", now)
                return False
            if ready:
                row.blob_id = ready.blob_id
                session.flush()
                retire(session, job.candidate_blob_id, now)
            elif not blobs.mark_ready(job.candidate_blob_id, 1, now):
                raise ConflictError("release_blob_changed", "Release blob changed")
            row.state, row.published_at, row.lease_until = "published", now, None
            row.staging_cleanup_at = now + timedelta(hours=2)
            row.row_version += 1
            audit(session, row)
            return True

    @staticmethod
    def _fail(session: Session, row: Row, error: str, now: datetime) -> None:
        candidate = row.blob_id
        row.state, row.error_code, row.blob_id, row.lease_until = "failed", error, None, None
        row.row_version += 1
        session.flush()
        retire(session, candidate, now)
        audit(session, row)

    def object_key(self, identity: UUID) -> tuple[Release, str]:
        with self.sessions() as session:
            pair = session.execute(
                select(Row, BlobObjectRow.object_key)
                .join(
                    BlobObjectRow,
                    Row.blob_id == BlobObjectRow.id,
                )
                .where(
                    Row.id == identity, Row.state == "published", BlobObjectRow.status == "ready"
                )
            ).one_or_none()
            if pair is None:
                raise NotFoundError("published_linux_release")
            return record(pair[0]), pair[1]

    def cleanup_candidate(self, now: datetime) -> UUID | None:
        with self.sessions() as session:
            return session.scalar(
                select(Row.id)
                .where(Row.state == "published", Row.staging_cleanup_at <= now)
                .order_by(Row.staging_cleanup_at, Row.id)
                .limit(1)
            )

    def defer_cleanup(self, identity: UUID, now: datetime, *, failed: bool = False) -> None:
        # Retry failures later so one unavailable object cannot starve other versions.
        # Repeat successful cleanup daily: a previously signed, slow PUT may finish late.
        delay = timedelta(minutes=15) if failed else timedelta(days=1)
        with self.sessions.begin() as session:
            session.execute(
                update(Row)
                .where(
                    Row.id == identity,
                    Row.state == "published",
                    Row.staging_cleanup_at <= now,
                )
                .values(staging_cleanup_at=now + delay)
            )
