"""Saved script display order against an explicitly disposable PostgreSQL database."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.maa_models import MaaApplicationRow, MaaScriptRow, MaaScriptVersionRow
from al1s.adapters.postgres.maa_unit_of_work import MaaSqlAlchemyUnitOfWork
from al1s.maa.catalog_command_service import MaaCatalogCommandService
from al1s.maa.catalog_service import MaaCatalogQueryService
from al1s.maa.errors import MaaDomainError
from tests.integration.maa_support import clean_maa_tables as clean_maa_tables
from tests.integration.maa_support import engine as engine

pytestmark = pytest.mark.integration


def _seed(engine, *, package: str, count: int) -> tuple[UUID, list[UUID]]:
    now = datetime.now(UTC)
    application_id = uuid4()
    script_ids = [uuid4() for _ in range(count)]
    with Session(engine) as session, session.begin():
        session.add(MaaApplicationRow(
            id=application_id, package_name=package, display_name=package,
            created_at=now, updated_at=now, row_version=1,
        ))
        session.flush()
        for index, script_id in enumerate(script_ids):
            session.add(MaaScriptRow(
                id=script_id, application_id=application_id, name=f"Script {index}",
                normalized_name=f"script {index}", script_type="module_process",
                status="active", created_at=now, updated_at=now, row_version=1,
            ))
        session.flush()
        for script_id in script_ids:
            version_id = uuid4()
            session.add(MaaScriptVersionRow(
                id=version_id, script_id=script_id, revision=1, schema_version=2,
                manifest_hash="a" * 64, manifest={}, created_at=now,
            ))
            session.flush()
            session.execute(update(MaaScriptRow).where(MaaScriptRow.id == script_id).values(
                current_version_id=version_id,
            ))
    return application_id, script_ids


def test_reorder_persists_across_pages_and_replay(engine):
    application_id, _ = _seed(engine, package="com.example.order", count=4)
    def factory():
        return MaaSqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))
    catalog = MaaCatalogQueryService(factory)
    commands = MaaCatalogCommandService(factory)
    original = catalog.list_library_scripts(application_id=application_id, after_id=None, limit=10)
    moved_id = original[-1].script_id
    first_id = original[0].script_id
    key = str(uuid4())
    command = dict(
        application_id=application_id, script_id=moved_id, target_id=first_id,
        placement="before", idempotency_key=key, correlation_id=uuid4(),
    )
    assert not commands.reorder_script(**command).replayed
    assert commands.reorder_script(**command).replayed
    first_page = catalog.list_library_scripts(application_id=application_id, after_id=None, limit=2)
    second_page = catalog.list_library_scripts(
        application_id=application_id, after_id=first_page[-1].script_id, limit=2,
    )
    assert [item.script_id for item in first_page + second_page] == [
        moved_id, *[item.script_id for item in original[:-1]],
    ]
    with Session(engine) as session:
        version = session.scalar(
            select(MaaScriptRow.row_version).where(MaaScriptRow.id == moved_id)
        )
        assert version == 1
    commands.reorder_script(
        application_id=application_id, script_id=moved_id,
        target_id=original[0].script_id, placement="after",
        idempotency_key=str(uuid4()), correlation_id=uuid4(),
    )
    assert [item.script_id for item in catalog.list_library_scripts(
        application_id=application_id, after_id=None, limit=10,
    )] == [original[0].script_id, moved_id, original[1].script_id, original[2].script_id]
    commands.reorder_script(
        application_id=application_id, script_id=moved_id,
        target_id=original[2].script_id, placement="after",
        idempotency_key=str(uuid4()), correlation_id=uuid4(),
    )
    assert [item.script_id for item in catalog.list_library_scripts(
        application_id=application_id, after_id=None, limit=10,
    )] == [item.script_id for item in original]


def test_reorder_rejects_other_application_and_appends_new_script(engine):
    application_id, _ = _seed(engine, package="com.example.one", count=2)
    other_id, _ = _seed(engine, package="com.example.two", count=1)
    def factory():
        return MaaSqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))
    catalog = MaaCatalogQueryService(factory)
    commands = MaaCatalogCommandService(factory)
    own = catalog.list_library_scripts(application_id=application_id, after_id=None, limit=10)
    other = catalog.list_library_scripts(application_id=other_id, after_id=None, limit=10)
    with pytest.raises(MaaDomainError) as error:
        commands.reorder_script(
            application_id=application_id, script_id=own[0].script_id,
            target_id=other[0].script_id, placement="after",
            idempotency_key=str(uuid4()), correlation_id=uuid4(),
        )
    assert error.value.status_code == 409
    commands.reorder_script(
        application_id=application_id, script_id=own[1].script_id,
        target_id=own[0].script_id, placement="before",
        idempotency_key=str(uuid4()), correlation_id=uuid4(),
    )
    now = datetime.now(UTC)
    new_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add(MaaScriptRow(
            id=new_id, application_id=application_id, name="New", normalized_name="new",
            script_type="module_process", status="active",
            created_at=now, updated_at=now, row_version=1,
        ))
        session.flush()
        version_id = uuid4()
        session.add(MaaScriptVersionRow(
            id=version_id, script_id=new_id, revision=1, schema_version=2,
            manifest_hash="b" * 64, manifest={}, created_at=now,
        ))
        session.flush()
        session.execute(update(MaaScriptRow).where(MaaScriptRow.id == new_id).values(
            current_version_id=version_id,
        ))
    assert [item.script_id for item in catalog.list_library_scripts(
        application_id=application_id, after_id=None, limit=10,
    )] == [own[1].script_id, own[0].script_id, new_id]
