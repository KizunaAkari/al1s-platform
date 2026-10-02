from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from al1s.execution.artifact_upload_types import PresignedArtifactUpload
from al1s.execution.errors import ConflictError, InvalidRequestError
from al1s.releases.contracts import MEDIA_TYPE, Release, ReleaseInput, VerificationJob, staging_key


class ReleaseStore(Protocol):
    def create(self, body: ReleaseInput, now: datetime) -> Release: ...
    def get(self, identity: UUID) -> Release: ...
    def list(self, offset: int, limit: int) -> list[Release]: ...
    def enqueue(self, identity: UUID, expected: int) -> Release: ...
    def claim(self, now: datetime) -> VerificationJob | None: ...
    def finish(self, job: VerificationJob, now: datetime, error: str | None = None) -> bool: ...
    def object_key(self, identity: UUID) -> tuple[Release, str]: ...
    def cleanup_candidate(self, now: datetime) -> UUID | None: ...
    def defer_cleanup(self, identity: UUID, now: datetime, *, failed: bool = False) -> None: ...


class ReleaseObjects(Protocol):
    def delete(self, object_key: str) -> None: ...
    def create_presigned_upload(
        self, object_key: str, *, media_type: str, sha256: str, expires_in: timedelta
    ) -> PresignedArtifactUpload: ...
    def finalize_artifact(
        self,
        source_key: str,
        destination_key: str,
        *,
        size_bytes: int,
        sha256: str,
        media_type: str,
    ) -> None: ...
    def get_range(self, object_key: str, start: int, end_inclusive: int) -> bytes: ...


class ReleaseService:
    def __init__(
        self,
        store: ReleaseStore,
        objects: ReleaseObjects,
        now: Callable[[], datetime] | None = None,
    ):
        self.store, self.objects = store, objects
        self.now = now or (lambda: datetime.now(UTC))

    def create(self, body: ReleaseInput) -> Release:
        if body.candidate_image in {"al1s-terminal-next:stable", "al1s-terminal-next:rollback"}:
            raise InvalidRequestError("reserved_image_tag", "Use an immutable candidate image tag")
        return self.store.create(body, self.now())

    def upload(self, identity: UUID) -> PresignedArtifactUpload:
        release = self.store.get(identity)
        if release.state not in {"draft", "failed"}:
            raise ConflictError("release_upload_closed", "Release is already queued or published")
        return self.objects.create_presigned_upload(
            staging_key(identity),
            media_type=MEDIA_TYPE,
            sha256=release.sha256,
            expires_in=timedelta(minutes=60),
        )

    def verify_once(self) -> bool:
        job = self.store.claim(self.now())
        if job is None:
            return False
        error = None
        try:
            self.objects.finalize_artifact(
                staging_key(job.release.release_id),
                job.destination_key,
                size_bytes=job.release.size_bytes,
                sha256=job.release.sha256,
                media_type=MEDIA_TYPE,
            )
        except Exception:
            # No signed URL, object-store credential or upstream body in audit/API.
            error = "release_verification_failed"
        self.store.finish(job, self.now(), error)
        return True

    def read_range(self, identity: UUID, start: int, end: int) -> tuple[Release, bytes]:
        release, key = self.store.object_key(identity)
        if start < 0 or end < start or end >= release.size_bytes or end - start + 1 > 4 * 1024**2:
            raise InvalidRequestError("invalid_release_range", "Request at most 4 MiB within file")
        return release, self.objects.get_range(key, start, end)

    def cleanup_once(self) -> bool:
        now = self.now()
        identity = self.store.cleanup_candidate(now)
        if identity is None:
            return False
        failed = False
        try:
            self.objects.delete(staging_key(identity))
        except Exception:
            failed = True
        self.store.defer_cleanup(identity, now, failed=failed)
        return True
