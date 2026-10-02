from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine, create_engine, event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.execution_models import (
    ExecutionLeaseRow,
    TargetDeviceIdentifierRow,
    TargetDeviceRow,
    TerminalCapabilityProfileRow,
    TerminalCredentialRow,
    TerminalRegistrationGrantRow,
    TerminalRow,
)
from al1s.adapters.postgres.execution_unit_of_work import ExecutionSqlAlchemyUnitOfWork
from al1s.adapters.postgres.models import AuditLogRow, OutboxEventRow
from al1s.adapters.postgres.mqtt_models import TerminalMqttSessionRow
from al1s.adapters.postgres.mqtt_unit_of_work import MqttSessionSqlAlchemyUnitOfWork
from al1s.app.config import Settings
from al1s.app.factory import create_app
from al1s.execution.errors import AuthenticationError, ConflictError, InvalidRequestError
from al1s.execution.mqtt_sessions import (
    MqttSessionPasswordSigner,
    TerminalMqttSessionRecord,
    TerminalMqttSessionService,
    TerminalMqttSessionUnitOfWork,
)
from al1s.execution.ports import ExecutionUnitOfWork
from al1s.execution.services import ExecutionResourceService
from al1s.execution.types import (
    CapabilityManifest,
    LeaseKind,
    LeaseOwnerKind,
    TargetDeviceMode,
    TargetIdentifierSource,
    TerminalAcceptanceStatus,
    TerminalServiceStatus,
    TerminalType,
)
from al1s.infrastructure.readiness import ReadinessResult

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
def clean_execution_tables(engine: Engine) -> Iterator[None]:
    table_names = (
        "terminal_mqtt_sessions, execution_leases, target_device_identifiers, target_devices, "
        "terminal_capability_profiles, terminal_credentials, "
        "terminal_registration_grants, terminals, audit_logs, outbox_events"
    )
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {table_names} CASCADE"))
    yield
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {table_names} CASCADE"))


@pytest.fixture
def service(engine: Engine) -> ExecutionResourceService:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def factory() -> ExecutionUnitOfWork:
        return cast(ExecutionUnitOfWork, ExecutionSqlAlchemyUnitOfWork(sessions))

    return ExecutionResourceService(factory)


def _register(
    service: ExecutionResourceService,
    terminal_type: TerminalType,
    *,
    installation_id: UUID | None = None,
) -> tuple[UUID, UUID, str, int]:
    install_id = installation_id or uuid4()
    grant = service.create_registration_grant(
        allowed_terminal_type=terminal_type,
        ttl=timedelta(minutes=10),
        correlation_id=uuid4(),
    )
    registered = service.register_terminal(
        registration_code=grant.registration_code,
        installation_id=install_id,
        terminal_type=terminal_type,
        display_name=f"test-{terminal_type.value}",
        agent_version="3a-test",
        correlation_id=uuid4(),
    )
    return (
        registered.terminal.terminal_id,
        install_id,
        registered.credential,
        registered.terminal.row_version,
    )


def test_adb_connect_is_explicit_atomic_and_idempotent(
    engine: Engine, service: ExecutionResourceService
) -> None:
    terminal_id, _, credential, _ = _register(service, TerminalType.LINUX)
    discovery = service.discover_target_identifier(
        credential=credential,
        source_type=TargetIdentifierSource.ADB_SERIAL,
        raw_identifier="adb-connect-test",
        correlation_id=uuid4(),
    )
    assert discovery.target_device_id is None
    assert service.list_adb_discoveries(terminal_id=terminal_id, after_id=None, limit=10) == [
        discovery
    ]
    target = service.connect_adb_discovery(
        identifier_id=discovery.identifier_id,
        expected_identifier_version=discovery.row_version,
        display_name="Test phone",
        correlation_id=uuid4(),
    )
    replay = service.connect_adb_discovery(
        identifier_id=discovery.identifier_id,
        expected_identifier_version=discovery.row_version,
        display_name="Ignored on replay",
        correlation_id=uuid4(),
    )
    assert target == replay
    assert target.mode is TargetDeviceMode.MOUNTED
    assert target.managing_terminal_id == terminal_id
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TargetDeviceRow)) == 1
    service.set_target_mode(
        device_id=target.device_id,
        expected_version=target.row_version,
        mode=TargetDeviceMode.UNASSIGNED,
        managing_terminal_id=None,
        correlation_id=uuid4(),
    )
    with pytest.raises(ConflictError, match="handover"):
        service.connect_adb_discovery(
            identifier_id=discovery.identifier_id,
            expected_identifier_version=discovery.row_version,
            display_name="No remount",
            correlation_id=uuid4(),
        )


