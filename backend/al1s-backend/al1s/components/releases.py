import hashlib
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

from al1s.components.bot_contracts import BotAlias, BuiltQqCandidate, QqCandidateDraft
from al1s.components.contracts import ComponentRelease, ComponentReleaseInput, ReleasePage
from al1s.components.types import ComponentVerificationJob
from al1s.components.verification import DockerArchiveVerifier
from al1s.execution.artifact_upload_types import PresignedArtifactUpload
from al1s.execution.errors import ConflictError, InvalidRequestError
from al1s.releases.service import ReleaseObjects


class ComponentReleaseStore(Protocol):
    def list_bot_releases(
        self, alias: BotAlias, limit: int, cursor: UUID | None
    ) -> ReleasePage: ...
    def host_manifest(self, identity: UUID) -> ComponentRelease: ...
    def store_qq_candidates(
        self, candidates: list[QqCandidateDraft], now: datetime
    ) -> list[ComponentRelease]: ...
    def finish_registry_candidate(
        self, identity: UUID, expected: int, built: BuiltQqCandidate
    ) -> ComponentRelease: ...
    def create_release(self, body: ComponentReleaseInput, now: datetime) -> ComponentRelease: ...
    def get_release(self, identity: UUID) -> ComponentRelease: ...
    def list_releases(self, component: str, limit: int, cursor: UUID | None) -> ReleasePage: ...
    def enqueue_release(self, identity: UUID, expected: int) -> ComponentRelease: ...
    def claim_release(self, now: datetime) -> ComponentVerificationJob | None: ...
    def finish_release(
        self, job: ComponentVerificationJob, now: datetime, error: str | None
    ) -> bool: ...
    def release_object_key(
        self, identity: UUID, internal: bool = False
    ) -> tuple[ComponentRelease, str]: ...


def staging_key(identity: UUID) -> str:
    return f"component-release-uploads/{identity}"


class ComponentReleaseService:
    def __init__(
        self,
        store: ComponentReleaseStore,
        objects: ReleaseObjects,
        verifier: DockerArchiveVerifier,
        temporary_directory: Path,
        now: Callable[[], datetime] | None = None,
        registry_prepare: Callable[[UUID], BuiltQqCandidate] | None = None,
    ):
        self.store, self.objects, self.verifier = store, objects, verifier
        self.temporary_directory = temporary_directory
        self.now = now or (lambda: datetime.now(UTC))
        self.registry_prepare = registry_prepare

    def create(self, body: ComponentReleaseInput) -> ComponentRelease:
        if body.source_kind != "archive" or body.component == "qq-bot":
            raise InvalidRequestError(
                "qq_candidates_managed", "QQ candidates come from the fixed official source"
            )
        return self.store.create_release(body, self.now())

    def upload(self, identity: UUID) -> PresignedArtifactUpload:
        release = self.store.get_release(identity)
        if release.source_kind != "archive" or release.sha256 is None:
            raise InvalidRequestError(
                "candidate_has_no_archive", "This candidate is prepared by the registered builder"
            )
        if release.state not in {"draft", "failed"}:
            raise ConflictError("component_upload_closed", "Version upload is closed")
        return self.objects.create_presigned_upload(
            staging_key(identity),
            media_type="application/x-tar",
            sha256=release.sha256,
            expires_in=timedelta(minutes=60),
        )

    def publish(self, identity: UUID, expected: int) -> ComponentRelease:
        return self.store.enqueue_release(identity, expected)

    def list(self, component: str, limit: int = 25, cursor: UUID | None = None) -> ReleasePage:
        return self.store.list_releases(component, limit, cursor)

    def list_bot(self, alias: BotAlias, limit: int = 5, cursor: UUID | None = None) -> ReleasePage:
        return self.store.list_bot_releases(alias, limit, cursor)

    def verify_once(self) -> bool:
        job = self.store.claim_release(self.now())
        if job is None:
            return False
        if job.release.source_kind == "registry":
            try:
                if self.registry_prepare is None:
                    raise ValueError("qq_builder_unconfigured")
                built = self.registry_prepare(job.release.release_id)
                self.store.finish_registry_candidate(
                    job.release.release_id, job.release.row_version, built
                )
            except Exception:
                self.store.finish_release(job, self.now(), "qq_candidate_preparation_failed")
            return True
        assert job.destination_key is not None
        assert job.release.size_bytes is not None and job.release.sha256 is not None
        error = None
        try:
            self.temporary_directory.mkdir(parents=True, exist_ok=True)
            self.objects.finalize_artifact(
                staging_key(job.release.release_id),
                job.destination_key,
                size_bytes=job.release.size_bytes,
                sha256=job.release.sha256,
                media_type="application/x-tar",
            )
            with tempfile.TemporaryDirectory(
                dir=self.temporary_directory, prefix="component-check-"
            ) as temporary:
                path = Path(temporary) / "image.tar"
                self._download(job, path)
                self.verifier.verify(path, job.release)
        except Exception:
            error = "component_package_verification_failed"
        self.store.finish_release(job, self.now(), error)
        return True

    def _download(self, job: ComponentVerificationJob, path: Path) -> None:
        digest = hashlib.sha256()
        size = job.release.size_bytes
        assert size is not None and job.destination_key is not None
        with path.open("xb") as output:
            for start in range(0, size, 4 * 1024**2):
                end = min(size, start + 4 * 1024**2) - 1
                body = self.objects.get_range(job.destination_key, start, end)
                if len(body) != end - start + 1:
                    raise ValueError("package_range_incomplete")
                digest.update(body)
                output.write(body)
        if digest.hexdigest() != job.release.sha256:
            raise ValueError("package_hash_mismatch")

    def read_range(
        self, identity: UUID, start: int, end: int, internal: bool = False
    ) -> tuple[ComponentRelease, bytes]:
        release, key = self.store.release_object_key(identity, internal)
        assert release.size_bytes is not None
        if start < 0 or end < start or end >= release.size_bytes or end - start + 1 > 4 * 1024**2:
            raise InvalidRequestError(
                "invalid_component_range", "Request at most 4 MiB within file"
            )
        return release, self.objects.get_range(key, start, end)
