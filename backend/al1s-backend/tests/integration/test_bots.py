from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine, create_engine, event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.bot_models import (
    BotConfigApplicationRow,
    BotConfigVersionRow,
    BotHealthReportRow,
    BotRegistrationGrantRow,
    BotServiceRow,
    BotWorkerIdentityRow,
)
from al1s.adapters.postgres.bot_unit_of_work import BotSqlAlchemyUnitOfWork
from al1s.adapters.postgres.notification_models import EncryptedSecretRow
from al1s.app.config import Settings
from al1s.app.factory import create_app
from al1s.bots.errors import BotAuthenticationError, BotDomainError
from al1s.bots.ports import BotUnitOfWork
from al1s.bots.service import BotService
from al1s.bots.types import BotConfigApplicationStatus, BotHealthStatus, BotServiceKind
from al1s.infrastructure.readiness import ReadinessResult
from al1s.secrets.security import FernetSecretCipher

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    database_url = os.getenv("AL1S_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("AL1S_TEST_DATABASE_URL is required for integration tests")
    database_engine = create_engine(database_url, pool_pre_ping=True)
    with database_engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    yield database_engine
    database_engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(engine: Engine) -> Iterator[None]:
    tables = (
        "bot_health_reports, bot_config_applications, bot_registration_grants, "
        "bot_worker_identities, bot_config_versions, bot_services, encrypted_secrets, "
        "audit_logs, outbox_events"
    )
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))
    yield
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))


def _service(engine: Engine, clock: list[datetime]) -> BotService:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def uow_factory() -> BotUnitOfWork:
        return cast(BotUnitOfWork, BotSqlAlchemyUnitOfWork(sessions))

    cipher = FernetSecretCipher("stage7-bot-master-key-that-is-at-least-32-bytes")
    return BotService(uow_factory, cipher, cipher, now=lambda: clock[0])


def _create_pair(service: BotService) -> tuple[UUID, UUID]:
    gateway_id = uuid4()
    bridge_id = uuid4()
    service.create_service(
        service_id=gateway_id,
        kind=BotServiceKind.ONEBOT_GATEWAY,
        name="Primary OneBot",
        correlation_id=uuid4(),
    )
    service.create_service(
        service_id=bridge_id,
        kind=BotServiceKind.DISCORD_BRIDGE,
        name="Primary Discord bridge",
        correlation_id=uuid4(),
    )
    return gateway_id, bridge_id


def _register(service: BotService, service_id: UUID) -> str:
    grant = service.create_registration_grant(
        service_id=service_id,
        ttl=timedelta(minutes=10),
        correlation_id=uuid4(),
    )
    return service.register_worker(
        registration_code=grant.registration_code,
        correlation_id=uuid4(),
    ).credential


def _apply_onebot(service: BotService, service_id: UUID, credential: str | None = None) -> str:
    worker_credential = credential or _register(service, service_id)
    submitted = service.submit_config(
        service_id=service_id,
        config_version_id=uuid4(),
        application_id=uuid4(),
        expected_version=service.get_service(service_id).row_version,
        settings={"ONEBOT_BASE_URL": "http://llonebot:3000"},
        secret="onebot-access-token",
        onebot_service_id=None,
        correlation_id=uuid4(),
    )
    service.settle_config_application(
        credential=worker_credential,
        application_id=submitted.application.application_id,
        receipt_event_id=uuid4(),
        status=BotConfigApplicationStatus.APPLIED,
        worker_instance_id="onebot-worker-1",
        error_code=None,
        error_summary=None,
        correlation_id=uuid4(),
    )
    return worker_credential


def test_worker_registration_is_one_time_and_persists_only_digests(engine: Engine) -> None:
    clock = [datetime(2026, 9, 5, 13, tzinfo=UTC)]
    service = _service(engine, clock)
    _gateway_id, bridge_id = _create_pair(service)
    grant = service.create_registration_grant(
        service_id=bridge_id,
        ttl=timedelta(minutes=10),
        correlation_id=uuid4(),
    )
    worker = service.register_worker(
        registration_code=grant.registration_code,
        correlation_id=uuid4(),
    )

    with pytest.raises(BotDomainError) as replay:
        service.register_worker(
            registration_code=grant.registration_code,
            correlation_id=uuid4(),
        )
    assert replay.value.code == "bot_registration_code_consumed"

    with Session(engine) as session:
        grant_row = session.get(BotRegistrationGrantRow, grant.grant_id)
        identity = session.get(BotWorkerIdentityRow, worker.identity_id)
        assert grant_row is not None and identity is not None
        assert grant.registration_code not in grant_row.secret_digest
        assert worker.credential not in identity.credential_digest
        assert grant_row.consumed_by_identity_id == worker.identity_id