def test_adb_observation_reports_connection_authorization_and_staleness(
    service: ExecutionResourceService,
) -> None:
    terminal_id, _, credential, _ = _register(service, TerminalType.LINUX)
    observed = service.discover_target_identifier(
        credential=credential,
        source_type=TargetIdentifierSource.ADB_SERIAL,
        raw_identifier="adb-presence-test",
        adb_state="device",
        correlation_id=uuid4(),
    )
    target = service.connect_adb_discovery(
        identifier_id=observed.identifier_id,
        expected_identifier_version=observed.row_version,
        display_name="Observed phone",
        correlation_id=uuid4(),
    )
    assert target.managing_terminal_id == terminal_id
    current = service.list_target_devices(limit=10)[0]
    assert current.availability == "connected"
    assert current.availability_reason is None
    assert current.availability_observed_at == observed.observed_at

    unauthorized = service.discover_target_identifier(
        credential=credential,
        source_type=TargetIdentifierSource.ADB_SERIAL,
        raw_identifier="adb-presence-test",
        adb_state="unauthorized",
        correlation_id=uuid4(),
    )
    # Binding advances the version; repeated probes do not.
    assert unauthorized.row_version == observed.row_version + 1
    assert service.list_target_devices(limit=10)[0].availability == "unauthorized"
    assert unauthorized.observed_at is not None
    service._now = lambda: unauthorized.observed_at + timedelta(seconds=31)
    stale = service.list_target_devices(limit=10)[0]
    assert stale.availability == "disconnected"
    assert stale.availability_reason == "adb_observation_stale"


def test_adb_observation_batch_is_owned_atomic_and_preserves_binding_version(
    engine: Engine, service: ExecutionResourceService
) -> None:
    _, _, credential, _ = _register(service, TerminalType.LINUX)
    _, _, other_credential, _ = _register(service, TerminalType.LINUX)
    first = service.discover_target_identifier(
        credential=credential, source_type=TargetIdentifierSource.ADB_SERIAL,
        raw_identifier="batch-phone-1", correlation_id=uuid4(),
    )
    second = service.discover_target_identifier(
        credential=credential, source_type=TargetIdentifierSource.ADB_SERIAL,
        raw_identifier="batch-phone-2", correlation_id=uuid4(),
    )
    with pytest.raises(ConflictError, match="not active for terminal"):
        service.report_adb_observations(
            credential=other_credential,
            items=((first.identifier_id, "device"), (second.identifier_id, "offline")),
        )
    with Session(engine) as session:
        assert session.get(TargetDeviceIdentifierRow, first.identifier_id).observed_at is None
    assert service.report_adb_observations(
        credential=credential,
        items=((first.identifier_id, "device"), (second.identifier_id, "unauthorized")),
    ) == 2
    with Session(engine) as session:
        one = session.get(TargetDeviceIdentifierRow, first.identifier_id)
        two = session.get(TargetDeviceIdentifierRow, second.identifier_id)
        assert one is not None and two is not None
        assert one.adb_state == "device" and two.adb_state == "unauthorized"
        assert one.row_version == first.row_version and two.row_version == second.row_version


def test_adb_connect_rejects_stale_and_wrong_source(
    engine: Engine, service: ExecutionResourceService
) -> None:
    _, _, credential, _ = _register(service, TerminalType.LINUX)
    for kind in [TargetIdentifierSource.ADB_SERIAL, TargetIdentifierSource.ANDROID_ID]:
        item = service.discover_target_identifier(
            credential=credential,
            source_type=kind,
            raw_identifier="connect-invalid",
            correlation_id=uuid4(),
        )
        with pytest.raises(ConflictError):
            service.connect_adb_discovery(
                identifier_id=item.identifier_id,
                expected_identifier_version=99,
                display_name="No target",
                correlation_id=uuid4(),
            )
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TargetDeviceRow)) == 0


def test_storage_heartbeat_latch_survives_reconnect(
    engine: Engine, service: ExecutionResourceService
) -> None:
    from al1s.execution.storage import StorageObservation

    terminal_id, _, credential, version = _register(service, TerminalType.LINUX)
    previous_time = None
    for free, expected in [
        (20, False),
        (19, True),
        (10, True),
        (None, True),
        (24, True),
        (25, False),
        (19, True),
    ]:
        current = service.heartbeat(
            credential=credential,
            expected_version=version,
            service_status=TerminalServiceStatus.ONLINE,
            acceptance_status=TerminalAcceptanceStatus.ACCEPTING,
            agent_version="storage-test",
            correlation_id=uuid4(),
            storage=None
            if free is None
            else StorageObservation("/data/runtime", 100, 100 - free, free),
        )
        version = current.row_version
        assert current.storage_alert_active is expected
        assert current.storage_probe_ok is (free is not None)
        if free is None:
            assert current.storage_observed_at == previous_time
        previous_time = current.storage_observed_at
    with Session(engine) as session:
        events = session.scalars(
            select(OutboxEventRow).where(
                OutboxEventRow.event_type == "terminal.storage_low.v1",
                OutboxEventRow.aggregate_id == terminal_id,
            )
        ).all()
        assert len(events) == 2
    assert service.authenticate_terminal(credential).storage_alert_active is True


