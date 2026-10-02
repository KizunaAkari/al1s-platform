from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.maa_models import MaaApplicationRow, MaaScriptRow, MaaScriptVersionRow
from al1s.adapters.postgres.maa_unit_of_work import MaaSqlAlchemyUnitOfWork
from al1s.execution.definitions import canonical_manifest_hash
from al1s.infrastructure.readiness import ReadinessResult
from al1s.maa.errors import MaaDomainError
from al1s.maa.script_command_service import MaaScriptCommandService
from tests.integration.maa_support import clean_maa_tables as clean_maa_tables
from tests.integration.maa_support import engine as engine

pytestmark = pytest.mark.integration


class Ready:
    def check(self) -> ReadinessResult:
        return ReadinessResult(ready=True, dependencies={})

    def close(self) -> None:
        pass


def seed(engine):
    now = datetime.now(UTC)
    app, target, source, version = [uuid4() for _ in range(4)]
    with Session(engine) as session, session.begin():
        session.add(
            MaaApplicationRow(
                id=app,
                package_name="com.test.refs",
                display_name="refs",
                created_at=now,
                updated_at=now,
                row_version=1,
            )
        )
        session.flush()
        for script_id in (target, source):
            session.add(
                MaaScriptRow(
                    id=script_id,
                    application_id=app,
                    name=str(script_id),
                    normalized_name=str(script_id),
                    script_type="module_process",
                    status="active",
                    created_at=now,
                    updated_at=now,
                    row_version=1,
                )
            )
        session.flush()
        seed_versions(session, source, target, version, now)
    return target, source, version


def seed_versions(session, source, target, version, now):
    base = {
        "version": 2,
        "script_type": "module_process",
        "cleanup_on_finish": False,
        "target": {"application_package": "com.test.refs"},
    }
    target_version = uuid4()
    for script_id, version_id, step in (
        (target, target_version, {"action": "wait", "seconds": 1}),
        (
            source,
            version,
            {
                "action": "wait",
                "seconds": 1,
                "failure_retry": {
                    "enabled": True,
                    "process_script_id": str(target),
                    "max_retries": 1,
                },
            },
        ),
    ):
        manifest = {**base, "steps": [step]}
        session.add(
            MaaScriptVersionRow(
                id=version_id,
                script_id=script_id,
                revision=1,
                schema_version=2,
                created_at=now,
                manifest=manifest,
                manifest_hash=canonical_manifest_hash(manifest),
            )
        )
    session.flush()
    session.execute(
        update(MaaScriptRow)
        .where(MaaScriptRow.id == target)
        .values(
            current_version_id=target_version,
        )
    )
    session.execute(
        update(MaaScriptRow)
        .where(MaaScriptRow.id == source)
        .values(
            candidate_version_id=version,
            current_version_id=version,
        )
    )


def commands(engine):
    factory = sessionmaker(engine)
    return MaaScriptCommandService(lambda: MaaSqlAlchemyUnitOfWork(factory))


def remove(service, script_id, key="delete-ref-test"):
    return service.delete_script(
        script_id=script_id, expected_row_version=1, idempotency_key=key, correlation_id=uuid4()
    )


def test_reference_blocks_delete_and_history_survives(engine):
    target, source, version = seed(engine)
    service = commands(engine)
    with pytest.raises(MaaDomainError) as caught:
        remove(service, target)
    assert caught.value.code == "script_in_use"
    assert caught.value.context["referrers"][0]["step_indices"] == [1]
    with Session(engine) as session, session.begin():
        session.execute(
            update(MaaScriptRow).where(MaaScriptRow.id == source).values(candidate_version_id=None)
        )
    with pytest.raises(MaaDomainError):
        remove(service, target)
    with Session(engine) as session, session.begin():
        session.execute(
            update(MaaScriptRow).where(MaaScriptRow.id == source).values(current_version_id=None)
        )
    assert not remove(service, target).replayed
    assert remove(service, target).replayed
    with Session(engine) as session:
        assert session.get(MaaScriptRow, target).deleted_at is not None
        assert session.get(MaaScriptVersionRow, version) is not None


