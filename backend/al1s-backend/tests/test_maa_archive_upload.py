from io import BytesIO
from unittest.mock import Mock
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from al1s.maa import archive_upload
from al1s.maa.archive import parse_script_archive
from al1s.maa.errors import MaaDomainError
from al1s.maa.import_command_service import MaaArchiveImportCommandService


def bundle():
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("test", b"a" * 1000)
    return output.getvalue()


@pytest.mark.parametrize("body", [b"", b"not a zip", b"PK"])
def test_invalid_upload(body):
    with pytest.raises(MaaDomainError):
        archive_upload.validate_upload_size(body)


def test_expansion_is_bounded_before_import(monkeypatch):
    monkeypatch.setattr(archive_upload, "MAX_EXPANDED_BYTES", 999)
    with pytest.raises(MaaDomainError, match="expanded"):
        archive_upload.validate_upload_size(bundle())


def test_compressed_size_is_bounded(monkeypatch):
    monkeypatch.setattr(archive_upload, "MAX_UPLOAD_BYTES", 4)
    with pytest.raises(MaaDomainError):
        archive_upload.validate_upload_size(bundle())


def test_malformed_metadata_is_a_validation_error():
    import json

    from tests.maa_archive_fixture import FixtureScript, build_archive

    valid = build_archive(
        FixtureScript("Malformed", {"script_type": "module_process", "steps": []})
    )
    output = BytesIO()
    with ZipFile(BytesIO(valid)) as source, ZipFile(output, "w") as target:
        for name in source.namelist():
            body = source.read(name)
            if name == "manifest.json":
                manifest = json.loads(body)
                del manifest["source"]
                body = json.dumps(manifest).encode()
            target.writestr(name, body)
    with pytest.raises(MaaDomainError) as error:
        parse_script_archive(output.getvalue())
    assert error.value.status_code == 422


def test_upload_reuses_importer_without_outer_transaction():
    factory, importer = Mock(), Mock()
    service = MaaArchiveImportCommandService(factory, Mock(), importer)
    correlation = uuid4()
    content = bundle()
    assert (
        service.import_uploaded_archive(content, correlation_id=correlation)
        is importer.import_archive.return_value
    )
    importer.import_archive.assert_called_once_with(content, correlation_id=correlation)
    factory.assert_not_called()
