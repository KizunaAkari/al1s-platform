from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from al1s.execution.definitions import canonical_manifest_hash
from al1s.maa.errors import MaaDomainError
from al1s.maa.publication_service import MaaScriptPublicationService
from al1s.maa.script_command_service import MaaScriptCommandService
from al1s.maa.types import ScriptRecord, ScriptStatus, ScriptType, ScriptVersionRecord


def test_new_editor_rejects_unmigrated_candidate_before_any_write():
    application_id, script_id = uuid4(), uuid4()
    uow = SimpleNamespace(scripts=Mock(), applications=Mock(), application_devices=Mock())
    uow.scripts.get_active.return_value = SimpleNamespace(
        script_id=script_id,
        application_id=application_id,
        script_type=ScriptType.MODULE_PROCESS,
        candidate_version_id=uuid4(),
    )
    uow.applications.get_active.return_value = SimpleNamespace(
        application_id=application_id,
        package_name="com.example.game",
    )
    service = MaaScriptCommandService(lambda: uow)
    with pytest.raises(MaaDomainError) as error:
        service._save_document(
            uow,
            script_id=script_id,
            expected_row_version=1,
            manifest={"steps": [{"action": "wait", "seconds": 1}]},
            device_id=uuid4(),
            correlation_id=uuid4(),
        )
    assert error.value.code == "legacy_candidate_requires_migration"
    uow.application_devices.for_phone_package.assert_not_called()


NOW = datetime(2026, 9, 30, tzinfo=UTC)
DOCUMENT = {
    "version": 2,
    "script_type": "module_process",
    "target": {"application_package": "com.example.game"},
    "steps": [{"action": "wait", "seconds": 1}],
}


def save_context():
    script = ScriptRecord(
        uuid4(),
        uuid4(),
        "main",
        "main",
        ScriptType.MODULE_PROCESS,
        ScriptStatus.VALIDATION_PENDING,
        None,
        None,
        NOW,
        NOW,
        None,
        2,
    )
    application = SimpleNamespace(
        application_id=script.application_id, package_name="com.example.game"
    )
    uow = SimpleNamespace(
        scripts=Mock(),
        script_versions=Mock(),
        qualifications=Mock(),
        blobs=Mock(),
        audit=Mock(),
        outbox=Mock(),
        flush=Mock(),
    )
    uow.script_versions.find_by_hash.return_value = None
    uow.script_versions.next_revision.return_value = 1
    uow.qualifications.find_by_idempotency.return_value = None
    uow.scripts.activate_saved_version.side_effect = lambda sid, rv, vid, now: replace(
        script, current_version_id=vid, row_version=rv + 1, status=ScriptStatus.ACTIVE
    )
    return script, application, uow, MaaScriptPublicationService(lambda: uow)


def save(service, uow, script, application, **overrides):
    args = dict(
        script=script,
        application=application,
        expected_script_version=2,
        manifest=DOCUMENT,
        correlation_id=uuid4(),
        now=NOW,
        source="document_save",
    )
    args.update(overrides)
    return service.save_current_document_in_uow(uow, **args)


def test_post_wait_survives_save_and_execution_compilation():
    from al1s.maa.compiler import MaaRegisteredActionCompiler

    script, application, uow, service = save_context()
    document = {**DOCUMENT, "steps": [
        {"action": "wait_text", "text": "领取", "wait_after_execution_seconds": 2.75},
    ]}
    save(service, uow, script, application, manifest=document)
    version = uow.script_versions.add_many.call_args.args[0][0]
    assert version.manifest == document
    assert version.manifest_hash == canonical_manifest_hash(document)
    compiled = MaaRegisteredActionCompiler().compile(version, [])
    assert compiled.definition["steps"][0]["wrappers"][0]["parameters"] == {"seconds": 2.75}