def test_desired_and_applied_versions_are_distinct_and_receipts_are_idempotent(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 9, 5, 14, tzinfo=UTC)]
    service = _service(engine, clock)
    gateway_id, bridge_id = _create_pair(service)
    _apply_onebot(service, gateway_id)
    credential = _register(service, bridge_id)

    v1 = service.submit_config(
        service_id=bridge_id,
        config_version_id=uuid4(),
        application_id=uuid4(),
        expected_version=1,
        settings={"guild_id": "123", "channel_id": "456"},
        secret="discord-token-v1",
        onebot_service_id=gateway_id,
        correlation_id=uuid4(),
    )
    fetched = service.get_worker_config(credential)
    assert fetched is not None
    assert fetched.secret == "discord-token-v1"
    assert fetched.linked_onebot is None
    receipt_v1 = uuid4()
    applied = service.settle_config_application(
        credential=credential,
        application_id=v1.application.application_id,
        receipt_event_id=receipt_v1,
        status=BotConfigApplicationStatus.APPLIED,
        worker_instance_id="discord-worker-1",
        error_code=None,
        error_summary=None,
        correlation_id=uuid4(),
    )
    replay = service.settle_config_application(
        credential=credential,
        application_id=v1.application.application_id,
        receipt_event_id=receipt_v1,
        status=BotConfigApplicationStatus.APPLIED,
        worker_instance_id="discord-worker-1",
        error_code=None,
        error_summary=None,
        correlation_id=uuid4(),
    )
    assert replay == applied
    after_v1 = service.get_service(bridge_id)
    assert after_v1.desired_config_version_id == v1.version.config_version_id
    assert after_v1.applied_config_version_id == v1.version.config_version_id

    clock[0] += timedelta(minutes=1)
    v2 = service.submit_config(
        service_id=bridge_id,
        config_version_id=uuid4(),
        application_id=uuid4(),
        expected_version=after_v1.row_version,
        settings={"guild_id": "123", "channel_id": "789"},
        secret="discord-token-v2",
        onebot_service_id=gateway_id,
        correlation_id=uuid4(),
    )
    service.settle_config_application(
        credential=credential,
        application_id=v2.application.application_id,
        receipt_event_id=uuid4(),
        status=BotConfigApplicationStatus.REJECTED,
        worker_instance_id="discord-worker-1",
        error_code="discord_login_failed",
        error_summary="Discord rejected the configured credential",
        correlation_id=uuid4(),
    )
    after_v2 = service.get_service(bridge_id)
    assert after_v2.desired_config_version_id == v2.version.config_version_id
    assert after_v2.applied_config_version_id == v1.version.config_version_id

    with Session(engine) as session:
        secrets = session.scalars(select(EncryptedSecretRow)).all()
        applications = session.scalars(
            select(BotConfigApplicationRow)
            .where(BotConfigApplicationRow.service_id == bridge_id)
            .order_by(BotConfigApplicationRow.requested_at)
        ).all()
        assert len(secrets) == 3
        assert all("discord-token" not in row.ciphertext for row in secrets)
        assert [row.status for row in applications] == ["applied", "rejected"]


