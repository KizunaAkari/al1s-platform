from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.maa_models import MaaApplicationRow, MaaScriptRow, MaaScriptVersionRow
from al1s.adapters.postgres.maa_script_repository import PostgresMaaScriptRepository
from al1s.adapters.postgres.maa_unit_of_work import MaaSqlAlchemyUnitOfWork
from al1s.adapters.postgres.models import AuditLogRow, OutboxEventRow
from al1s.maa.errors import MaaDomainError
from al1s.maa.publication_service import MaaScriptPublicationService
from tests.integration.maa_support import clean_maa_tables as clean_maa_tables
from tests.integration.maa_support import engine as engine

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 30, tzinfo=UTC)
DOCUMENT = {
    "version": 2,
    "script_type": "module_process",
    "target": {"application_package": "com.example.save"},
    "steps": [{"action": "wait", "seconds": 1}],
}


def seed(engine):
    app_id, script_id = uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            MaaApplicationRow(
                id=app_id,
                package_name="com.example.save",
                display_name="Save",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        session.add(
            MaaScriptRow(
                id=script_id,
                application_id=app_id,
                name="main",
                normalized_name="main",
                script_type="module_process",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    return app_id, script_id


def save(engine, app_id, script_id, expected=1):
    def factory():
        return MaaSqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))

    service = MaaScriptPublicationService(factory)
    with factory() as uow:
        result = service.save_current_document_in_uow(
            uow,
            script=uow.scripts.get_active(script_id),
            application=uow.applications.get_active(app_id),
            expected_script_version=expected,
            manifest=DOCUMENT,
            correlation_id=uuid4(),
            now=NOW,
            source="document_save",
        )
        uow.commit()
        return result


def test_direct_save_reuse_and_single_update_without_candidate_event(engine):
    app_id, script_id = seed(engine)
    updates = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("UPDATE maa_scripts"):
            updates.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        first = save(engine, app_id, script_id)
        second = save(engine, app_id, script_id, 2)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert first.row_version == 2 and second.row_version == 3
    assert first.current_version_id == second.current_version_id
    assert len(updates) == 2
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(MaaScriptVersionRow)) == 1
        assert session.get(MaaScriptRow, script_id).candidate_version_id is None
        assert session.scalars(select(OutboxEventRow.event_type)).all() == [
            "maa.script.version-published.v1",
            "maa.script.version-published.v1",
        ]


def test_failed_activation_rolls_back_version_and_receipt(engine, monkeypatch):
    app_id, script_id = seed(engine)
    monkeypatch.setattr(PostgresMaaScriptRepository, "activate_saved_version", lambda *args: None)
    with pytest.raises(MaaDomainError) as error:
        save(engine, app_id, script_id)
    assert error.value.code == "stale_script"
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(MaaScriptVersionRow)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxEventRow)) == 0
        assert session.scalar(select(func.count()).select_from(AuditLogRow)) == 0
        script = session.get(MaaScriptRow, script_id)
        assert script.row_version == 1 and script.current_version_id is None
