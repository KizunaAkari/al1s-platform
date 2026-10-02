from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from al1s.adapters.postgres.editor_models import EditorSessionRow
from al1s.adapters.postgres.execution_models import TargetDeviceRow
from al1s.adapters.postgres.scheduling_models import ExecutionRow
from al1s.adapters.postgres.target_control import lock_target_assignment
from al1s.execution.delivery_types import PackageReceiptDisposition
from al1s.execution.errors import ConflictError
from al1s.execution.types import TargetDeviceMode
from tests.integration.execution_support import (
    _delivery_services,
    _prepare_terminal,
    _services,
    _single_command,
)
from tests.integration.execution_support import (
    clean_tables as _clean_tables,
)
from tests.integration.execution_support import (
    engine as _engine,
)

pytestmark = pytest.mark.integration
engine = _engine
clean_tables = _clean_tables


def unassign(service, engine, target):
    with Session(engine) as session:
        version = session.get(TargetDeviceRow, target).row_version
    return service.set_target_mode(
        device_id=target,
        expected_version=version,
        mode=TargetDeviceMode.UNASSIGNED,
        managing_terminal_id=None,
        correlation_id=uuid4(),
    )


@pytest.mark.parametrize("accept", [False, True])
def test_unreceived_work_and_issued_offline_permit_block_handoff(engine, accept):
    clock = [datetime(2026, 9, 21, tzinfo=UTC)]
    resources, scheduling = _services(engine, clock)
    terminal, target = _prepare_terminal(resources)
    scheduling.create_task(_single_command(target), correlation_id=uuid4())
    scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())
    if accept:
        delivery, _ = _delivery_services(engine, clock)
        command = delivery.list_commands(terminal)[0]
        receipt = delivery.receive_package(
            terminal_id=terminal,
            package_id=command.package_id,
            attempt_id=command.attempt_id,
            command_id=command.command_id,
            report_id=uuid4(),
            disposition=PackageReceiptDisposition.ACCEPTED,
            rejection_code=None,
            diagnostic=None,
            occurred_at=clock[0],
            correlation_id=uuid4(),
        )
        assert receipt.offline_start_permit is not None
        # Even if execution history is already terminal, an unreconciled issued
        # permit must independently prevent reassignment.
        with Session(engine) as session:
            session.execute(
                update(ExecutionRow)
                .where(ExecutionRow.target_device_id == target)
                .values(status="ended", result="failure", ended_at=clock[0])
            )
            session.commit()
    with pytest.raises(ConflictError) as caught:
        unassign(resources, engine, target)
    assert caught.value.code == "target_control_pending"
    with Session(engine) as session:
        assert session.get(TargetDeviceRow, target).managing_terminal_id == terminal


@pytest.mark.parametrize("status", ["pending", "closing"])
def test_editor_must_finish_before_handoff(engine, status):
    now = datetime(2026, 9, 21, tzinfo=UTC)
    resources, _ = _services(engine, [now])
    terminal, target = _prepare_terminal(resources)
    identity = uuid4()
    with Session(engine) as session:
        session.add(
            EditorSessionRow(
                id=identity,
                terminal_id=terminal,
                device_id=target,
                request_id=uuid4(),
                status=status,
                created_at=now,
                updated_at=now,
                create_deadline=now + timedelta(minutes=1),
                row_version=1,
            )
        )
        session.commit()
    with pytest.raises(ConflictError) as caught:
        unassign(resources, engine, target)
    assert caught.value.code == "target_control_pending"
    with Session(engine) as session:
        session.get(EditorSessionRow, identity).status = "closed"
        session.commit()
    assert unassign(resources, engine, target).mode is TargetDeviceMode.UNASSIGNED
    with Session(engine) as session, pytest.raises(ConflictError):
        lock_target_assignment(session, target, terminal)


def test_control_creation_and_handoff_share_the_target_lock(engine):
    now = datetime(2026, 9, 21, tzinfo=UTC)
    resources, _ = _services(engine, [now])
    terminal, target = _prepare_terminal(resources)
    entered = Event()

    def handoff():
        entered.set()
        try:
            unassign(resources, engine, target)
        except ConflictError as exc:
            return exc.code
        return "unexpected_success"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(engine) as session:
            lock_target_assignment(session, target, terminal)
            future = pool.submit(handoff)
            assert entered.wait(3)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
            session.add(
                EditorSessionRow(
                    id=uuid4(),
                    terminal_id=terminal,
                    device_id=target,
                    request_id=uuid4(),
                    status="pending",
                    created_at=now,
                    updated_at=now,
                    create_deadline=now + timedelta(minutes=1),
                    row_version=1,
                )
            )
            session.commit()
        assert future.result(timeout=5) == "target_control_pending"