def test_discord_identity_can_connect_before_forwarding_and_reuse_its_secret(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 9, 5, 14, 30, tzinfo=UTC)]
    service = _service(engine, clock)
    gateway_id, bridge_id = _create_pair(service)
    credential = _register(service, bridge_id)
    base = service.submit_config(
        service_id=bridge_id,
        config_version_id=uuid4(),
        application_id=uuid4(),
        expected_version=1,
        settings={"DISCORD_APPLICATION_ID": "1439231483326894160"},
        secret="discord-token",
        onebot_service_id=None,
        correlation_id=uuid4(),
    )
    worker_config = service.get_worker_config(credential)
    assert worker_config is not None
    assert worker_config.linked_onebot is None
    assert worker_config.secret == "discord-token"
    assert base.version.secret_id is not None
    service.settle_config_application(
        credential=credential,
        application_id=base.application.application_id,
        receipt_event_id=uuid4(),
        status=BotConfigApplicationStatus.APPLIED,
        worker_instance_id="discord-worker-1",
        error_code=None,
        error_summary=None,
        correlation_id=uuid4(),
    )
    _apply_onebot(service, gateway_id)
    current = service.get_service(bridge_id)
    key_version, key_application = uuid4(), uuid4()
    settings = {
        "DISCORD_APPLICATION_ID": "1439231483326894160",
        "DISCORD_OWNER_USER_IDS": "577928624884154368",
        "DISCORD_GUILD_IDS": "1439045928785940598",
    }
    updated = service.submit_config(
        service_id=bridge_id,
        config_version_id=key_version,
        application_id=key_application,
        expected_version=current.row_version,
        settings=settings,
        secret=None,
        onebot_service_id=gateway_id,
        correlation_id=uuid4(),
        retain_secret_from_config_version_id=base.version.config_version_id,
    )
    assert updated.version.secret_id == base.version.secret_id
    replay = service.submit_config(
        service_id=bridge_id,
        config_version_id=key_version,
        application_id=key_application,
        expected_version=current.row_version,
        settings=settings,
        secret=None,
        onebot_service_id=gateway_id,
        correlation_id=uuid4(),
        retain_secret_from_config_version_id=base.version.config_version_id,
    )
    assert replay.version.config_version_id == updated.version.config_version_id
    worker_config = service.get_worker_config(credential)
    assert worker_config is not None and worker_config.linked_onebot is None
    assert worker_config.secret == "discord-token"
    with Session(engine) as session:
        secrets = session.scalars(select(EncryptedSecretRow)).all()
        assert len(secrets) == 2  # One Discord token and one OneBot token.

    with pytest.raises(BotDomainError) as stale:
        service.submit_config(
            service_id=bridge_id,
            config_version_id=uuid4(),
            application_id=uuid4(),
            expected_version=updated.service.row_version,
            settings=settings,
            secret=None,
            onebot_service_id=gateway_id,
            correlation_id=uuid4(),
            retain_secret_from_config_version_id=base.version.config_version_id,
        )
    assert stale.value.code == "stale_bot_config_source"


def test_worker_isolation_rotation_and_disable_revoke_credentials(engine: Engine) -> None:
    clock = [datetime(2026, 9, 5, 15, tzinfo=UTC)]
    service = _service(engine, clock)
    gateway_id, bridge_id = _create_pair(service)
    bridge_credential = _register(service, bridge_id)
    gateway_credential = _register(service, gateway_id)
    _apply_onebot(service, gateway_id, gateway_credential)
    service.submit_config(
        service_id=bridge_id,
        config_version_id=uuid4(),
        application_id=uuid4(),
        expected_version=1,
        settings={"guild_id": "123"},
        secret="discord-token",
        onebot_service_id=gateway_id,
        correlation_id=uuid4(),
    )
    assert service.get_worker_config(gateway_credential) is None
    assert service.get_worker_config(bridge_credential) is not None

    current = service.get_service(bridge_id)
    rotated = service.rotate_credential(
        service_id=bridge_id,
        expected_version=current.row_version,
        correlation_id=uuid4(),
    )
    with pytest.raises(BotAuthenticationError):
        service.get_worker_config(bridge_credential)
    assert service.get_worker_config(rotated.credential) is not None

    current = service.get_service(bridge_id)
    service.disable_service(
        service_id=bridge_id,
        expected_version=current.row_version,
        correlation_id=uuid4(),
    )
    with pytest.raises(BotAuthenticationError):
        service.get_worker_config(rotated.credential)


