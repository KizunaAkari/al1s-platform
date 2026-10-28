from datetime import UTC, datetime
from uuid import UUID

from al1s.components.bot_sources import OfficialQqSource
from al1s.components.contracts import ComponentRelease
from al1s.components.qq_compatibility import assert_qq_source_verified
from al1s.components.releases import ComponentReleaseStore
from al1s.execution.errors import ConflictError


class QqCandidateService:
    def __init__(
        self,
        store: ComponentReleaseStore,
        source: OfficialQqSource,
        runtime_digest: str,
        runtime_version: str,
        architecture: str = "amd64",
    ):
        self.store, self.source = store, source
        self.runtime_digest, self.runtime_version, self.architecture = (
            runtime_digest,
            runtime_version,
            architecture,
        )

    def check(self) -> list[ComponentRelease]:
        if not self.runtime_digest or not self.runtime_version:
            raise ConflictError(
                "qq_runtime_unconfigured", "The complete QQ runtime has not been registered"
            )
        try:
            candidates = self.source.check(
                self.architecture, self.runtime_version, self.runtime_digest
            )
        except ValueError as exc:
            raise ConflictError(str(exc), "Official QQ source could not be verified") from None
        return self.store.store_qq_candidates(candidates, datetime.now(UTC))

    def prepare(self, identity: UUID, expected: int) -> ComponentRelease:
        release = self.store.get_release(identity)
        if release.component != "qq-bot" or release.source_kind != "registry":
            raise ConflictError("qq_candidate_mismatch", "Select a registered QQ candidate")
        try:
            assert_qq_source_verified(release.native_version, release.registry_digest)
        except ValueError:
            raise ConflictError(
                "qq_data_format_unverified", "此原生版本的数据格式尚未验证; 当前服务保持原版本"
            ) from None
        return self.store.enqueue_release(identity, expected)
