from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, insert

from al1s.adapters.postgres.component_models import ComponentOperationRow
from al1s.components.bot_contracts import BuiltQqCandidate, QqCandidateDraft
from al1s.components.contracts import ComponentTarget, ComponentUpgradeRequest
from al1s.execution.errors import ConflictError
from tests.test_component_postgres import repository as repository


def candidates(count=1):
    return [
        QqCandidateDraft(
            native_version=f"8.3.{index}",
            architecture="amd64",
            registry_digest="sha256:" + "a" * 64,
            platform_manifest_digest="sha256:" + "b" * 64,
            runtime_artifact_digest="sha256:" + "c" * 64,
            runtime_version="1.0.0",
        )
        for index in range(count)
    ]


def published(repository):
    now = datetime.now(UTC)
    native = candidates()[0].model_copy(
        update={
            "native_version": "8.3.2",
            "registry_digest": (
                "sha256:0b9e20fdda15db223eceff87b0afd51c82"
                "dee156ce3c49424f1eb7f417786cdd"
            ),
        }
    )
    row = repository.store_qq_candidates([native], now)[0]
    repository.enqueue_release(row.release_id, row.row_version)
    job = repository.claim_release(now)
    assert job is not None and job.release.release_id == row.release_id
    built = BuiltQqCandidate(
        candidate_image="al1s-plachta:qq-test-8-3-0",
        expected_image_id="sha256:" + "d" * 64,
        native_version="8.3.2",
        runtime_version="1.0.0",
        proof=True,
    )
    return repository.finish_registry_candidate(row.release_id, job.release.row_version, built)


def test_candidate_batch_has_two_data_statements_and_frozen_identity(repository):
    calls = []
    connection = repository.sessions.kw["bind"]

    def count(*args):
        if args[2].lstrip().upper().startswith(("SELECT", "INSERT")):
            calls.append(args[2])

    event.listen(connection, "before_cursor_execute", count)
    try:
        rows = repository.store_qq_candidates(candidates(20), datetime.now(UTC))
        assert len(rows) == 20 and len(calls) == 2
    finally:
        event.remove(connection, "before_cursor_execute", count)
    altered = candidates()[0].model_copy(update={"registry_digest": "sha256:" + "e" * 64})
    with pytest.raises(ConflictError):
        repository.store_qq_candidates([altered], datetime.now(UTC))
    assert repository.get_release(rows[0].release_id).candidate_image is None


def test_registry_host_manifest_preserves_state_and_verification_lease_without_blob(repository):
    from sqlalchemy import update

    from al1s.adapters.postgres.component_models import ComponentReleaseRow

    now = datetime.now(UTC)
    row = repository.store_qq_candidates(candidates(), now)[0]
    with pytest.raises(ConflictError, match="not published"):
        repository.host_manifest(row.release_id)
    repository.enqueue_release(row.release_id, row.row_version)
    job = repository.claim_release(now)
    assert job is not None
    result = repository.host_manifest(row.release_id)
    assert result.source_kind == "registry" and result.blob_id is None
    with pytest.raises(ConflictError, match="blob"):
        repository.release_object_key(row.release_id, True)
    with repository.sessions.begin() as session:
        session.execute(
            update(ComponentReleaseRow)
            .where(ComponentReleaseRow.id == row.release_id)
            .values(lease_until=now - timedelta(seconds=1))
        )
    with pytest.raises(ConflictError, match="lease expired"):
        repository.host_manifest(row.release_id)


def test_published_candidate_cas_and_independent_scope_admission(repository):
    row = published(repository)
    now = datetime.now(UTC)
    target = ComponentTarget(kind="bot", bot_alias="qq")
    request = ComponentUpgradeRequest(
        component="qq-bot",
        target=target,
        release_id=row.release_id,
        expected_source="f" * 64,
        idempotency_key="qq-first",
    )
    operation = repository.reserve_operation(request, "a" * 64, now)
    repository.update_operation(
        operation.operation_id, {"state": "unknown", "stage": "unknown"}, now
    )
    assert (
        repository.reserve_operation(request, "a" * 64, now).operation_id == operation.operation_id
    )
    assert repository.blockers(target)
    assert not repository.blockers(ComponentTarget(kind="bot", bot_alias="discord"))
    with pytest.raises(ConflictError):
        repository.reserve_operation(
            request.model_copy(update={"idempotency_key": "qq-second"}), "b" * 64, now
        )


def test_large_bot_history_is_filtered_and_keyset_paged_in_fixed_queries(repository):
    row = published(repository)
    now = datetime.now(UTC)
    values = [
        dict(
            id=uuid4(),
            component="qq-bot",
            scope="bot:qq",
            terminal_id=None,
            release_id=row.release_id,
            expected_source="f" * 64,
            idempotency_key=str(index),
            request_hash="a" * 64,
            state="succeeded",
            stage="completed",
            created_at=now - timedelta(seconds=index),
            updated_at=now,
            row_version=1,
        )
        for index in range(2500)
    ]
    with repository.sessions.begin() as session:
        session.execute(insert(ComponentOperationRow), values)
    calls = []
    connection = repository.sessions.kw["bind"]

    def count(*args):
        if args[2].lstrip().upper().startswith("SELECT"):
            calls.append(args[2])

    event.listen(connection, "before_cursor_execute", count)
    try:
        first = repository.list_operations(25, None, "bot:qq")
        second = repository.list_operations(25, first.next_cursor, "bot:qq")
        assert len(first.items) == len(second.items) == 25 and len(calls) == 3
        assert {r.operation_id for r in first.items}.isdisjoint(
            r.operation_id for r in second.items
        )
        assert repository.list_operations(25, None, "bot:discord").items == []
        from al1s.execution.errors import NotFoundError

        with pytest.raises(NotFoundError):
            repository.list_operations(25, first.next_cursor, "bot:discord")
    finally:
        event.remove(connection, "before_cursor_execute", count)