def test_discord_worker_waits_for_referenced_onebot_applied_config(engine: Engine) -> None:
    clock = [datetime(2026, 9, 5, 15, 15, tzinfo=UTC)]
    service = _service(engine, clock)
    gateway_id, bridge_id = _create_pair(service)
    bridge_credential = _register(service, bridge_id)
    service.submit_config(
        service_id=bridge_id,
        config_version_id=uuid4(),
        application_id=uuid4(),
        expected_version=1,
        settings={"DISCORD_APPLICATION_ID": "1439231483326894160"},
        secret="discord-token",
        onebot_service_id=gateway_id,
        correlation_id=uuid4(),
    )

    fetched = service.get_worker_config(bridge_credential)
    assert fetched is not None and fetched.linked_onebot is None

    _apply_onebot(service, gateway_id)
    fetched = service.get_worker_config(bridge_credential)
    assert fetched is not None and fetched.linked_onebot is None


def test_public_settings_reject_nested_secrets_and_heartbeat_replay_is_idempotent(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 9, 5, 15, 30, tzinfo=UTC)]
    service = _service(engine, clock)
    gateway_id, bridge_id = _create_pair(service)
    credential = _register(service, bridge_id)

    with pytest.raises(BotDomainError) as secret_error:
        service.submit_config(
            service_id=bridge_id,
            config_version_id=uuid4(),
            application_id=uuid4(),
            expected_version=1,
            settings={"discord": {"access_token": "must-not-be-public"}},
            secret=None,
            onebot_service_id=gateway_id,
            correlation_id=uuid4(),
        )
    assert secret_error.value.code == "bot_secret_in_settings"

    receipt_event_id = uuid4()
    first = service.heartbeat(
        credential=credential,
        receipt_event_id=receipt_event_id,
        config_version_id=None,
        status=BotHealthStatus.HEALTHY,
        diagnostics={"worker_instance": "discord-worker-1"},
    )
    replay = service.heartbeat(
        credential=credential,
        receipt_event_id=receipt_event_id,
        config_version_id=None,
        status=BotHealthStatus.UNHEALTHY,
        diagnostics={"worker_instance": "ignored-replay"},
    )
    assert replay == first

    with Session(engine) as session:
        assert session.scalar(select(BotConfigVersionRow)) is None
        assert session.scalar(select(EncryptedSecretRow)) is None
        health = session.scalars(select(BotHealthReportRow)).all()
        assert len(health) == 1
        assert health[0].status == BotHealthStatus.HEALTHY.value


def test_concurrent_config_submissions_have_one_winner_and_no_orphan_secret(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 9, 5, 15, 45, tzinfo=UTC)]
    service = _service(engine, clock)
    gateway_id, bridge_id = _create_pair(service)
    barrier = Barrier(2)

    def submit(index: int) -> str:
        barrier.wait()
        try:
            service.submit_config(
                service_id=bridge_id,
                config_version_id=uuid4(),
                application_id=uuid4(),
                expected_version=1,
                settings={"DISCORD_APPLICATION_ID": str(100_000_000_000_000_00 + index)},
                secret=f"discord-token-{index}",
                onebot_service_id=gateway_id,
                correlation_id=uuid4(),
            )
        except BotDomainError as error:
            return error.code
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(submit, (1, 2)))

    assert sorted(outcomes) == ["created", "stale_bot_service"]
    with Session(engine) as session:
        versions = session.scalars(select(BotConfigVersionRow)).all()
        applications = session.scalars(select(BotConfigApplicationRow)).all()
        secrets = session.scalars(select(EncryptedSecretRow)).all()
        assert len(versions) == len(applications) == len(secrets) == 1
        assert versions[0].version_no == 1


