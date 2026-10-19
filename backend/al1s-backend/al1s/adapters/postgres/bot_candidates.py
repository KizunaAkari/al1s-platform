from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.component_models import ComponentReleaseRow as Row
from al1s.adapters.postgres.component_records import release_record
from al1s.components.bot_contracts import BuiltQqCandidate, QqCandidateDraft
from al1s.components.contracts import ComponentRelease
from al1s.components.qq_compatibility import assert_qq_source_verified, compatible_qq_sources
from al1s.execution.errors import ConflictError, NotFoundError


class BotCandidateRepository:
    sessions: sessionmaker[Session]

    def store_qq_candidates(
        self, candidates: list[QqCandidateDraft], now: datetime
    ) -> list[ComponentRelease]:
        if not candidates or len(candidates) > 20:
            return []
        values = []
        for item in candidates:
            values.append(
                dict(
                    id=uuid4(),
                    component="qq-bot",
                    version=f"{item.native_version}+meme-{item.runtime_version}",
                    architecture=item.architecture,
                    native_version=item.native_version,
                    compatible_from=compatible_qq_sources(item.native_version),
                    data_policy="same-format",
                    source_kind="registry",
                    state="draft",
                    created_at=now,
                    row_version=1,
                    **item.model_dump(exclude={"native_version", "architecture"}),
                )
            )
        with self.sessions.begin() as session:
            session.execute(
                insert(Row)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=[Row.component, Row.version, Row.architecture]
                )
            )
            keys = [("qq-bot", v["version"], v["architecture"]) for v in values]
            rows = session.scalars(
                select(Row).where(tuple_(Row.component, Row.version, Row.architecture).in_(keys))
            ).all()
            frozen = {(v["version"], v["architecture"]): v for v in values}
            for row in rows:
                expected = frozen[(row.version, row.architecture)]
                if any(
                    getattr(row, k) != expected[k]
                    for k in (
                        "registry_digest",
                        "platform_manifest_digest",
                        "runtime_artifact_digest",
                        "runtime_version",
                    )
                ):
                    raise ConflictError(
                        "qq_candidate_changed",
                        "Upstream or runtime identity changed; existing version is preserved",
                    )
            return [release_record(row) for row in rows]

    def finish_registry_candidate(
        self, identity: UUID, expected: int, built: BuiltQqCandidate
    ) -> ComponentRelease:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(Row, identity, with_for_update=True)
            if row is None:
                raise NotFoundError("component_release")
            try:
                assert_qq_source_verified(row.native_version, row.registry_digest)
            except ValueError:
                raise ConflictError(
                    "qq_data_format_unverified", "Candidate data format is unverified"
                ) from None
            if row.compatible_from != compatible_qq_sources(row.native_version):
                raise ConflictError(
                    "qq_data_format_unverified", "Candidate compatibility evidence changed"
                )
            if (
                row.state != "verifying"
                or row.row_version != expected
                or row.source_kind != "registry"
                or not built.proof
                or row.lease_until is None
                or row.lease_until <= now
            ):
                raise ConflictError(
                    "qq_candidate_not_verified", "Candidate preparation is no longer owned"
                )
            if (
                built.native_version != row.native_version
                or built.runtime_version != row.runtime_version
            ):
                raise ConflictError(
                    "qq_candidate_version_mismatch", "Candidate does not match its frozen source"
                )
            row.candidate_image = built.candidate_image
            row.expected_image_id = built.expected_image_id
            row.state, row.published_at, row.lease_until = "published", now, None
            row.row_version += 1
            return release_record(row)