def _manifest(*, provider_keys: tuple[str, ...] = ("maa", "rknn")) -> CapabilityManifest:
    return CapabilityManifest(
        schema_version=1,
        protocol_version=1,
        agent_version="3a-test",
        os_name="linux",
        os_version="ubuntu-22.04",
        architecture="aarch64",
        cpu_cores=8,
        memory_bytes=2 * 1024**3,
        storage_available_bytes=100 * 1024**3,
        accelerator_type="rk3576",
        low_resource=True,
        provider_keys=provider_keys,
        details={"npu_tops": 6},
    )


def test_registration_consumes_grant_and_never_persists_plaintext(
    engine: Engine, service: ExecutionResourceService
) -> None:
    grant = service.create_registration_grant(
        allowed_terminal_type=TerminalType.LINUX,
        ttl=timedelta(minutes=10),
        correlation_id=uuid4(),
    )
    registered = service.register_terminal(
        registration_code=grant.registration_code,
        installation_id=uuid4(),
        terminal_type=TerminalType.LINUX,
        display_name="rk3576",
        agent_version="3a-test",
        correlation_id=uuid4(),
    )

    with Session(engine) as session:
        grant_row = session.get(TerminalRegistrationGrantRow, grant.grant_id)
        credential_row = session.scalar(
            select(TerminalCredentialRow).where(
                TerminalCredentialRow.terminal_id == registered.terminal.terminal_id
            )
        )
        outbox_text = str(session.scalars(select(OutboxEventRow.payload)).all())
        audit_text = str(session.scalars(select(AuditLogRow.details)).all())

    assert grant_row is not None and grant_row.consumed_at is not None
    assert credential_row is not None
    assert grant.registration_code not in grant_row.secret_digest
    assert registered.credential not in credential_row.secret_digest
    assert grant.registration_code not in outbox_text + audit_text
    assert registered.credential not in outbox_text + audit_text
    with pytest.raises(ConflictError, match="already been used"):
        service.register_terminal(
            registration_code=grant.registration_code,
            installation_id=uuid4(),
            terminal_type=TerminalType.LINUX,
            display_name="replay",
            agent_version="3a-test",
            correlation_id=uuid4(),
        )


def test_reregister_reuses_terminal_and_rotates_credential(
    service: ExecutionResourceService,
) -> None:
    terminal_id, installation_id, old_credential, _ = _register(service, TerminalType.LINUX)
    second_grant = service.create_registration_grant(
        allowed_terminal_type=TerminalType.LINUX,
        ttl=timedelta(minutes=10),
        correlation_id=uuid4(),
    )
    second = service.register_terminal(
        registration_code=second_grant.registration_code,
        installation_id=installation_id,
        terminal_type=TerminalType.LINUX,
        display_name="same-installation",
        agent_version="3a-test-2",
        correlation_id=uuid4(),
    )

    assert second.terminal.terminal_id == terminal_id
    assert second.credential != old_credential
    with pytest.raises(AuthenticationError):
        service.authenticate_terminal(old_credential)
    assert service.authenticate_terminal(second.credential).terminal_id == terminal_id


def test_admin_name_survives_heartbeat_and_reregistration(
    engine: Engine, service: ExecutionResourceService,
) -> None:
    terminal_id, installation_id, credential, version = _register(service, TerminalType.LINUX)
    with Session(engine) as session:
        row = session.get(TerminalRow, terminal_id)
        assert row is not None
        row.service_status = TerminalServiceStatus.OFFLINE.value
        session.commit()

    renamed = service.rename_terminal(
        terminal_id=terminal_id, expected_name_version=0,
        display_name="  Desk phone  ", correlation_id=uuid4(),
    )
    assert renamed.display_name == "Desk phone" and renamed.name_version == 1
    with pytest.raises(ConflictError):
        service.rename_terminal(
            terminal_id=terminal_id, expected_name_version=0,
            display_name="Stale name", correlation_id=uuid4(),
        )
    heartbeat = service.heartbeat(
        credential=credential, expected_version=version,
        service_status=TerminalServiceStatus.ONLINE,
        acceptance_status=TerminalAcceptanceStatus.ACCEPTING,
        agent_version="next", correlation_id=uuid4(),
    )
    assert heartbeat.display_name == "Desk phone" and heartbeat.name_version == 1
    grant = service.create_registration_grant(
        allowed_terminal_type=TerminalType.LINUX,
        ttl=timedelta(minutes=10), correlation_id=uuid4(),
    )
    registered = service.register_terminal(
        registration_code=grant.registration_code, installation_id=installation_id,
        terminal_type=TerminalType.LINUX, display_name="Device self report",
        agent_version="next", correlation_id=uuid4(),
    )
    assert registered.terminal.terminal_id == terminal_id
    assert registered.terminal.display_name == "Desk phone"
    assert registered.terminal.name_version == 1
    with Session(engine) as session:
        row = session.get(TerminalRow, terminal_id)
        assert row is not None and row.name_is_custom and row.name_version == 1