def test_reusing_old_candidate_cannot_reference_deleted_target(engine):
    target, source, version = seed(engine)
    with Session(engine) as session, session.begin():
        session.execute(
            update(MaaScriptRow)
            .where(MaaScriptRow.id == source)
            .values(
                current_version_id=None,
                candidate_version_id=None,
            )
        )
    remove(commands(engine), target)
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.execute(
            update(MaaScriptRow)
            .where(MaaScriptRow.id == source)
            .values(candidate_version_id=version)
        )
        session.commit()


def test_stale_delete_changes_nothing(engine):
    target, _, _ = seed(engine)
    with pytest.raises(MaaDomainError) as caught:
        commands(engine).delete_script(
            script_id=target,
            expected_row_version=99,
            idempotency_key="stale-delete",
            correlation_id=uuid4(),
        )
    assert caught.value.code == "stale_script"
    with Session(engine) as session:
        assert (
            session.scalar(select(MaaScriptRow.deleted_at).where(MaaScriptRow.id == target)) is None
        )


def test_candidate_waits_for_delete_and_rejects_deleted_target(engine):
    target, source, version = seed(engine)
    with Session(engine) as session, session.begin():
        session.execute(
            update(MaaScriptRow)
            .where(MaaScriptRow.id == source)
            .values(
                current_version_id=None,
                candidate_version_id=None,
            )
        )
    started = Event()

    def update_candidate():
        with Session(engine) as session:
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            started.set()
            with pytest.raises(IntegrityError):
                session.execute(
                    update(MaaScriptRow)
                    .where(MaaScriptRow.id == source)
                    .values(
                        candidate_version_id=version,
                    )
                )
                session.commit()

    with ThreadPoolExecutor(max_workers=1) as pool, Session(engine) as deleting:
        deleting.execute(select(MaaScriptRow).where(MaaScriptRow.id == target).with_for_update())
        future = pool.submit(update_candidate)
        assert started.wait(3)
        deleting.execute(
            update(MaaScriptRow)
            .where(MaaScriptRow.id == target)
            .values(deleted_at=datetime.now(UTC))
        )
        deleting.commit()
        future.result(timeout=8)


def test_http_reference_list_and_conflict(engine):
    import asyncio

    import httpx

    from al1s.app.config import Settings
    from al1s.app.factory import create_app

    target, source, _ = seed(engine)
    app = create_app(
        Settings(admin_password="deletion-test-password", admin_cookie_secure=False),
        readiness_service=Ready(),  # type: ignore[arg-type]
    )

    async def exercise():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"X-AL1S-CSRF": "1"},
            ) as client,
        ):
            login = await client.post(
                "/api/v1/auth/login",
                json={"password": "deletion-test-password"},
            )
            assert login.status_code == 200
            refs = await client.get(f"/api/v1/maa/scripts/{target}/referrers")
            assert refs.status_code == 200
            assert refs.json()["items"][0]["script_id"] == str(source)
            response = await client.delete(
                f"/api/v1/maa/scripts/{target}",
                headers={
                    "Idempotency-Key": "http-delete",
                    "If-Match": "1",
                },
            )
            assert response.status_code == 409
            assert response.json()["code"] == "script_in_use"

    asyncio.run(exercise())


def test_upgrade_backfills_existing_versions(engine):
    from alembic import command
    from alembic.config import Config

    from al1s.adapters.postgres.maa_reference_queries import script_referrers

    target, source, _ = seed(engine)
    config = Config("alembic.ini")
    try:
        command.downgrade(config, "20260909_0024")
    finally:
        command.upgrade(config, "head")
    with Session(engine) as session:
        refs = script_referrers(session, target, after_id=None, limit=50)
        assert refs == [{"script_id": str(source), "name": str(source), "step_indices": [1]}]
