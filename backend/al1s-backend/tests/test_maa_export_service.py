from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest

from al1s.execution.definitions import canonical_manifest_hash
from al1s.maa.archive import parse_script_archive
from al1s.maa.errors import MaaDomainError
from al1s.maa.export_service import MaaScriptExportService
from al1s.maa.import_service import MaaArchiveImportService
from al1s.maa.types import ScriptType


def fixture():
    application, root, recovery = uuid4(), uuid4(), uuid4()
    root_version, recovery_version = uuid4(), uuid4()
    documents = {
        root: {
            "version": 2,
            "script_type": "module_process",
            "cleanup_on_finish": False,
            "target": {"application_package": "com.example.game"},
            "steps": [
                {
                    "action": "wait",
                    "seconds": 1,
                    "failure_retry": {
                        "enabled": False,
                        "process_script_id": str(recovery),
                        "max_retries": 1,
                    },
                }
            ],
        },
        recovery: {
            "version": 2,
            "script_type": "module_process",
            "cleanup_on_finish": False,
            "target": {"application_package": "com.example.game"},
            "steps": [{"action": "wait", "seconds": 1}],
        },
    }
    records = {
        i: SimpleNamespace(
            script_id=i,
            application_id=application,
            name=n,
            script_type=ScriptType.MODULE_PROCESS,
            current_version_id=v,
            candidate_version_id=None,
        )
        for i, n, v in [(root, "Main", root_version), (recovery, "Recovery", recovery_version)]
    }
    versions = {
        v: SimpleNamespace(
            script_version_id=v,
            script_id=i,
            manifest=documents[i],
            manifest_hash=canonical_manifest_hash(documents[i]),
        )
        for i, v in [(root, root_version), (recovery, recovery_version)]
    }
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.scripts.get_active.return_value = records[root]
    uow.script_versions.get.return_value = versions[root_version]
    uow.applications.get_active.return_value = SimpleNamespace(
        package_name="com.example.game", display_name="Game"
    )
    uow.scripts.find_active_by_ids.side_effect = lambda ids: {i: records[i] for i in ids}
    uow.script_versions.find_by_ids.side_effect = lambda ids: {i: versions[i] for i in ids}
    uow.script_versions.list_blob_references_many.return_value = {}
    uow.blobs.find_ready_by_ids.return_value = {}
    return MaaScriptExportService(lambda: uow, Mock()), uow, root, root_version, recovery


def test_export_deadline_prevents_late_success():
    service, _uow, root, version, _ = fixture()
    readings = iter([0, 0, 180])
    service._clock = lambda: next(readings)
    with pytest.raises(MaaDomainError) as error:
        service.export(root, version)
    assert error.value.code == "archive_export_timeout"


def test_export_includes_disabled_recovery_and_import_remaps_identity():
    service, _uow, root, version, recovery = fixture()
    parsed = parse_script_archive(service.export(root, version))
    assert {s.source_script_id for s in parsed.scripts} == {str(root), str(recovery)}
    importer = MaaArchiveImportService(Mock(), Mock())
    prepared = importer._prepare_scripts(parsed)
    scripts, versions, _, receipts, _ = importer._build_script_facts(
        prepared,
        {"com.example.game": SimpleNamespace(application_id=uuid4())},
        {},
        uuid4(),
        uuid4(),
        datetime.now(UTC),
    )
    identities = {s.name: s.script_id for s in scripts}
    main = next(v for v in versions if v.script_id == identities["Main"])
    assert main.manifest["steps"][0]["failure_retry"]["process_script_id"] == str(
        identities["Recovery"]
    )
    assert identities["Recovery"] != recovery
    assert all(s.current_version_id is None for s in scripts)
    assert all(r.kind.value == "static_check" for r in receipts)
    assert main.manifest_hash == canonical_manifest_hash(main.manifest)


def test_missing_recovery_fails_export_not_partial_zip():
    service, uow, root, version, _ = fixture()
    original = uow.scripts.find_active_by_ids.side_effect
    uow.scripts.find_active_by_ids.side_effect = lambda ids: original(ids) if root in ids else {}
    with pytest.raises(MaaDomainError, match="Recovery scripts"):
        service.export(root, version)


@pytest.mark.parametrize("kind", ["module_start", "module_process", "module_end"])
def test_exports_each_saved_script_type_with_name_and_package(kind):
    service, uow, root, version_id, _ = fixture()
    version = uow.script_versions.get.return_value
    version.manifest["script_type"] = kind
    uow.scripts.get_active.return_value.script_type = ScriptType(kind)
    version.manifest["steps"] = [{"action": "wait", "seconds": 1}]
    version.manifest_hash = canonical_manifest_hash(version.manifest)
    parsed = parse_script_archive(service.export(root, version_id))
    assert len(parsed.scripts) == 1
    script = parsed.scripts[0]
    assert script.script_type == kind
    assert script.name == "Main"
    assert script.application_package == "com.example.game"


def test_checksum_mismatch_rejects_export():
    service, uow, root, version, _ = fixture()
    uow.script_versions.get.return_value.manifest_hash = "0" * 64
    with pytest.raises(MaaDomainError, match="checksum"):
        service.export(root, version)