def test_logical_phone_rename_preserves_identity_and_rejects_stale_version(
    engine: Engine, service: ExecutionResourceService,
) -> None:
    target = service.create_target_device(
        display_name="Original phone", correlation_id=uuid4(),
    )
    renamed = service.rename_target_device(
        device_id=target.device_id, expected_version=target.row_version,
        display_name="  Desk phone  ", correlation_id=uuid4(),
    )
    assert renamed.display_name == "Desk phone"
    assert renamed.device_id == target.device_id
    assert renamed.mode == target.mode
    assert renamed.managing_terminal_id == target.managing_terminal_id
    assert renamed.row_version == target.row_version + 1
    assert service.get_target_device(target.device_id).display_name == "Desk phone"
    with Session(engine) as session:
        row = session.get(TargetDeviceRow, target.device_id)
        assert row is not None and row.display_name == "Desk phone"
        audits = session.scalars(
            select(AuditLogRow).where(AuditLogRow.action == "target_device.display_name.update")
        ).all()
        assert len(audits) == 1
    with pytest.raises(ConflictError):
        service.rename_target_device(
            device_id=target.device_id, expected_version=target.row_version,
            display_name="Stale", correlation_id=uuid4(),
        )
    with pytest.raises(InvalidRequestError):
        service.rename_target_device(
            device_id=target.device_id, expected_version=renamed.row_version,
            display_name="   ", correlation_id=uuid4(),
        )
    with pytest.raises(InvalidRequestError):
        service.rename_target_device(
            device_id=target.device_id, expected_version=renamed.row_version,
            display_name="x" * 121, correlation_id=uuid4(),
        )
    assert service.get_target_device(target.device_id).display_name == "Desk phone"


def test_android_registration_creates_standalone_target_and_reuses_it(
    engine: Engine, service: ExecutionResourceService
) -> None:
    installation_id = uuid4()
    grant = service.create_registration_grant(
        allowed_terminal_type=TerminalType.ANDROID,
        ttl=timedelta(minutes=10),
        correlation_id=uuid4(),
    )
    first = service.register_terminal(
        registration_code=grant.registration_code,
        installation_id=installation_id,
        terminal_type=TerminalType.ANDROID,
        display_name="Xiaomi root demo",
        agent_version="android-root-demo-1",
        correlation_id=uuid4(),
    )

    assert first.target_device is not None
    assert first.target_device.mode is TargetDeviceMode.STANDALONE
    assert first.target_device.managing_terminal_id == first.terminal.terminal_id
    second_grant = service.create_registration_grant(
        allowed_terminal_type=TerminalType.ANDROID,
        ttl=timedelta(minutes=10),
        correlation_id=uuid4(),
    )
    second = service.register_terminal(
        registration_code=second_grant.registration_code,
        installation_id=installation_id,
        terminal_type=TerminalType.ANDROID,
        display_name="Xiaomi root demo",
        agent_version="android-root-demo-2",
        correlation_id=uuid4(),
    )

    assert second.terminal.terminal_id == first.terminal.terminal_id
    assert second.target_device is not None
    assert second.target_device.device_id == first.target_device.device_id
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TerminalRow)) == 1
        assert session.scalar(select(func.count()).select_from(TargetDeviceRow)) == 1
        assert session.scalar(select(func.count()).select_from(TargetDeviceIdentifierRow)) == 1


def test_android_target_bound_registration_preserves_linux_mounted_mode(
    service: ExecutionResourceService,
) -> None:
    linux_id, _, _, _ = _register(service, TerminalType.LINUX)
    target = service.create_target_device(display_name="Existing Xiaomi", correlation_id=uuid4())
    mounted = service.set_target_mode(
        device_id=target.device_id,
        expected_version=target.row_version,
        mode=TargetDeviceMode.MOUNTED,
        managing_terminal_id=linux_id,
        correlation_id=uuid4(),
    )
    grant = service.create_registration_grant(
        allowed_terminal_type=TerminalType.ANDROID,
        target_device_id=mounted.device_id,
        ttl=timedelta(minutes=10),
        correlation_id=uuid4(),
    )
    registered = service.register_terminal(
        registration_code=grant.registration_code,
        installation_id=uuid4(),
        terminal_type=TerminalType.ANDROID,
        display_name="Existing Xiaomi APK",
        agent_version="android-root-demo-1",
        correlation_id=uuid4(),
    )

    assert registered.target_device is not None
    assert registered.target_device.device_id == mounted.device_id
    assert registered.target_device.mode is TargetDeviceMode.MOUNTED
    assert registered.target_device.managing_terminal_id == linux_id


