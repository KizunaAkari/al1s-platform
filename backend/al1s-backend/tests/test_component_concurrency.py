from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from al1s.adapters.postgres.component_guard import ComponentAdmission
from al1s.adapters.postgres.component_models import ComponentReleaseRow
from al1s.adapters.postgres.repositories import PostgresBlobCatalogRepository
from al1s.components.contracts import ComponentTarget, ComponentUpgradeRequest
from al1s.execution.errors import ConflictError
from al1s.kernel.types import NewBlob
from tests.test_component_inventory import release
from tests.test_component_postgres import repository as repository


def published(repository):
    now = datetime.now(UTC)
    body = release(version=str(uuid4()))
    row = repository.create_release(body, now)
    with repository.sessions.begin() as session:
        blob = uuid4()
        blobs = PostgresBlobCatalogRepository(session)
        blobs.add_pending(
            NewBlob(blob, body.sha256, body.size_bytes, "application/x-tar", str(blob))
        )
        session.flush()
        blobs.mark_ready(blob, 1, now)
        current = session.get(ComponentReleaseRow, row.release_id)
        current.blob_id, current.state, current.published_at = blob, "published", now
    return body


def test_unknown_keeps_global_admission_blocked_until_evidence(repository):
    body, now = published(repository), datetime.now(UTC)
    request = ComponentUpgradeRequest(
        component="postgresql",
        target=ComponentTarget(kind="platform"),
        release_id=body.release_id,
        expected_source="c" * 64,
        idempotency_key="upgrade-1",
    )
    first = repository.reserve_operation(request, "d" * 64, now)
    assert repository.reserve_operation(request, "d" * 64, now).operation_id == first.operation_id
    with pytest.raises(ConflictError):
        repository.reserve_operation(
            request.model_copy(update={"idempotency_key": "upgrade-2"}), "e" * 64, now
        )
    repository.update_operation(first.operation_id, {"state": "unknown", "stage": "unknown"}, now)
    with repository.sessions() as session:
        with pytest.raises(ConflictError):
            ComponentAdmission(session).admit()
        with pytest.raises(IntegrityError) as caught, session.begin_nested():
            session.execute(text("INSERT INTO task_requests(id) VALUES(:id)"), {"id": uuid4()})
        assert caught.value.orig.diag.constraint_name == "component_admission"
    with pytest.raises(ConflictError):
        repository.update_operation(
            first.operation_id, {"state": "failed_safe", "proof": False}, now
        )
    repository.update_operation(first.operation_id, {"state": "failed_safe", "proof": True}, now)
    with repository.sessions() as session:
        ComponentAdmission(session).admit()


def test_replay_of_unknown_operation_never_posts_again_or_depends_on_changed_source(repository):
    from al1s.components.operations import ComponentUpgradeService
    from al1s.components.status_capability import StatusCapability
    from tests.test_component_operations import current

    body = published(repository)

    class Inventory:
        def get(self, *args):
            return current()

    class Host:
        calls = 0

        def submit(self, identity):
            self.calls += 1
            return {"state": "unknown", "stage": "unconfirmed", "proof": False}

    inventory, host = Inventory(), Host()
    service = ComponentUpgradeService(repository, inventory, host, StatusCapability("x" * 32))
    request = ComponentUpgradeRequest(
        component="postgresql",
        target=ComponentTarget(kind="platform"),
        release_id=body.release_id,
        expected_source="c" * 64,
        idempotency_key="unknown-replay",
    )
    first = service.start(request)
    inventory.get = lambda *_: None
    second = service.start(request)
    assert second.operation_id == first.operation_id and second.state == "unknown"
    assert host.calls == 1
