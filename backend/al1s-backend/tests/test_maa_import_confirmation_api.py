import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, Response

from al1s.api.maa_archive_upload import upload_archive
from al1s.api.maa_catalog import ImportArchiveRequest, import_archive
from al1s.api.maa_import_operations import submit
from al1s.maa.errors import MaaDomainError
from al1s.maa.types import ImportBatchRecord, ImportResult, ImportStatus


def batch():
    return ImportBatchRecord(
        batch_id=uuid4(),
        logical_sha256="a" * 64,
        archive_sha256="b" * 64,
        archive_schema="al1s-script-archive/v2",
        status=ImportStatus.COMPLETED,
        script_count=1,
        application_count=1,
        resource_reference_count=0,
        unique_resource_count=0,
        error_code=None,
        diagnostic=None,
        created_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        row_version=2,
    )


def request_for(service, selection):
    async def stream():
        yield b"zip"

    return SimpleNamespace(
        headers={"content-type": "application/zip", "X-AL1S-Import-Selection": selection},
        stream=stream,
        app=SimpleNamespace(
            state=SimpleNamespace(
                maa_archive_import_commands=service,
                maa_archive_upload_slots=asyncio.Semaphore(1),
            )
        ),
    )


@pytest.mark.parametrize("asynchronous", [False, True])
def test_zip_endpoints_forward_explicit_selection(asynchronous):
    async def run():
        app_id, script_id = uuid4(), uuid4()
        options = {
            "target_applications": {"com.example": str(app_id)},
            "confirmed_overwrites": {str(script_id): 7},
        }
        expected = {
            "target_applications": {"com.example": app_id},
            "confirmed_overwrites": {script_id: 7},
        }
        service = Mock()
        service.import_uploaded_archive.return_value = ImportResult(batch(), False)
        service.prepare_upload.return_value = (object(), batch(), True)
        request = request_for(service, json.dumps(options))
        if asynchronous:
            await submit(request, BackgroundTasks(), Response())
            service.prepare_upload.assert_called_once_with(b"zip", **expected)
        else:
            await upload_archive(request, Response())
            kwargs = service.import_uploaded_archive.call_args.kwargs
            assert kwargs["target_applications"] == expected["target_applications"]
            assert kwargs["confirmed_overwrites"] == expected["confirmed_overwrites"]

    asyncio.run(run())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_confirmation_error_is_returned_before_background_work(asynchronous):
    async def run():
        service = Mock()
        error = MaaDomainError(
            "archive_overwrite_confirmation_required",
            "Confirm overwrite",
            409,
            context={"overwrites": [{"script_id": str(uuid4()), "row_version": 3}]},
        )
        service.import_uploaded_archive.side_effect = error
        service.prepare_upload.side_effect = error
        request = request_for(service, "{}")
        background = BackgroundTasks()
        with pytest.raises(MaaDomainError) as caught:
            if asynchronous:
                await submit(request, background, Response())
            else:
                await upload_archive(request, Response())
        assert caught.value is error
        assert not background.tasks
        assert not request.app.state.maa_archive_upload_slots.locked()

    asyncio.run(run())


def test_blob_endpoint_forwards_confirmation():
    app_id, script_id = uuid4(), uuid4()
    body = ImportArchiveRequest(
        archive_blob_id=uuid4(),
        target_applications={"com.example": app_id},
        confirmed_overwrites={script_id: 4},
    )
    service = Mock()
    service.import_ready_blob.return_value = SimpleNamespace(
        receipt=SimpleNamespace(response_body=asdict(batch()), response_status=201),
        replayed=False,
    )
    request = SimpleNamespace(
        headers={},
        state=SimpleNamespace(request_id=str(uuid4())),
        app=SimpleNamespace(state=SimpleNamespace(maa_archive_import_commands=service)),
    )
    import_archive(body, request, Response(), "test-idempotency")
    kwargs = service.import_ready_blob.call_args.kwargs
    assert kwargs["target_applications"] == {"com.example": app_id}
    assert kwargs["confirmed_overwrites"] == {script_id: 4}
