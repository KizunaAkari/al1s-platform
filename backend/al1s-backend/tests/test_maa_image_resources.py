import hashlib
from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from PIL import Image

from al1s.maa.errors import MaaDomainError
from al1s.maa.image_resources import MaaImageResourceService, validate_png


def png():
    output = BytesIO()
    Image.new("RGB", (8, 6), "blue").save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize("body", [b"", b"not png", b"\x89PNG\r\n\x1a\n", png()[:-12]])
def test_rejects_invalid_or_truncated_png(body):
    with pytest.raises(MaaDomainError):
        validate_png(body)


def test_valid_png_dimensions():
    assert validate_png(png()) == (8, 6)


def fixture_service():
    uow = SimpleNamespace(
        scripts=Mock(), blobs=Mock(), gc_jobs=Mock(), imports=Mock(), flush=Mock(), commit=Mock()
    )
    uow.scripts.get_active.return_value = object()
    uow.blobs.find_ready_by_sha256.return_value = None
    state = {"open": False}

    @contextmanager
    def factory():
        assert not state["open"]
        state["open"] = True
        try:
            yield uow
        finally:
            state["open"] = False

    store = Mock()

    def put(key, body, media_type):
        assert not state["open"], "Object IO must not hold a DB transaction"
        assert uow.commit.call_count == 1
        assert uow.gc_jobs.schedule.call_count == 1

    store.put.side_effect = put
    store.head.return_value = SimpleNamespace(size_bytes=len(png()), media_type="image/png")
    store.get_range.return_value = png()
    record = SimpleNamespace(
        blob_id=uuid4(), sha256="a" * 64, size_bytes=len(png()), media_type="image/png"
    )
    uow.blobs.find_ready_by_id.return_value = record
    return MaaImageResourceService(factory, store), uow, store, record


def test_stages_before_io_and_finalizes_verified_resource():
    service, uow, store, record = fixture_service()
    response = service.upload(uuid4(), png())
    assert response["resource"]["$blob"] == str(record.blob_id)
    assert response["width"] == 8
    assert uow.commit.call_count == 2
    uow.imports.acquire_finalize_lock.assert_called_once()
    store.put.assert_called_once()
    pending = uow.blobs.add_pending.call_args.args[0]
    assert uow.gc_jobs.schedule.call_args.args[1] == pending.blob_id
    uow.blobs.mark_ready.assert_called_once()


def test_duplicate_reuses_ready_blob_without_upload():
    service, uow, store, record = fixture_service()
    uow.blobs.find_ready_by_sha256.return_value = record
    assert service.upload(uuid4(), png())["resource"]["$blob"] == str(record.blob_id)
    store.put.assert_not_called()
    uow.blobs.add_pending.assert_not_called()


def test_failed_upload_leaves_scheduled_pending_cleanup():
    service, uow, store, _ = fixture_service()
    store.put.side_effect = OSError("storage unavailable")
    with pytest.raises(OSError):
        service.upload(uuid4(), png())
    assert uow.commit.call_count == 1
    uow.gc_jobs.schedule.assert_called_once()
    uow.blobs.mark_ready.assert_not_called()


def test_corrupted_storage_result_is_never_ready():
    service, uow, store, _ = fixture_service()
    store.get_range.return_value = b"corrupted"
    with pytest.raises(MaaDomainError):
        service.upload(uuid4(), png())
    uow.blobs.mark_ready.assert_not_called()


def test_missing_script_does_not_create_resource():
    service, uow, store, _ = fixture_service()
    uow.scripts.get_active.return_value = None
    with pytest.raises(MaaDomainError):
        service.upload(uuid4(), png())
    store.put.assert_not_called()
    uow.blobs.add_pending.assert_not_called()


def test_read_requires_version_reference_and_verifies_bytes():
    service, uow, store, record = fixture_service()
    script_id, version_id = uuid4(), uuid4()
    uow.script_versions = Mock()
    uow.script_versions.get.return_value = SimpleNamespace(script_id=script_id)
    uow.script_versions.has_blob_reference.return_value = True
    record.object_key = "test.png"
    record.sha256 = hashlib.sha256(png()).hexdigest()
    assert service.read(script_id, version_id, record.blob_id) == png()
    uow.script_versions.has_blob_reference.assert_called_once_with(version_id, record.blob_id)
    store.get_range.assert_called_once_with("test.png", 0, len(png()) - 1)
    store.get_range.return_value = b"wrong"
    with pytest.raises(MaaDomainError, match="checksum"):
        service.read(script_id, version_id, record.blob_id)


@pytest.mark.parametrize("same_script, referenced", [(False, True), (True, False)])
def test_read_refuses_unrelated_resource(same_script, referenced):
    service, uow, store, record = fixture_service()
    script_id = uuid4()
    uow.script_versions = Mock()
    uow.script_versions.get.return_value = SimpleNamespace(
        script_id=script_id if same_script else uuid4()
    )
    uow.script_versions.has_blob_reference.return_value = referenced
    with pytest.raises(MaaDomainError):
        service.read(script_id, uuid4(), record.blob_id)
    store.get_range.assert_not_called()