def test_image_stability_survives_save_with_blob_binding_and_execution_compilation():
    from al1s.maa.compiler import MaaRegisteredActionCompiler

    script, application, uow, service = save_context()
    blob_id = uuid4()
    blob = SimpleNamespace(blob_id=blob_id, media_type="image/png", size_bytes=10)
    uow.blobs.find_ready_by_sha256_many.return_value = {"a" * 64: blob}
    document = {**DOCUMENT, "steps": [{
        "action": "wait_image", "consecutive_match_count": 3,
        "template_base64": {"$blob": str(blob_id), "sha256": "a" * 64,
                            "media_type": "image/png", "size_bytes": 10},
    }]}
    saved = save(service, uow, script, application, manifest=document)
    version = uow.script_versions.add_many.call_args.args[0][0]
    references = uow.script_versions.add_blob_references.call_args.args[0]
    assert saved.current_version_id == version.script_version_id
    assert version.manifest == document
    assert version.manifest_hash == canonical_manifest_hash(document)
    compiled = MaaRegisteredActionCompiler().compile(version, references).definition["steps"][0]
    assert compiled["parameters"]["template_base64"]["blob_id"] == str(blob_id)
    assert compiled["wrappers"][0]["parameters"] == {"consecutive_match_count": 3}


def test_current_save_prepares_real_version_and_activates_with_one_cas():
    script, application, uow, service = save_context()
    result = save(service, uow, script, application)
    version = uow.script_versions.add_many.call_args.args[0][0]
    assert version.manifest == DOCUMENT
    assert version.manifest_hash == canonical_manifest_hash(DOCUMENT)
    assert result.current_version_id == version.script_version_id
    uow.scripts.activate_saved_version.assert_called_once_with(
        script.script_id, 2, version.script_version_id, NOW
    )
    uow.scripts.set_candidate.assert_not_called()
    uow.scripts.publish_candidate.assert_not_called()
    uow.qualifications.has_passed.assert_not_called()
    assert [c.args[0].event_type for c in uow.outbox.add.call_args_list] == [
        "maa.script.version-published.v1"
    ]
    assert [c.args[0].action for c in uow.audit.add.call_args_list] == ["maa.script.publish"]


def test_current_save_reuses_an_immutable_version():
    script, application, uow, service = save_context()
    version = ScriptVersionRecord(
        uuid4(), script.script_id, 1, 2, canonical_manifest_hash(DOCUMENT), DOCUMENT, NOW
    )
    uow.script_versions.find_by_hash.return_value = version
    result = save(service, uow, script, application)
    assert result.current_version_id == version.script_version_id
    uow.script_versions.add_many.assert_not_called()
    uow.script_versions.add_blob_references.assert_not_called()


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"expected_script_version": 1}, "stale_script"),
        ({"manifest": {**DOCUMENT, "steps": [{"action": "not_supported"}]}}, "unregistered_action"),
    ],
)
def test_current_save_rejects_before_writing(overrides, code):
    script, application, uow, service = save_context()
    with pytest.raises(MaaDomainError) as error:
        save(service, uow, script, application, **overrides)
    assert error.value.code == code
    uow.script_versions.add_many.assert_not_called()
    uow.scripts.activate_saved_version.assert_not_called()
    uow.outbox.add.assert_not_called()


def test_failed_activation_does_not_emit_publication():
    script, application, uow, service = save_context()
    uow.scripts.activate_saved_version.side_effect = None
    uow.scripts.activate_saved_version.return_value = None
    with pytest.raises(MaaDomainError, match="concurrently"):
        save(service, uow, script, application)
    uow.outbox.add.assert_not_called()
    uow.audit.add.assert_not_called()


def test_legacy_candidate_still_emits_its_own_validation_state():
    script, application, uow, service = save_context()
    uow.scripts.set_candidate.side_effect = lambda sid, rv, vid, now: replace(
        script, candidate_version_id=vid, row_version=rv + 1
    )
    staged = service.stage_candidate(
        uow,
        script=script,
        application=application,
        expected_script_version=2,
        manifest=DOCUMENT,
        correlation_id=uuid4(),
        now=NOW,
        source="candidate_save",
    )
    assert staged.script.current_version_id is None
    assert staged.script.candidate_version_id == staged.version.script_version_id
    assert uow.outbox.add.call_args.args[0].payload["state"] == "awaiting_quick_test"
    assert uow.outbox.add.call_args.args[0].event_type == "maa.script.validation-state-changed.v1"
    uow.scripts.activate_saved_version.assert_not_called()