def test_android_target_binding_conflict_rolls_back_registration(
    engine: Engine, service: ExecutionResourceService
) -> None:
    target = service.create_target_device(display_name="One APK only", correlation_id=uuid4())
    first_grant = service.create_registration_grant(
        allowed_terminal_type=TerminalType.ANDROID,
        target_device_id=target.device_id,
        ttl=timedelta(minutes=10),
        correlation_id=uuid4(),
    )
    service.register_terminal(
        registration_code=first_grant.registration_code,
        installation_id=uuid4(),
        terminal_type=TerminalType.ANDROID,
        display_name="First APK",
        agent_version="android-root-demo-1",
        correlation_id=uuid4(),
    )
    second_grant = service.create_registration_grant(
        allowed_terminal_type=TerminalType.ANDROID,
        target_device_id=target.device_id,
        ttl=timedelta(minutes=10),
        correlation_id=uuid4(),
    )

    with pytest.raises(ConflictError, match="another Android installation"):
        service.register_terminal(
            registration_code=second_grant.registration_code,
            installation_id=uuid4(),
            terminal_type=TerminalType.ANDROID,
            display_name="Conflicting APK",
            agent_version="android-root-demo-1",
            correlation_id=uuid4(),
        )

    with Session(engine) as session:
        grant_row = session.get(TerminalRegistrationGrantRow, second_grant.grant_id)
        assert grant_row is not None and grant_row.consumed_at is None
        assert session.scalar(select(func.count()).select_from(TerminalRow)) == 1
        assert session.scalar(select(func.count()).select_from(TargetDeviceIdentifierRow)) == 1


def test_target_bound_registration_grant_requires_android(
    service: ExecutionResourceService,
) -> None:
    target = service.create_target_device(display_name="Scoped", correlation_id=uuid4())
    with pytest.raises(InvalidRequestError, match="must only allow Android"):
        service.create_registration_grant(
            allowed_terminal_type=TerminalType.LINUX,
            target_device_id=target.device_id,
            ttl=timedelta(minutes=10),
            correlation_id=uuid4(),
        )


def test_heartbeat_reconciles_older_version_and_credential_rotation_fences_old_secret(
    service: ExecutionResourceService,
) -> None:
    terminal_id, _, credential, version = _register(service, TerminalType.ANDROID)
    heartbeat = service.heartbeat(
        credential=credential,
        expected_version=version,
        service_status=TerminalServiceStatus.ONLINE,
        acceptance_status=TerminalAcceptanceStatus.DRAINING,
        agent_version="3a-test",
        correlation_id=uuid4(),
    )
    reconciled = service.heartbeat(
        credential=credential,
        expected_version=version,
        service_status=TerminalServiceStatus.ONLINE,
        acceptance_status=TerminalAcceptanceStatus.ACCEPTING,
        agent_version="3a-test",
        correlation_id=uuid4(),
    )
    assert reconciled.row_version == heartbeat.row_version + 1
    assert reconciled.acceptance_status is TerminalAcceptanceStatus.ACCEPTING
    with pytest.raises(ConflictError, match="stale"):
        service.heartbeat(
            credential=credential,
            expected_version=reconciled.row_version + 1,
            service_status=TerminalServiceStatus.ONLINE,
            acceptance_status=TerminalAcceptanceStatus.ACCEPTING,
            agent_version="3a-test",
            correlation_id=uuid4(),
        )
    rotated, new_credential = service.rotate_credential(
        credential=credential,
        expected_version=reconciled.row_version,
        correlation_id=uuid4(),
    )
    assert rotated.terminal_id == terminal_id
    with pytest.raises(AuthenticationError):
        service.authenticate_terminal(credential)
    assert service.authenticate_terminal(new_credential).terminal_id == terminal_id


def test_capability_profiles_are_immutable_idempotent_and_monotonic(
    engine: Engine, service: ExecutionResourceService
) -> None:
    terminal_id, _, credential, _ = _register(service, TerminalType.LINUX)
    first = service.publish_capability(
        credential=credential,
        revision=1,
        manifest=_manifest(),
        correlation_id=uuid4(),
    )
    replay = service.publish_capability(
        credential=credential,
        revision=1,
        manifest=_manifest(provider_keys=("rknn", "maa")),
        correlation_id=uuid4(),
    )

    assert replay.profile_id == first.profile_id
    with pytest.raises(ConflictError, match="different content"):
        service.publish_capability(
            credential=credential,
            revision=1,
            manifest=_manifest(provider_keys=("maa",)),
            correlation_id=uuid4(),
        )
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(TerminalCapabilityProfileRow)
                .where(TerminalCapabilityProfileRow.terminal_id == terminal_id)
            )
            == 1
        )
        terminal = session.get(TerminalRow, terminal_id)
        assert terminal is not None and terminal.current_capability_profile_id == first.profile_id


