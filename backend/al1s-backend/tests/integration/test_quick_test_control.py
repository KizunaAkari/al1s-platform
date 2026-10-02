"""Quick-test stop and event contracts on a disposable PostgreSQL database."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

from al1s.adapters.postgres.execution_models import TargetDeviceRow, TerminalRow
from al1s.adapters.postgres.maa_models import (
    MaaApplicationDeviceRow,
    MaaQuickTestEventRow,
    MaaQuickTestSessionRow,
    MaaScriptRow,
    MaaScriptVersionRow,
)
from al1s.adapters.postgres.technical_retention import PostgresTechnicalRetention
from al1s.maa.errors import MaaDomainError
from al1s.maa.types import QuickTestEventRecord, QuickTestSessionStatus
from tests.integration.maa_support import _quick_test_service, bundle, setup_import
from tests.integration.maa_support import clean_maa_tables as clean_maa_tables
from tests.integration.maa_support import engine as engine

pytestmark = pytest.mark.integration


def _issued(engine: Engine, *, key: str):
    importer, application, target = setup_import(engine)
    importer.import_archive(
        bundle(), correlation_id=uuid4(),
        confirmed_overwrites={target.script_id: target.row_version},
    )
    with Session(engine) as session:
        script = session.get(MaaScriptRow, target.script_id)
        assert script is not None and script.current_version_id is not None
        version = session.get(MaaScriptVersionRow, script.current_version_id)
        assert version is not None
        candidate_id = version.id
    terminal_id, device_id = uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        session.add(TerminalRow(
            id=terminal_id, installation_id=uuid4(), terminal_type="linux",
            display_name="debug terminal", service_status="online",
            acceptance_status="accepting", agent_version="test", row_version=1,
        ))
        session.flush()
        session.add(TargetDeviceRow(
            id=device_id, display_name="debug phone", platform="android",
            mode="mounted", managing_terminal_id=terminal_id, row_version=1,
        ))
        session.flush()
        session.add(MaaApplicationDeviceRow(
            id=uuid4(), application_id=application.application_id,
            device_id=device_id, package_name=application.package_name,
            created_at=datetime.now(UTC), deleted_at=None,
        ))
    service = _quick_test_service(engine)
    issued = service.issue(
        script_id=target.script_id, candidate_version_id=candidate_id,
        terminal_id=terminal_id, target_device_id=device_id,
        idempotency_key=key, ttl=timedelta(minutes=15),
    )
    return service, issued, terminal_id


def test_unclaimed_stop_prevents_terminal_claim(engine: Engine) -> None:
    service, issued, terminal_id = _issued(engine, key="stop-before-claim")
    session = issued.session
    stopped = service.request_cancel(script_id=session.script_id, session_id=session.session_id)
    assert stopped.status is QuickTestSessionStatus.CANCELLED
    assert stopped.cancel_requested_at is not None
    assert service.request_cancel(
        script_id=session.script_id, session_id=session.session_id
    ) == stopped
    assert service.list_pending(terminal_id, limit=10) == ()
    with pytest.raises(MaaDomainError):
        service.claim(session.session_id, terminal_id)


def test_claimed_stop_requires_terminal_result_and_events_are_ordered(engine: Engine) -> None:
    service, issued, terminal_id = _issued(engine, key="stop-during-run")
    session = issued.session
    service.claim(session.session_id, terminal_id)
    requested = service.request_cancel(script_id=session.script_id, session_id=session.session_id)
    assert requested.status is QuickTestSessionStatus.CLAIMED
    assert requested.cancel_requested_at is not None
    assert service.get_control(session_id=session.session_id, terminal_id=terminal_id) == requested
    with pytest.raises(MaaDomainError):
        service.get_control(session_id=session.session_id, terminal_id=uuid4())

    now = datetime.now(UTC)
    events = (
        QuickTestEventRecord(session.session_id, 1, "started", None, None, now),
        QuickTestEventRecord(session.session_id, 2, "step_started", 1, None, now),
    )
    assert service.report_events(session_id=session.session_id, terminal_id=terminal_id,
                                 events=events) == 2
    assert service.report_events(session_id=session.session_id, terminal_id=terminal_id,
                                 events=events) == 2
    detail = service.get_detail(script_id=session.script_id, session_id=session.session_id)
    assert detail.session.started_at is not None
    assert [item.sequence for item in service.list_events(
        script_id=session.script_id, session_id=session.session_id, after=0, limit=10
    )] == [1, 2]
    with pytest.raises(MaaDomainError, match="sequence"):
        service.report_events(session_id=session.session_id, terminal_id=terminal_id,
                              events=(QuickTestEventRecord(
                                  session.session_id, 4, "step_succeeded", 1, None, now,
                              ),))
    completed = service.record_result(
        session_id=session.session_id, script_id=session.script_id,
        authenticated_terminal_id=terminal_id,
        candidate_version_id=session.script_version_id,
        manifest_hash=session.manifest_hash,
        definition_hash=session.definition_hash,
        executor_version="test-runtime", passed=False,
        error_code="execution_cancelled", idempotency_key="stopped-result",
        correlation_id=uuid4(),
    )
    assert completed.session.status is QuickTestSessionStatus.COMPLETED
    assert completed.session.cancel_requested_at is not None
    assert completed.receipt.error_code == "execution_cancelled"
    old = now - timedelta(days=8)
    with Session(engine) as db, db.begin():
        db.execute(update(MaaQuickTestSessionRow).where(
            MaaQuickTestSessionRow.id == session.session_id,
        ).values(created_at=old))
        db.execute(update(MaaQuickTestEventRow).where(
            MaaQuickTestEventRow.session_id == session.session_id,
            MaaQuickTestEventRow.sequence == 1,
        ).values(created_at=old))
    with Session(engine) as db, db.begin():
        PostgresTechnicalRetention(db, owner_module="maa").redact(now, limit=50)
    replay = (
        QuickTestEventRecord(session.session_id, 1, "started", None, None, old),
        events[1],
        QuickTestEventRecord(session.session_id, 3, "step_succeeded", 1, None, now),
    )
    assert service.report_events(
        session_id=session.session_id, terminal_id=terminal_id, events=replay,
    ) == 3
    assert [item.sequence for item in service.list_events(
        script_id=session.script_id, session_id=session.session_id, after=0, limit=10,
    )] == [2, 3]
    with Session(engine) as db, db.begin():
        db.execute(update(MaaQuickTestEventRow).where(
            MaaQuickTestEventRow.session_id == session.session_id,
        ).values(created_at=old))
    with Session(engine) as db, db.begin():
        PostgresTechnicalRetention(db, owner_module="maa").redact(now, limit=50)
    assert service.list_events(
        script_id=session.script_id, session_id=session.session_id, after=0, limit=10
    ) == ()
    assert service.report_events(
        session_id=session.session_id, terminal_id=terminal_id,
        events=(QuickTestEventRecord(
            session.session_id, 4, "log", None, "late_result", now,
        ),),
    ) == 4
