from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.api.maa_import_selection import selection_header
from al1s.maa.archive import parse_script_archive
from al1s.maa.errors import MaaDomainError
from al1s.maa.import_selection import resolve_import
from al1s.maa.import_service import MaaArchiveImportService
from al1s.maa.types import ApplicationRecord, ScriptRecord, ScriptStatus, ScriptType
from tests.maa_archive_fixture import FixtureScript, build_archive


def make_case(kind="module_process", name="Incoming"):
    now = datetime.now(UTC)
    app = ApplicationRecord(uuid4(), "com.example.game", "Game", now, now, None, 1)
    document = {
        "version": 2,
        "script_type": kind,
        "target": {"application_package": app.package_name},
        "steps": [{"action": "wait", "seconds": 1}],
    }
    parsed = parse_script_archive(build_archive(FixtureScript(name, document)))
    target = ScriptRecord(
        uuid4(),
        app.application_id,
        name,
        name.casefold(),
        ScriptType(kind),
        ScriptStatus.ACTIVE,
        uuid4(),
        None,
        now,
        now,
        None,
        4,
    )
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.applications.find_import_targets.return_value = {app.package_name: [app]}
    uow.scripts.find_active_by_keys.return_value = {}
    uow.scripts.find_import_boundaries.return_value = {}
    return uow, parsed, app, target


def test_new_process_is_not_an_overwrite():
    uow, parsed, app, _ = make_case()
    result = resolve_import(uow, parsed)
    assert result.applications == {app.package_name: app}
    assert result.replacements == {}
    uow.scripts.add_many.assert_not_called()
    uow.commit.assert_not_called()


def test_same_name_needs_exact_confirmation_and_rejects_stale_version():
    uow, parsed, app, target = make_case()
    uow.scripts.find_active_by_keys.return_value = {
        (app.application_id, target.normalized_name): target,
    }
    with pytest.raises(MaaDomainError) as error:
        resolve_import(uow, parsed)
    assert error.value.code == "archive_overwrite_confirmation_required"
    assert error.value.context["overwrites"][0]["target_name"] == target.name
    for version in (target.row_version - 1, target.row_version + 1):
        with pytest.raises(MaaDomainError):
            resolve_import(uow, replace(parsed, confirmed_overwrites={target.script_id: version}))
    result = resolve_import(
        uow,
        replace(parsed, confirmed_overwrites={target.script_id: target.row_version}),
    )
    assert result.replacements == {1: target}
    uow.commit.assert_not_called()


@pytest.mark.parametrize("kind", ["module_start", "module_end"])
def test_boundary_uses_type_not_name_and_cannot_be_created(kind):
    uow, parsed, app, target = make_case(kind)
    with pytest.raises(MaaDomainError) as error:
        resolve_import(uow, parsed)
    assert error.value.code == "archive_boundary_missing"
    target = replace(target, name="Existing", normalized_name="existing")
    uow.scripts.find_import_boundaries.return_value = {
        (app.application_id, target.script_type): target
    }
    with pytest.raises(MaaDomainError) as error:
        resolve_import(uow, parsed)
    warning = error.value.context["overwrites"][0]
    assert (warning["incoming_name"], warning["target_name"]) == ("Incoming", "Existing")
    selected = replace(parsed, confirmed_overwrites={target.script_id: target.row_version})
    assert resolve_import(uow, selected).replacements[1] == target


def test_same_name_other_type_is_never_overwritten():
    uow, parsed, app, target = make_case()
    target = replace(target, script_type=ScriptType.MODULE_END)
    uow.scripts.find_active_by_keys.return_value = {
        (app.application_id, target.normalized_name): target,
    }
    with pytest.raises(MaaDomainError) as error:
        resolve_import(uow, parsed)
    assert error.value.code == "archive_script_type_conflict"


def test_missing_or_ambiguous_application_requires_selection():
    uow, parsed, app, _ = make_case()
    other = replace(app, application_id=uuid4())
    for choices in ([], [app, other]):
        uow.applications.find_import_targets.return_value = {app.package_name: choices}
        with pytest.raises(MaaDomainError) as error:
            resolve_import(uow, parsed)
        assert error.value.code == "archive_application_selection_required"
    selected = replace(parsed, target_applications={app.package_name: other.application_id})
    assert resolve_import(uow, selected).applications[app.package_name] == other
    uow.applications.add_many.assert_not_called()