def test_bot_history_queries_are_bounded_and_indexed_at_one_hundred_thousand_rows(
    engine: Engine,
) -> None:
    now = datetime(2026, 9, 5, 15, 50, tzinfo=UTC)
    service = _service(engine, [now])
    _gateway_id, bridge_id = _create_pair(service)
    _register(service, bridge_id)
    with Session(engine) as session:
        identity_id = session.scalar(
            select(BotWorkerIdentityRow.id).where(BotWorkerIdentityRow.service_id == bridge_id)
        )
    assert identity_id is not None

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO bot_config_versions (
                    id, service_id, version_no, settings, secret_id,
                    onebot_service_id, config_hash, created_at
                )
                SELECT
                    gen_random_uuid(), :service_id, series_no, '{}'::jsonb, NULL,
                    NULL, repeat('a', 64), :now + series_no * interval '1 microsecond'
                FROM generate_series(1, 100000) AS series_no
                """
            ),
            {"service_id": bridge_id, "now": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO bot_config_applications (
                    id, service_id, config_version_id, status, worker_instance_id,
                    receipt_event_id, requested_at, completed_at, row_version
                )
                SELECT
                    gen_random_uuid(), service_id, id, 'applied', 'scale-worker',
                    gen_random_uuid(), created_at, created_at, 2
                FROM bot_config_versions
                WHERE service_id = :service_id
                """
            ),
            {"service_id": bridge_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO bot_health_reports (
                    id, receipt_event_id, service_id, identity_id,
                    config_version_id, status, diagnostics, reported_at
                )
                SELECT
                    gen_random_uuid(), gen_random_uuid(), service_id, :identity_id,
                    id, 'healthy', '{}'::jsonb, created_at
                FROM bot_config_versions
                WHERE service_id = :service_id
                """
            ),
            {"service_id": bridge_id, "identity_id": identity_id},
        )
        connection.execute(text("ANALYZE bot_config_applications"))
        connection.execute(text("ANALYZE bot_health_reports"))

    query_count = 0

    def count_query(*_args: object, **_kwargs: object) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        applications = service.list_config_applications(
            bridge_id, before_requested_at=None, before_id=None, limit=50
        )
        health = service.list_health_reports(
            bridge_id, before_reported_at=None, before_id=None, limit=50
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_query)
    assert len(applications) == len(health) == 50
    assert query_count == 4

    with engine.connect() as connection:
        application_plan = connection.execute(
            text(
                """
                EXPLAIN (FORMAT JSON)
                SELECT id
                FROM bot_config_applications
                WHERE service_id = :service_id
                ORDER BY requested_at DESC, id DESC
                LIMIT 50
                """
            ),
            {"service_id": bridge_id},
        ).scalar_one()
        health_plan = connection.execute(
            text(
                """
                EXPLAIN (FORMAT JSON)
                SELECT id
                FROM bot_health_reports
                WHERE service_id = :service_id
                ORDER BY reported_at DESC, id DESC
                LIMIT 50
                """
            ),
            {"service_id": bridge_id},
        ).scalar_one()
    assert "ix_bot_config_applications_service_requested" in str(application_plan)
    assert "ix_bot_health_service_reported" in str(health_plan)

    cleanup = service.cleanup_history(
        cutoff=now + timedelta(days=1), limit=50, correlation_id=uuid4()
    )
    assert cleanup.deleted_health_reports == 50
    assert cleanup.deleted_applications == 50
    assert cleanup.deleted_config_versions == 50
    assert cleanup.deleted_secrets == 0
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(BotHealthReportRow)) == 99_950
        assert session.scalar(select(func.count()).select_from(BotConfigApplicationRow)) == 99_950


def test_history_cleanup_protects_current_versions_latest_health_and_their_secrets(
    engine: Engine,
) -> None:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    service = _service(engine, [now])
    gateway_id, _bridge_id = _create_pair(service)
    credential = _apply_onebot(service, gateway_id)
    applied = service.get_service(gateway_id).applied_config_version_id
    latest = service.heartbeat(
        credential=credential,
        receipt_event_id=uuid4(),
        config_version_id=applied,
        status=BotHealthStatus.HEALTHY,
        diagnostics={},
    )
    desired = service.submit_config(
        service_id=gateway_id,
        config_version_id=uuid4(),
        application_id=uuid4(),
        expected_version=service.get_service(gateway_id).row_version,
        settings={"ONEBOT_BASE_URL": "http://llonebot:3000"},
        secret="replacement-token",
        onebot_service_id=None,
        correlation_id=uuid4(),
    )
    result = service.cleanup_history(
        cutoff=now + timedelta(days=31), limit=100, correlation_id=uuid4()
    )
    assert result.deleted_config_versions == result.deleted_secrets == 0
    assert result.deleted_health_reports == 0
    with Session(engine) as session:
        assert session.get(BotHealthReportRow, latest.report_id) is not None
        assert session.get(BotConfigVersionRow, applied) is not None
        assert session.get(BotConfigVersionRow, desired.version.config_version_id) is not None
        assert session.get(BotConfigApplicationRow, desired.application.application_id) is not None
        assert session.scalar(select(func.count()).select_from(EncryptedSecretRow)) == 2


def test_bot_http_contract_hides_user_secrets_and_worker_response_is_no_store(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 9, 5, 16, tzinfo=UTC)]
    service = _service(engine, clock)

    class Ready:
        def check(self) -> ReadinessResult:
            return ReadinessResult(ready=True, dependencies={})

        def close(self) -> None:
            return

    app = create_app(
        Settings(admin_password="integration-bot-password", admin_cookie_secure=False),
        readiness_service=Ready(),  # type: ignore[arg-type]
        bot_service=service,
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport, base_url="http://test",
                headers={"X-AL1S-CSRF": "1"},
            ) as client,
        ):
            logged_in = await client.post(
                "/api/v1/auth/login", json={"password": "integration-bot-password"},
            )
            assert logged_in.status_code == 200
            gateway = await client.post(
                "/api/v1/bots/services",
                headers={"Idempotency-Key": "gateway"},
                json={"kind": "onebot_gateway", "name": "HTTP gateway"},
            )
            bridge = await client.post(
                "/api/v1/bots/services",
                headers={"Idempotency-Key": "bridge"},
                json={"kind": "discord_bridge", "name": "HTTP bridge"},
            )
            assert gateway.status_code == bridge.status_code == 201
            bridge_id = bridge.json()["service_id"]
            grant = await client.post(
                f"/api/v1/bots/services/{bridge_id}/registration-grants",
                json={"ttl_seconds": 600},
            )
            registered = await client.post(
                "/api/v1/bots/workers/register",
                json={"registration_code": grant.json()["registration_code"]},
            )
            credential = registered.json()["credential"]
            configured = await client.post(
                f"/api/v1/bots/services/{bridge_id}/config-versions",
                headers={"Idempotency-Key": "bridge-v1", "If-Match": '"1"'},
                json={
                    "settings": {"guild_id": "123"},
                    "secret": "http-discord-token",
                    "onebot_service_id": gateway.json()["service_id"],
                },
            )
            assert configured.status_code == 201
            assert configured.json()["version"]["secret_configured"] is True
            assert "http-discord-token" not in configured.text
            version_id = configured.json()["version"]["config_version_id"]
            read_version = await client.get(
                f"/api/v1/bots/services/{bridge_id}/config-versions/{version_id}"
            )
            assert read_version.status_code == 200
            assert read_version.headers["cache-control"] == "no-store"
            assert read_version.json()["settings"] == {"guild_id": "123"}
            assert read_version.json()["secret_configured"] is True
            assert "http-discord-token" not in read_version.text
            wrong_owner = await client.get(
                f"/api/v1/bots/services/{gateway.json()['service_id']}/config-versions/{version_id}"
            )
            assert wrong_owner.status_code == 404

            gateway_id = UUID(gateway.json()["service_id"])
            gateway_credential = _apply_onebot(service, gateway_id)
            assert gateway_credential

            unauthenticated = await client.get("/api/v1/bots/worker/config")
            fetched = await client.get(
                "/api/v1/bots/worker/config",
                headers={"Authorization": f"Bearer {credential}"},
            )
            assert unauthenticated.status_code == 401
            assert unauthenticated.headers["cache-control"] == "no-store"
            assert fetched.status_code == 200
            assert fetched.headers["cache-control"] == "no-store"
            assert fetched.json()["secret"] == "http-discord-token"
            assert fetched.json()["linked_onebot"] is None
            application_page = await client.get(
                f"/api/v1/bots/services/{bridge_id}/config-applications?limit=1"
            )
            health_page = await client.get(
                f"/api/v1/bots/services/{bridge_id}/health-reports?limit=1"
            )
            assert application_page.status_code == health_page.status_code == 200
            assert len(application_page.json()["items"]) == 1
            assert health_page.json()["items"] == []

    asyncio.run(exercise())

    with Session(engine) as session:
        services = session.scalars(select(BotServiceRow)).all()
        versions = session.scalars(select(BotConfigVersionRow)).all()
        assert len(services) == 2
        assert len(versions) == 2
