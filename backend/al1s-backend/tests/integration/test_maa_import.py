from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Lock
from uuid import uuid4

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.maa_models import (
    MaaApplicationRow,
    MaaImportBatchRow,
    MaaMutationReceiptRow,
    MaaScriptRow,
    MaaScriptVersionRow,
)
from al1s.adapters.postgres.models import BlobObjectRow
from al1s.maa.errors import MaaDomainError
from al1s.maa.mutation_service import MutationOutcome
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.types import (
    ApplicationRecord,
    ImportStatus,
)
from tests.integration.maa_support import (
    MemoryBlobStore,
    _mutation_service,
    _service,
)
from tests.integration.maa_support import (
    clean_maa_tables as clean_maa_tables,
)
from tests.integration.maa_support import (
    engine as engine,
)
from tests.maa_archive_fixture import FixtureScript, build_archive, image_data_url

pytestmark = pytest.mark.integration




def test_import_cancel_fences_late_archive_write(engine):
    from tests.integration.maa_support import bundle, setup_import

    service, _, target = setup_import(engine)
    parsed, batch, replayed = service.prepare_archive(
        bundle(), confirmed_overwrites={target.script_id: target.row_version},
    )
    assert not replayed
    cancelled = service.cancel(batch.batch_id, batch.row_version)
    assert cancelled.error_code == "archive_import_cancelled"
    assert service.cancel(batch.batch_id, batch.row_version) == cancelled
    with pytest.raises(MaaDomainError) as error:
        service.finish_archive(parsed, batch, correlation_id=uuid4())
    assert error.value.code == "archive_import_state_conflict"
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(MaaScriptRow)) == 1
        assert session.scalar(select(func.count()).select_from(MaaScriptVersionRow)) == 0
        stored = session.get(MaaImportBatchRow, batch.batch_id)
        assert stored is not None and stored.error_code == "archive_import_cancelled"


def test_import_deadline_fences_late_archive_write(engine):
    from tests.integration.maa_support import bundle, setup_import

    service, _, target = setup_import(engine)
    parsed, batch, _ = service.prepare_archive(
        bundle(), confirmed_overwrites={target.script_id: target.row_version},
    )
    service._now = lambda: batch.created_at + timedelta(minutes=30)
    with pytest.raises(MaaDomainError) as error:
        service.finish_archive(parsed, batch, correlation_id=uuid4())
    assert error.value.code == "archive_import_timeout"
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(MaaScriptRow)) == 1
        assert session.scalar(select(func.count()).select_from(MaaScriptVersionRow)) == 0
        stored = session.get(MaaImportBatchRow, batch.batch_id)
        assert stored is not None and stored.error_code == "archive_import_timeout"


















def test_management_mutation_receipt_replays_and_rejects_key_reuse(
    engine: Engine,
) -> None:
    service = _mutation_service(engine)
    application_id = uuid4()
    now = datetime.now(UTC)

    def mutate(uow: MaaUnitOfWork) -> MutationOutcome:
        uow.applications.add_many(
            [
                ApplicationRecord(
                    application_id=application_id,
                    package_name="com.example.idempotent",
                    display_name="Idempotent",
                    created_at=now,
                    updated_at=now,
                    deleted_at=None,
                    row_version=1,
                )
            ]
        )
        return MutationOutcome(
            aggregate_type="maa_application",
            aggregate_id=application_id,
            response_status=201,
            response_body={"application_id": str(application_id), "row_version": 1},
        )

    first = service.execute(
        operation="maa.applications.create",
        idempotency_key="create-app-1",
        request_payload={"package_name": "com.example.idempotent"},
        mutate=mutate,
    )
    replay = service.execute(
        operation="maa.applications.create",
        idempotency_key="create-app-1",
        request_payload={"package_name": "com.example.idempotent"},
        mutate=mutate,
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.receipt == first.receipt
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(MaaApplicationRow)) == 1
        assert session.scalar(select(func.count()).select_from(MaaMutationReceiptRow)) == 1

    with pytest.raises(MaaDomainError, match="different request") as exc_info:
        service.execute(
            operation="maa.applications.create",
            idempotency_key="create-app-1",
            request_payload={"package_name": "com.example.changed"},
            mutate=mutate,
        )
    assert exc_info.value.code == "idempotency_key_conflict"