def test_duplicate_boundary_target_and_extraneous_confirmation_rejected():
    uow, parsed, app, target = make_case("module_end")
    uow.scripts.find_import_boundaries.return_value = {
        (app.application_id, target.script_type): target
    }
    duplicate = replace(parsed.scripts[0], ordinal=2, name="Other")
    with pytest.raises(MaaDomainError) as error:
        resolve_import(uow, replace(parsed, scripts=(*parsed.scripts, duplicate)))
    assert error.value.code == "archive_duplicate_target"
    uow, parsed, _, _ = make_case()
    with pytest.raises(MaaDomainError):
        resolve_import(uow, replace(parsed, confirmed_overwrites={uuid4(): 1}))


def test_overwrite_builds_new_version_under_same_identity_and_name():
    uow, parsed, app, target = make_case()
    target = replace(target, name="Retained name")
    service = MaaArchiveImportService(lambda: uow, MagicMock())
    prepared = service._prepare_scripts(parsed)
    scripts, versions, _, _, items = service._build_script_facts(
        prepared,
        {app.package_name: app},
        {},
        uuid4(),
        uuid4(),
        datetime.now(UTC),
        replacements={1: target},
        revisions={target.script_id: 9},
    )
    assert scripts == [target]
    assert versions[0].script_id == target.script_id
    assert versions[0].revision == 9
    assert versions[0].script_version_id != target.current_version_id
    assert items[0].script_id == target.script_id


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        '{"unknown": true}',
        '{"confirmed_overwrites": {"not-a-uuid": 1}}',
        '{"confirmed_overwrites": {"00000000-0000-0000-0000-000000000001": true}}',
    ],
)
def test_selection_header_rejects_invalid_input(value):
    with pytest.raises(MaaDomainError) as error:
        selection_header(value)
    assert error.value.status_code == 422


def test_archive_type_mismatch_is_rejected_before_writes():
    uow, parsed, _, _ = make_case()
    parsed = replace(parsed, scripts=(replace(parsed.scripts[0], script_type="module_end"),))
    with pytest.raises(MaaDomainError) as error:
        MaaArchiveImportService(lambda: uow, MagicMock())._prepare_scripts(parsed)
    assert error.value.code == "archive_script_type_mismatch"
    uow.commit.assert_not_called()


def test_edited_end_without_default_cleanup_is_importable():
    uow, parsed, _, _ = make_case("module_end")
    prepared = MaaArchiveImportService(lambda: uow, MagicMock())._prepare_scripts(parsed)
    assert prepared[0].script_type is ScriptType.MODULE_END
    assert "cleanup_on_finish" not in prepared[0].document


@pytest.mark.parametrize("update_count", [0, 1])
def test_persistence_retains_identity_and_uses_cas(update_count):
    from al1s.maa.import_persistence import persist_imported_scripts

    uow, parsed, app, target = make_case()
    service = MaaArchiveImportService(lambda: uow, MagicMock())
    facts = service._build_script_facts(
        service._prepare_scripts(parsed),
        {app.package_name: app},
        {},
        uuid4(),
        uuid4(),
        datetime.now(UTC),
        replacements={1: target},
        revisions={target.script_id: 9},
    )
    scripts, versions, refs, qualifications, _ = facts
    uow.scripts.activate_imported_versions.return_value = 0
    uow.scripts.replace_imported_versions.return_value = update_count
    if update_count:
        persist_imported_scripts(
            uow,
            scripts,
            versions,
            refs,
            qualifications,
            {1: target},
            datetime.now(UTC),
        )
    else:
        with pytest.raises(MaaDomainError) as error:
            persist_imported_scripts(
                uow,
                scripts,
                versions,
                refs,
                qualifications,
                {1: target},
                datetime.now(UTC),
            )
        assert error.value.code == "archive_overwrite_stale"
    uow.scripts.add_many.assert_called_once_with([])
    assert uow.scripts.replace_imported_versions.call_args.args[0] == [
        (target.script_id, target.row_version, versions[0].script_version_id),
    ]
    uow.commit.assert_not_called()


def test_unconfirmed_import_never_starts_batch_or_uploads_resources():
    uow, parsed, app, target = make_case()
    uow.imports.find_by_logical_hash.return_value = None
    uow.scripts.find_active_by_keys.return_value = {
        (app.application_id, target.normalized_name): target,
    }
    store = MagicMock()
    service = MaaArchiveImportService(lambda: uow, store)
    content = build_archive(FixtureScript("Incoming", parsed.scripts[0].document))
    with pytest.raises(MaaDomainError):
        service.import_archive(content, correlation_id=uuid4())
    uow.imports.add.assert_not_called()
    uow.script_versions.add_many.assert_not_called()
    store.put.assert_not_called()
    uow.commit.assert_not_called()
