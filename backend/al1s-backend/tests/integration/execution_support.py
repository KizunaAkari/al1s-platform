"""Shared isolated scheduling integration fixtures and builders."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.execution_unit_of_work import ExecutionSqlAlchemyUnitOfWork
from al1s.adapters.postgres.scheduling_unit_of_work import (
    SchedulingSqlAlchemyUnitOfWork,
)
from al1s.execution.definitions import (
    ExecutionDefinitionRegistry,
    canonical_manifest_hash,
)
from al1s.execution.delivery_service import TerminalDeliveryService
from al1s.execution.offline_permits import OfflinePermitSigner
from al1s.execution.ports import ExecutionUnitOfWork
from al1s.execution.scheduling_ports import SchedulingUnitOfWork
from al1s.execution.scheduling_service import ExecutionSchedulingService
from al1s.execution.scheduling_types import (
    CapabilityRequirements,
    CreateTaskCommand,
    LoopScheduleSpec,
    ResolvedExecutionDefinition,
    TaskType,
    TimedScheduleSpec,
)
from al1s.execution.services import ExecutionResourceService
from al1s.execution.timeout_service import ExecutionTimeoutService
from al1s.execution.types import (
    CapabilityManifest,
    TargetDeviceMode,
    TerminalAcceptanceStatus,
    TerminalServiceStatus,
    TerminalType,
)

TEST_OFFLINE_SIGNER = OfflinePermitSigner("test-offline-permit-signing-key-32-bytes-minimum")


class FakeDefinitionProvider:
    def __init__(self) -> None:
        self.available = True
        self.resolve_count = 0
        self.revision_id = "revision-1"

    def resolve(
        self, logical_content_id: str, parameters: dict[str, object]
    ) -> ResolvedExecutionDefinition:
        if not self.available:
            raise AssertionError("provider must not be consulted for an idempotent replay")
        self.resolve_count += 1
        manifest = {
            "logical_content_id": logical_content_id,
            "entry": "main",
            "parameters_schema": 1,
        }
        return ResolvedExecutionDefinition(
            revision_id=self.revision_id,
            schema_version=1,
            manifest_hash=canonical_manifest_hash(manifest),
            manifest=manifest,
            capability_requirements=CapabilityRequirements(
                provider_keys=("maa",),
                requires_target_device=True,
            ),
        )


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
    table_names = (
        "command_acknowledgements, terminal_reports, commands, "
        "offline_start_permits, task_packages, state_transitions, "
        "task_retry_origins, execution_snapshot_blobs, "
        "execution_snapshots, execution_attempts, executions, plan_occurrences, "
        "task_schedule_revision_times, task_schedule_revisions, task_schedules, "
        "task_requests, execution_leases, target_device_identifiers, "
        "target_devices, terminal_capability_profiles, terminal_credentials, "
        "terminal_registration_grants, terminals, audit_logs, outbox_events"
    )
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {table_names} CASCADE"))
    yield
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {table_names} CASCADE"))


def _services(
    engine: Engine,
    clock: list[datetime],
    provider: FakeDefinitionProvider | None = None,
) -> tuple[ExecutionResourceService, ExecutionSchedulingService]:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def resource_uow() -> ExecutionUnitOfWork:
        return cast(ExecutionUnitOfWork, ExecutionSqlAlchemyUnitOfWork(sessions))

    def scheduling_uow() -> SchedulingUnitOfWork:
        return cast(SchedulingUnitOfWork, SchedulingSqlAlchemyUnitOfWork(sessions))

    definitions = ExecutionDefinitionRegistry()
    # Generic scheduling fixtures use a test module, not a fictitious Maa identity.
    definitions.register("test", provider or FakeDefinitionProvider())

    def now() -> datetime:
        return clock[0]

    return (
        ExecutionResourceService(resource_uow, now=now),
        ExecutionSchedulingService(scheduling_uow, definitions, now=now),
    )


def _delivery_services(
    engine: Engine, clock: list[datetime]
) -> tuple[TerminalDeliveryService, ExecutionTimeoutService]:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def scheduling_uow() -> SchedulingUnitOfWork:
        return cast(SchedulingUnitOfWork, SchedulingSqlAlchemyUnitOfWork(sessions))

    def now() -> datetime:
        return clock[0]

    return (
        TerminalDeliveryService(
            scheduling_uow,
            now=now,
            offline_permit_signer=TEST_OFFLINE_SIGNER,
        ),
        ExecutionTimeoutService(scheduling_uow, now=now),
    )


def _prepare_terminal_with_credential(
    resource_service: ExecutionResourceService,
) -> tuple[UUID, UUID, str]:
    grant = resource_service.create_registration_grant(
        allowed_terminal_type=TerminalType.LINUX,
        ttl=timedelta(minutes=10),
        correlation_id=uuid4(),
    )
    registered = resource_service.register_terminal(
        registration_code=grant.registration_code,
        installation_id=uuid4(),
        terminal_type=TerminalType.LINUX,
        display_name="stage3b-terminal",
        agent_version="3b-test",
        correlation_id=uuid4(),
    )
    terminal = resource_service.heartbeat(
        credential=registered.credential,
        expected_version=registered.terminal.row_version,
        service_status=TerminalServiceStatus.ONLINE,
        acceptance_status=TerminalAcceptanceStatus.ACCEPTING,
        agent_version="3b-test",
        correlation_id=uuid4(),
    )
    resource_service.publish_capability(
        credential=registered.credential,
        revision=1,
        manifest=CapabilityManifest(
            schema_version=1,
            protocol_version=1,
            agent_version="3b-test",
            os_name="linux",
            os_version="test",
            architecture="aarch64",
            cpu_cores=8,
            memory_bytes=2 * 1024**3,
            storage_available_bytes=20 * 1024**3,
            accelerator_type="rk3576",
            low_resource=True,
            provider_keys=("maa", "rknn"),
            details={},
        ),
        correlation_id=uuid4(),
    )
    target = resource_service.create_target_device(
        display_name="test-phone", correlation_id=uuid4()
    )
    mounted = resource_service.set_target_mode(
        device_id=target.device_id,
        expected_version=target.row_version,
        mode=TargetDeviceMode.MOUNTED,
        managing_terminal_id=terminal.terminal_id,
        correlation_id=uuid4(),
    )
    return terminal.terminal_id, mounted.device_id, registered.credential


def _prepare_terminal(resource_service: ExecutionResourceService) -> tuple[UUID, UUID]:
    terminal_id, target_id, _ = _prepare_terminal_with_credential(resource_service)
    return terminal_id, target_id


def _loop_command(target_id: UUID, *, key: str = "loop-key") -> CreateTaskCommand:
    return CreateTaskCommand(
        idempotency_key=key,
        name="loop test",
        task_type=TaskType.LOOP,
        source_module="test",
        logical_content_id="strategy-1",
        parameters={"mode": "test"},
        requested_terminal_id=None,
        requested_target_device_id=target_id,
        timeout_seconds=300,
        max_retries=1,
        record_video=True,
        loop=LoopScheduleSpec(repeat_count=2),
    )


def _single_command(target_id: UUID, *, key: str = "single-key") -> CreateTaskCommand:
    return replace(
        _loop_command(target_id, key=key),
        name="single test",
        task_type=TaskType.SINGLE,
        loop=None,
    )


def _timed_command(target_id: UUID, *, key: str = "timed-key") -> CreateTaskCommand:
    return CreateTaskCommand(
        idempotency_key=key,
        name="timed test",
        task_type=TaskType.TIMED,
        source_module="test",
        logical_content_id="strategy-1",
        parameters={},
        requested_terminal_id=None,
        requested_target_device_id=target_id,
        timeout_seconds=300,
        max_retries=0,
        record_video=False,
        timed=TimedScheduleSpec(
            timezone="Asia/Shanghai",
            start_date=date(2026, 8, 29),
            end_date=date(2026, 8, 29),
            daily_times=(time(12, 0), time(15, 0)),
        ),
    )