def test_management_mutation_receipt_serializes_concurrent_replay(engine: Engine) -> None:
    service = _mutation_service(engine)
    call_lock = Lock()
    mutation_calls = 0

    def mutate(_uow: MaaUnitOfWork) -> MutationOutcome:
        nonlocal mutation_calls
        with call_lock:
            mutation_calls += 1
        return MutationOutcome(
            aggregate_type="maa_strategy",
            aggregate_id=uuid4(),
            response_status=200,
            response_body={"published": True},
        )

    def execute_once() -> bool:
        result = service.execute(
            operation="maa.strategies.publish:00000000-0000-0000-0000-000000000001",
            idempotency_key="publish-concurrent-1",
            request_payload={"expected_row_version": 4},
            mutate=mutate,
        )
        return result.replayed

    with ThreadPoolExecutor(max_workers=2) as executor:
        replayed = list(executor.map(lambda _index: execute_once(), range(2)))

    assert sorted(replayed) == [False, True]
    assert mutation_calls == 1
    with Session(engine) as session:
        receipts = session.scalars(select(MaaMutationReceiptRow)).all()
        assert len(receipts) == 1


def test_archive_validation_failure_preserves_existing_saved_script(engine: Engine) -> None:
    from tests.integration.maa_support import setup_import

    service, _, target = setup_import(engine)
    archive = build_archive(FixtureScript(
        "Saved", {
            "version": 2,
            "script_type": "module_process",
            "target": {"application_package": "com.example.game"},
            "steps": [{"action": "unknown_action"}],
        },
    ))
    with pytest.raises(MaaDomainError) as caught:
        service.import_archive(
            archive, correlation_id=uuid4(),
            confirmed_overwrites={target.script_id: target.row_version},
        )
    assert caught.value.code == "unregistered_action"
    with Session(engine) as session:
        saved = session.get(MaaScriptRow, target.script_id)
        assert saved is not None
        assert saved.current_version_id is None and saved.row_version == 1
        assert session.scalar(select(func.count()).select_from(MaaScriptVersionRow)) == 0
        assert session.scalar(select(func.count()).select_from(BlobObjectRow)) == 0


def test_archive_resource_upload_retry_keeps_only_ready_asset(engine: Engine) -> None:
    from tests.integration.maa_support import setup_import

    _, _, target = setup_import(engine)
    store = MemoryBlobStore(fail_first_put=True)
    service = _service(engine, store)
    archive = build_archive(FixtureScript(
        "New image", {
            "version": 2,
            "script_type": "module_process",
            "target": {"application_package": "com.example.game"},
            "steps": [{"action": "wait_image", "template_base64": image_data_url()}],
        },
    ))
    with pytest.raises(MaaDomainError) as caught:
        service.import_archive(archive, correlation_id=uuid4())
    assert caught.value.code == "archive_import_failed"
    result = service.import_archive(archive, correlation_id=uuid4())
    assert result.batch.status is ImportStatus.COMPLETED
    with Session(engine) as session:
        assert session.get(MaaScriptRow, target.script_id) is not None
        assert session.scalar(select(func.count()).select_from(MaaScriptRow)) == 2
        assert session.scalar(select(func.count()).select_from(BlobObjectRow)
                              .where(BlobObjectRow.status == "ready")) == 1


def test_import_query_count_is_bounded_for_larger_archive(engine: Engine) -> None:
    from tests.integration.maa_support import setup_import

    service, _, _ = setup_import(engine)
    counts: list[int] = []
    for size in (10, 100):
        archive = build_archive(*(
            FixtureScript(f"Process {size}-{index}", {
                "version": 2,
                "script_type": "module_process",
                "target": {"application_package": "com.example.game"},
                "steps": [{"action": "wait", "seconds": 1}],
            }) for index in range(size)
        ))
        count = 0

        def count_statement(*_args: object) -> None:
            nonlocal count
            count += 1

        event.listen(engine, "before_cursor_execute", count_statement)
        try:
            result = service.import_archive(archive, correlation_id=uuid4())
            assert result.batch.status is ImportStatus.COMPLETED
        finally:
            event.remove(engine, "before_cursor_execute", count_statement)
        counts.append(count)
    assert counts[1] <= counts[0] + 10