def test_identifiers_remain_pending_until_explicit_binding_and_mode_is_typed(
    service: ExecutionResourceService,
) -> None:
    linux_id, _, linux_credential, _ = _register(service, TerminalType.LINUX)
    android_id, _, _, _ = _register(service, TerminalType.ANDROID)
    discovery = service.discover_target_identifier(
        credential=linux_credential,
        source_type=TargetIdentifierSource.ADB_SERIAL,
        raw_identifier="samsung-unrooted",
        correlation_id=uuid4(),
    )
    target = service.create_target_device(display_name="Samsung", correlation_id=uuid4())

    assert discovery.target_device_id is None
    bound = service.bind_target_identifier(
        device_id=target.device_id,
        identifier_id=discovery.identifier_id,
        expected_identifier_version=discovery.row_version,
        correlation_id=uuid4(),
    )
    assert bound.target_device_id == target.device_id
    with pytest.raises(ConflictError, match="requires a android terminal"):
        service.set_target_mode(
            device_id=target.device_id,
            expected_version=target.row_version,
            mode=TargetDeviceMode.STANDALONE,
            managing_terminal_id=linux_id,
            correlation_id=uuid4(),
        )
    standalone = service.set_target_mode(
        device_id=target.device_id,
        expected_version=target.row_version,
        mode=TargetDeviceMode.STANDALONE,
        managing_terminal_id=android_id,
        correlation_id=uuid4(),
    )
    with pytest.raises(ConflictError, match="must be unassigned"):
        service.set_target_mode(
            device_id=target.device_id,
            expected_version=standalone.row_version,
            mode=TargetDeviceMode.MOUNTED,
            managing_terminal_id=linux_id,
            correlation_id=uuid4(),
        )
    unassigned = service.set_target_mode(
        device_id=target.device_id,
        expected_version=standalone.row_version,
        mode=TargetDeviceMode.UNASSIGNED,
        managing_terminal_id=None,
        correlation_id=uuid4(),
    )
    mounted = service.set_target_mode(
        device_id=target.device_id,
        expected_version=unassigned.row_version,
        mode=TargetDeviceMode.MOUNTED,
        managing_terminal_id=linux_id,
        correlation_id=uuid4(),
    )
    assert mounted.mode is TargetDeviceMode.MOUNTED


def test_concurrent_lease_acquisition_has_one_winner(
    engine: Engine, service: ExecutionResourceService
) -> None:
    terminal_id, _, _, _ = _register(service, TerminalType.LINUX)
    target = service.create_target_device(display_name="leased", correlation_id=uuid4())
    target = service.set_target_mode(
        device_id=target.device_id,
        expected_version=target.row_version,
        mode=TargetDeviceMode.MOUNTED,
        managing_terminal_id=terminal_id,
        correlation_id=uuid4(),
    )
    barrier = Barrier(2)

    def acquire(owner_id: UUID) -> UUID | None:
        barrier.wait(timeout=5)
        try:
            lease = service.acquire_lease(
                terminal_id=terminal_id,
                target_device_id=target.device_id,
                lease_kind=LeaseKind.EXECUTION,
                owner_kind=LeaseOwnerKind.EXECUTION,
                owner_id=owner_id,
                ttl=timedelta(minutes=1),
            )
        except ConflictError:
            return None
        return lease.lease_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(acquire, uuid4())
        second = executor.submit(acquire, uuid4())
        results = [first.result(timeout=10), second.result(timeout=10)]

    assert sum(result is not None for result in results) == 1
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExecutionLeaseRow)
                .where(ExecutionLeaseRow.released_at.is_(None))
            )
            == 1
        )


def test_identifier_digest_is_unique_without_automatic_device_merge(
    engine: Engine, service: ExecutionResourceService
) -> None:
    _, _, first_credential, _ = _register(service, TerminalType.LINUX)
    _, _, second_credential, _ = _register(service, TerminalType.LINUX)
    first = service.discover_target_identifier(
        credential=first_credential,
        source_type=TargetIdentifierSource.ADB_SERIAL,
        raw_identifier="shared-serial",
        correlation_id=uuid4(),
    )
    with pytest.raises(ConflictError, match="another terminal"):
        service.discover_target_identifier(
            credential=second_credential,
            source_type=TargetIdentifierSource.ADB_SERIAL,
            raw_identifier="shared-serial",
            correlation_id=uuid4(),
        )
    with Session(engine) as session:
        row = session.get(TargetDeviceIdentifierRow, first.identifier_id)
        assert row is not None and row.target_device_id is None


def test_terminal_list_is_bounded_and_uses_one_query(
    engine: Engine, service: ExecutionResourceService
) -> None:
    with Session(engine) as session:
        session.add_all(
            [
                TerminalRow(
                    id=uuid4(),
                    installation_id=uuid4(),
                    terminal_type=TerminalType.LINUX.value,
                    display_name=f"bulk-{index:03d}",
                    service_status=TerminalServiceStatus.OFFLINE.value,
                    acceptance_status=TerminalAcceptanceStatus.ACCEPTING.value,
                    agent_version="bulk-test",
                    row_version=1,
                )
                for index in range(100)
            ]
        )
        session.commit()

    query_count = 0

    def count_query(*_args: object) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        terminals = service.list_terminals(limit=100)
    finally:
        event.remove(engine, "before_cursor_execute", count_query)

    assert len(terminals) == 100
    assert query_count == 1


