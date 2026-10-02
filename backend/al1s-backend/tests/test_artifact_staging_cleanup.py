from datetime import UTC, datetime, timedelta
from uuid import uuid4

from al1s.maa.artifact_staging_cleanup import ArtifactStagingCleanup, StagingCandidate

NOW = datetime(2026, 9, 25, tzinfo=UTC)


class Repository:
    def __init__(self, candidate: StagingCandidate) -> None:
        self.candidate = candidate
        self.next_at = None

    def claim(self, now, *, limit, lease):
        assert limit == 10 and lease == timedelta(minutes=15)
        return (self.candidate,)

    def settle_many(self, outcomes):
        assert len(outcomes) == 1 and outcomes[0].candidate == self.candidate
        self.next_at = outcomes[0].next_at
        return {self.candidate.artifact_id}


class Objects:
    def __init__(self, *, fail=False) -> None:
        self.deleted = []
        self.fail = fail

    def delete(self, key):
        if self.fail:
            raise OSError("storage unavailable")
        self.deleted.append(key)


def candidate(*, age: timedelta = timedelta(days=1), wrong_key=False):
    terminal, artifact = uuid4(), uuid4()
    key = f"artifacts/{terminal}/{artifact}"
    return StagingCandidate(
        artifact, terminal, "verified-artifacts/safe" if wrong_key else key,
        NOW - age, NOW - age, NOW + timedelta(minutes=15),
    )


def test_success_rechecks_then_finishes_after_seven_days():
    recent = Repository(candidate())
    objects = Objects()
    assert ArtifactStagingCleanup(recent, objects, now=lambda: NOW).run_once() == (1, 1, 0)
    assert objects.deleted == [recent.candidate.object_key]
    assert recent.next_at == NOW + timedelta(days=1)

    old = Repository(candidate(age=timedelta(days=8)))
    assert ArtifactStagingCleanup(old, objects, now=lambda: NOW).run_once() == (1, 1, 0)
    assert old.next_at is None


def test_failure_retries_and_foreign_key_is_never_deleted():
    repository = Repository(candidate())
    objects = Objects(fail=True)
    assert ArtifactStagingCleanup(repository, objects, now=lambda: NOW).run_once() == (1, 0, 1)
    assert repository.next_at == NOW + timedelta(minutes=15)

    repository = Repository(candidate(wrong_key=True))
    objects = Objects()
    assert ArtifactStagingCleanup(repository, objects, now=lambda: NOW).run_once() == (1, 0, 1)
    assert objects.deleted == []
