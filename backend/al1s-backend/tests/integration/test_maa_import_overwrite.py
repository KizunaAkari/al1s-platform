"""Run only against the explicitly supplied disposable PostgreSQL test database."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.maa_models import (
    MaaApplicationRow,
    MaaScriptRow,
    MaaScriptVersionRow,
)
from al1s.maa.errors import MaaDomainError
from tests.integration.maa_support import (
    bundle,
    setup_import,
)
from tests.integration.maa_support import (
    clean_maa_tables as clean_maa_tables,
)
from tests.integration.maa_support import (
    engine as engine,
)

pytestmark = pytest.mark.integration






@pytest.mark.parametrize("kind", ["module_process", "module_start", "module_end"])
def test_confirmed_overwrite_keeps_identity_history_and_replays(engine, kind):
    service, app, target = setup_import(engine, kind)
    name = "Saved" if kind == "module_process" else "Incoming different name"
    content = bundle(name, kind)
    with pytest.raises(MaaDomainError) as error:
        service.import_archive(content, correlation_id=uuid4())
    assert error.value.code == "archive_overwrite_confirmation_required"
    options = dict(
        target_applications={app.package_name: app.application_id},
        confirmed_overwrites={target.script_id: 1},
    )
    result = service.import_archive(content, correlation_id=uuid4(), **options)
    assert service.import_archive(content, correlation_id=uuid4(), **options).replayed
    with Session(engine) as session:
        record = session.get(MaaScriptRow, target.script_id)
        old_version = record.current_version_id
        assert record.name == "Saved" and record.row_version == 2
        assert session.scalar(select(func.count()).select_from(MaaScriptRow)) == 1
    newer = bundle(name, kind, seconds=2)
    options["confirmed_overwrites"] = {target.script_id: 2}
    service.import_archive(newer, correlation_id=uuid4(), **options)
    with Session(engine) as session:
        record = session.get(MaaScriptRow, target.script_id)
        assert record.current_version_id != old_version
        assert session.get(MaaScriptVersionRow, old_version) is not None
        assert session.scalar(select(func.count()).select_from(MaaScriptVersionRow)) == 2
    assert result.batch.status.value == "completed"


def test_new_process_and_missing_category(engine):
    service, app, _ = setup_import(engine)
    service.import_archive(bundle("New"), correlation_id=uuid4())
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(MaaScriptRow)) == 2
        row = session.get(MaaApplicationRow, app.application_id)
        row.deleted_at = datetime.now(UTC)
        session.commit()
    with pytest.raises(MaaDomainError) as error:
        service.import_archive(bundle("Other"), correlation_id=uuid4())
    assert error.value.code == "archive_application_selection_required"


def test_edit_between_prepare_and_finish_does_not_overwrite(engine):
    service, _, target = setup_import(engine)
    parsed, batch, _ = service.prepare_archive(
        bundle(),
        confirmed_overwrites={target.script_id: 1},
    )
    with Session(engine) as session:
        row = session.get(MaaScriptRow, target.script_id)
        row.row_version += 1
        session.commit()
    with pytest.raises(MaaDomainError) as error:
        service.finish_archive(parsed, batch, correlation_id=uuid4())
    assert error.value.code == "archive_overwrite_confirmation_required"
    with Session(engine) as session:
        assert session.get(MaaScriptRow, target.script_id).current_version_id is None
        assert session.scalar(select(func.count()).select_from(MaaScriptVersionRow)) == 0


def test_failure_after_version_write_rolls_back_all_scripts(engine, monkeypatch):
    service, _, target = setup_import(engine)
    from al1s.adapters.postgres.maa_repositories import PostgresMaaScriptRepository

    monkeypatch.setattr(PostgresMaaScriptRepository, "replace_imported_versions", lambda *a: 0)
    with pytest.raises(MaaDomainError) as error:
        service.import_archive(
            bundle(),
            correlation_id=uuid4(),
            confirmed_overwrites={target.script_id: 1},
        )
    assert error.value.code == "archive_overwrite_stale"
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(MaaScriptVersionRow)) == 0
        assert session.get(MaaScriptRow, target.script_id).row_version == 1