def test_terminal_management_projection_is_bounded_and_uses_one_query(
    engine: Engine, service: ExecutionResourceService
) -> None:
    now = datetime.now(UTC)
    managed_terminal_id = UUID(int=1)
    leased_terminal_id = UUID(int=2)
    deletable_terminal_id = UUID(int=3)
    with Session(engine) as session:
        session.add_all(
            [
                TerminalRow(
                    id=terminal_id,
                    installation_id=uuid4(),
                    terminal_type=TerminalType.LINUX.value,
                    display_name=f"managed-{terminal_id.int}",
                    service_status=TerminalServiceStatus.OFFLINE.value,
                    acceptance_status=TerminalAcceptanceStatus.DISABLED.value,
                    agent_version="management-test",
                    row_version=1,
                )
                for terminal_id in (
                    managed_terminal_id,
                    leased_terminal_id,
                    deletable_terminal_id,
                )
            ]
        )
        session.flush()
        session.add(
            TargetDeviceRow(
                id=uuid4(),
                display_name="managed-phone",
                platform="android",
                mode=TargetDeviceMode.MOUNTED.value,
                managing_terminal_id=managed_terminal_id,
                row_version=1,
                updated_at=now,
            )
        )
        session.add(
            ExecutionLeaseRow(
                id=uuid4(),
                terminal_id=leased_terminal_id,
                target_device_id=None,
                lease_kind=LeaseKind.MAINTENANCE.value,
                owner_kind=LeaseOwnerKind.SYSTEM.value,
                owner_id=uuid4(),
                acquired_at=now,
                expires_at=now + timedelta(minutes=5),
                row_version=1,
            )
        )
        session.commit()

    query_count = 0

    def count_query(*_args: object) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        items = service.list_terminal_management(limit=100)
    finally:
        event.remove(engine, "before_cursor_execute", count_query)

    availability = {item.terminal.terminal_id: item.delete for item in items}
    assert query_count == 1
    assert availability[managed_terminal_id].refusal_code == "terminal_manages_target_devices"
    assert availability[leased_terminal_id].refusal_code == "terminal_in_use"
    assert availability[deletable_terminal_id].allowed is True


