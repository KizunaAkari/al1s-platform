import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres import release_models  # noqa: F401
from al1s.adapters.postgres.execution_models import TerminalRow
from al1s.adapters.postgres.maintenance_repository import MaintenanceRepository
from al1s.adapters.postgres.models import OutboxEventRow
from al1s.execution.errors import ConflictError
from al1s.execution.host_management import HostCommand
from al1s.execution.maintenance_types import MaintenanceRecord


def test_reservation_timeout_late_result_and_duplicate_are_atomic():
    url = os.environ.get("AL1S_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated PostgreSQL required")
    engine = create_engine(url)
    now, terminal, identity = datetime.now(UTC), uuid4(), uuid4()
    with engine.connect() as connection:
        transaction = connection.begin()
        sessions = sessionmaker(bind=connection, expire_on_commit=False,
                                join_transaction_mode="create_savepoint")
        try:
            with sessions.begin() as session:
                session.add(TerminalRow(id=terminal, installation_id=uuid4(),
                    terminal_type="linux", display_name="maintenance-test",
                    service_status="online", acceptance_status="accepting", agent_version="test",
                    row_version=1, last_seen_at=now))
            repository = MaintenanceRepository(sessions)
            value = MaintenanceRecord(identity, terminal, "restart_host", "a" * 64, "pending",
                now, now + timedelta(seconds=600), None, None, None, 0, None)
            assert repository.reserve(value)[1]
            assert not repository.reserve(value)[1]
            competing = MaintenanceRecord(uuid4(), terminal, "restart_host", "b" * 64, "pending",
                now, now + timedelta(seconds=600), None, None, None, 0, None)
            with pytest.raises(ConflictError):
                repository.reserve(competing)
            remote = HostCommand(command_id=identity, action="restart_host", state="recovering",
                accepted_at=now.timestamp(), started_at=(now + timedelta(seconds=10)).timestamp(),
                expires_at=(now + timedelta(seconds=600)).timestamp(), error_code=None, version=3)
            assert repository.observe(terminal, identity, now + timedelta(seconds=20),
                                      remote).state == "recovering"
            older = remote.model_copy(update={"state": "accepted", "version": 1})
            assert repository.observe(terminal, identity, now + timedelta(seconds=30),
                                      older).state == "recovering"
            timeout = repository.observe(terminal, identity, now + timedelta(seconds=610))
            assert timeout.state == "recovery_timeout"
            repository.observe(terminal, identity, now + timedelta(seconds=611))
            late = remote.model_copy(update={"state": "succeeded", "version": 4})
            settled = repository.observe(terminal, identity, now + timedelta(seconds=620), late)
            assert settled.state == "recovery_timeout" and settled.late_state == "succeeded"
            with sessions() as session:
                assert session.scalar(select(func.count()).select_from(OutboxEventRow).where(
                    OutboxEventRow.aggregate_id == terminal,
                    OutboxEventRow.event_type == "terminal.maintenance_failed.v1")) == 1
            assert repository.latest(terminal).command_id == identity
            assert repository.reserve(competing)[1]
            cancelled = HostCommand(
                command_id=competing.command_id, action="restart_host", state="cancelled",
                accepted_at=now.timestamp(), started_at=None,
                expires_at=(now + timedelta(seconds=600)).timestamp(),
                error_code="cancelled_by_user", version=2,
            )
            result = repository.observe(terminal, competing.command_id, now, cancelled)
            assert result.state == "cancelled"
            assert not result.response()["can_cancel"]
            with sessions() as session:
                assert session.scalar(select(func.count()).select_from(OutboxEventRow).where(
                    OutboxEventRow.aggregate_id == terminal,
                    OutboxEventRow.event_type == "terminal.maintenance_failed.v1")) == 1
        finally:
            transaction.rollback()
    engine.dispose()
