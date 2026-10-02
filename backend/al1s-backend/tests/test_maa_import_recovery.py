from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest

from al1s.maa.errors import MaaDomainError
from al1s.maa.import_service import MaaArchiveImportService
from al1s.maa.types import ImportBatchRecord, ImportStatus


def setup(age):
    now = datetime.now(UTC)
    batch = ImportBatchRecord(
        batch_id=uuid4(),
        logical_sha256="a" * 64,
        archive_sha256="b" * 64,
        archive_schema="al1s-script-archive/v1",
        status=ImportStatus.PROCESSING,
        script_count=1,
        application_count=1,
        resource_reference_count=0,
        unique_resource_count=0,
        error_code=None,
        diagnostic=None,
        created_at=now - age,
        completed_at=None,
        row_version=4,
    )
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.imports.find_by_logical_hash.return_value = batch
    uow.imports.fail.return_value = True
    uow.imports.restart_failed.return_value = True
    service = MaaArchiveImportService(lambda: uow, Mock(), now=lambda: now)
    parsed = SimpleNamespace(
        logical_sha256=batch.logical_sha256, archive_sha256=batch.archive_sha256
    )
    return service, uow, parsed, batch


def test_live_import_is_not_stolen():
    service, uow, parsed, _ = setup(timedelta(minutes=29))
    with pytest.raises(MaaDomainError) as error:
        service._start_batch(parsed)
    assert error.value.code == "archive_import_in_progress"
    uow.imports.fail.assert_not_called()


def test_expired_import_can_be_retried_with_new_fence(monkeypatch):
    # Selection has its own tests; this test isolates expiry and fencing.
    monkeypatch.setattr("al1s.maa.import_service.resolve_import", Mock())
    service, uow, parsed, batch = setup(timedelta(minutes=30))
    restarted, replayed = service._start_batch(parsed)
    assert restarted.row_version == batch.row_version + 2
    assert not replayed
    assert uow.imports.restart_failed.call_args.args[1] == batch.row_version + 1
    uow.commit.assert_called_once()


def test_late_worker_cannot_finalize_or_fail_replacement():
    service, uow, parsed, batch = setup(timedelta(minutes=1))
    with pytest.raises(MaaDomainError):
        service._finalize_import(parsed, (), {}, uuid4(), batch.row_version - 1)
    service._record_failure(parsed, {}, uuid4(), RuntimeError(), batch.row_version - 1)
    uow.scripts.add_many.assert_not_called()
    uow.imports.fail.assert_not_called()
    uow.gc_jobs.schedule_many.assert_not_called()


def test_import_preserves_native_screen_size():
    from unittest.mock import Mock

    from al1s.maa.archive import parse_script_archive
    from al1s.maa.import_service import MaaArchiveImportService
    from tests.maa_archive_fixture import FixtureScript, build_archive

    parsed = parse_script_archive(
        build_archive(
            FixtureScript(
                "Native",
                {
                    "version": 2,
                    "script_type": "module_process",
                    "target": {
                        "application_package": "com.example.game",
                        "screen_size": {"width": 64, "height": 96},
                    },
                    "steps": [{"action": "tap", "x": 10, "y": 20}],
                },
            )
        )
    )
    prepared = MaaArchiveImportService(Mock(), Mock())._prepare_scripts(parsed)
    assert prepared[0].document["target"]["screen_size"] == {"width": 64, "height": 96}