def test_terminal_registration_and_path_authentication_http_contract(
    service: ExecutionResourceService,
) -> None:
    class Ready:
        def check(self) -> ReadinessResult:
            return ReadinessResult(ready=True, dependencies={})

        def close(self) -> None:
            return

    app = create_app(
        Settings(admin_password="test-resource-password", admin_cookie_secure=False),
        readiness_service=Ready(),  # type: ignore[arg-type]
        execution_resource_service=service,
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
            login = await client.post(
                "/api/v1/auth/login", json={"password": "test-resource-password"},
            )
            assert login.status_code == 200
            grant_response = await client.post(
                "/api/v1/terminals/registration-grants",
                json={"allowed_terminal_type": "linux", "ttl_seconds": 600},
            )
            assert grant_response.status_code == 201
            registration = await client.post(
                "/api/v1/terminals/register",
                json={
                    "registration_code": grant_response.json()["registration_code"],
                    "installation_id": str(uuid4()),
                    "terminal_type": "linux",
                    "display_name": "http-terminal",
                    "agent_version": "http-test",
                },
            )
            assert registration.status_code == 201
            body = registration.json()
            terminal_id = body["terminal"]["terminal_id"]
            credential = body["credential"]
            terminal_page = await client.get("/api/v1/terminals")
            assert terminal_page.status_code == 200
            management = terminal_page.json()["items"][0]
            assert management["delete"] == {
                "allowed": False,
                "refusal_code": "terminal_not_offline",
                "refusal_message": "Terminal must be offline before deletion",
            }
            wrong_path = await client.post(
                f"/api/v1/terminals/{uuid4()}/heartbeat",
                headers={"Authorization": f"Bearer {credential}"},
                json={
                    "expected_version": body["terminal"]["row_version"],
                    "service_status": "online",
                    "acceptance_status": "accepting",
                    "agent_version": "http-test",
                },
            )
            assert wrong_path.status_code == 401
            heartbeat = await client.post(
                f"/api/v1/terminals/{terminal_id}/heartbeat",
                headers={"Authorization": f"Bearer {credential}"},
                json={
                    "expected_version": body["terminal"]["row_version"],
                    "service_status": "online",
                    "acceptance_status": "accepting",
                    "agent_version": "http-test",
                },
            )
            assert heartbeat.status_code == 200
            assert heartbeat.json()["row_version"] == body["terminal"]["row_version"] + 1
            rename = await client.patch(
                f"/api/v1/terminals/{terminal_id}/display-name",
                json={"expected_name_version": 0, "display_name": "  Platform lab  "},
            )
            assert rename.status_code == 200
            assert rename.json()["display_name"] == "Platform lab"
            assert rename.json()["name_version"] == 1
            discovered = await client.post("/api/v1/target-devices/discoveries",
                headers={"Authorization": f"Bearer {credential}"},
                json={"source_type": "adb_serial", "identifier": "http-adb"})
            assert discovered.status_code == 201
            observation_id = discovered.json()["identifier_id"]
            batch = await client.post(
                "/api/v1/target-devices/adb-observations",
                headers={"Authorization": f"Bearer {credential}"},
                json={"items": [{"identifier_id": observation_id, "adb_state": "device"}]},
            )
            assert batch.status_code == 200
            assert batch.json() == {"updated": 1}
            rejected_batch = await client.post(
                "/api/v1/target-devices/adb-observations",
                json={"items": [{"identifier_id": observation_id, "adb_state": "device"}]},
            )
            assert rejected_batch.status_code == 401
            page = await client.get("/api/v1/target-devices/adb-discoveries",
                params={"terminal_id": terminal_id})
            assert page.status_code == 200
            item = page.json()["items"][0]
            connected = await client.post(
                f"/api/v1/target-devices/discoveries/{item['identifier_id']}/connect",
                json={
                    "expected_identifier_version": item["row_version"],
                    "display_name": "HTTP phone",
                },
            )
            assert connected.status_code == 200
            assert connected.json()["mode"] == "mounted"
            assert connected.json()["managing_terminal_id"] == terminal_id
            phone_id = connected.json()["device_id"]
            phone_version = connected.json()["row_version"]
            phone_rename = await client.patch(
                f"/api/v1/target-devices/{phone_id}/display-name",
                json={"expected_version": phone_version, "display_name": "  Test handset  "},
            )
            assert phone_rename.status_code == 200
            assert phone_rename.json()["display_name"] == "Test handset"
            assert phone_rename.json()["device_id"] == phone_id
            assert phone_rename.json()["mode"] == "mounted"
            assert phone_rename.json()["managing_terminal_id"] == terminal_id
            stale_phone_rename = await client.patch(
                f"/api/v1/target-devices/{phone_id}/display-name",
                json={"expected_version": phone_version, "display_name": "Stale"},
            )
            assert stale_phone_rename.status_code == 409
            blank_phone_rename = await client.patch(
                f"/api/v1/target-devices/{phone_id}/display-name",
                json={
                    "expected_version": phone_rename.json()["row_version"],
                    "display_name": "   ",
                },
            )
            assert blank_phone_rename.status_code == 400

    asyncio.run(exercise())


def test_terminal_mqtt_session_http_contract_persists_only_digest(
    engine: Engine,
    service: ExecutionResourceService,
) -> None:
    class Ready:
        def check(self) -> ReadinessResult:
            return ReadinessResult(ready=True, dependencies={})

        def close(self) -> None:
            return

    class Broker:
        def __init__(self) -> None:
            self.sessions: list[TerminalMqttSessionRecord] = []

        def ensure_terminal_subscriber(
            self, session: TerminalMqttSessionRecord, password: str
        ) -> None:
            assert password
            self.sessions.append(session)

        def revoke_terminal_subscriber(self, session: TerminalMqttSessionRecord) -> None:
            del session

    broker = Broker()
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    mqtt_service = TerminalMqttSessionService(
        cast(
            Callable[[], TerminalMqttSessionUnitOfWork],
            lambda: MqttSessionSqlAlchemyUnitOfWork(sessions),
        ),
        broker,
        MqttSessionPasswordSigner("integration-mqtt-signing-key-at-least-32-bytes"),
        public_host="mqtt.example.test",
        public_port=8883,
        tls_enabled=True,
    )
    terminal_id, _installation_id, credential, _version = _register(service, TerminalType.LINUX)
    app = create_app(
        Settings(),
        readiness_service=Ready(),  # type: ignore[arg-type]
        execution_resource_service=service,
        terminal_mqtt_session_service=mqtt_service,
    )

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        ):
            unauthenticated = await client.post("/api/v1/terminal/mqtt-sessions")
            issued = await client.post(
                "/api/v1/terminal/mqtt-sessions",
                headers={"Authorization": f"Bearer {credential}"},
            )
            return unauthenticated, issued

    unauthenticated, issued = asyncio.run(exercise())

    assert unauthenticated.status_code == 401
    assert issued.status_code == 200
    body = issued.json()
    assert body["broker_host"] == "mqtt.example.test"
    assert body["topic"] == f"al1s/v1/terminals/{terminal_id}/hints"
    with Session(engine) as session:
        row = session.scalar(
            select(TerminalMqttSessionRow).where(TerminalMqttSessionRow.terminal_id == terminal_id)
        )
    assert row is not None
    assert row.password_digest != body["password"]
    assert body["password"] not in str(row.__dict__)
    assert [item.session_id for item in broker.sessions] == [UUID(body["session_id"])]
