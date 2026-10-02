"""Expired accepted permits are settled only after platform reconciliation."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from al1s.execution.delivery_service import TerminalDeliveryService
from al1s.execution.delivery_types import PackageStatus
from al1s.execution.errors import ConflictError
from al1s.execution.offline_permits import OfflinePermitSigner, OfflinePermitStatus
from al1s.execution.scheduling_types import AttemptStatus, ExecutionStatus, TaskLifecycleStatus

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _case(*, expired: bool = True) -> tuple[TerminalDeliveryService, MagicMock, dict]:
    ids = {name: uuid4() for name in ("terminal", "attempt", "package", "permit", "report")}
    execution_id = uuid4()
    task_id = uuid4()
    package = SimpleNamespace(
        package_id=ids["package"], terminal_id=ids["terminal"],
        attempt_id=ids["attempt"], execution_id=execution_id,
        package_hash="a" * 64, status=PackageStatus.ACCEPTED, row_version=2,
    )
    attempt = SimpleNamespace(
        attempt_id=ids["attempt"], execution_id=execution_id,
        status=AttemptStatus.QUEUED, started_at=None, row_version=1,
    )
    execution = SimpleNamespace(
        execution_id=execution_id, task_request_id=task_id, terminal_id=ids["terminal"],
        status=ExecutionStatus.QUEUED, started_at=None, cancel_requested_at=None, row_version=2,
    )
    task = SimpleNamespace(task_id=task_id, lifecycle_status=TaskLifecycleStatus.ACTIVE)
    permit = SimpleNamespace(
        permit_id=ids["permit"], terminal_id=ids["terminal"],
        package_id=ids["package"], execution_id=execution_id,
        package_hash=package.package_hash, status=OfflinePermitStatus.ISSUED,
        expires_at=NOW - timedelta(seconds=1) if expired else NOW + timedelta(seconds=1),
        row_version=1,
    )
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.terminal_reports.get.return_value = None
    uow.packages.execution_route_for_package.return_value = (task_id, execution_id)
    uow.tasks.get_for_reconciliation.return_value = task
    uow.packages.get_for_update.return_value = package
    uow.attempts.get_for_update.return_value = attempt
    uow.executions.get_for_update.return_value = execution
    uow.offline_permits.get_by_attempt_for_update.return_value = permit
    uow.packages.cancel.return_value = package
    uow.offline_permits.revoke.return_value = permit
    service = TerminalDeliveryService(
        lambda: uow,
        now=lambda: NOW,
        offline_permit_signer=OfflinePermitSigner("private-test-key-" * 3),
    )
    return service, uow, ids


def _settle(service: TerminalDeliveryService, ids: dict) -> object:
    return service.settle_expired_prestart(
        terminal_id=ids["terminal"], attempt_id=ids["attempt"],
        package_id=ids["package"], permit_id=ids["permit"],
        report_id=ids["report"], occurred_at=NOW, correlation_id=uuid4(),
    )


def test_expired_prestart_settles_once_and_reuses_report() -> None:
    service, uow, ids = _case()
    ended_attempt = SimpleNamespace(
        status=AttemptStatus.ENDED,
        execution_id=uow.attempts.get_for_update.return_value.execution_id,
    )
    ended_execution = SimpleNamespace(
        status=ExecutionStatus.ENDED,
        execution_id=uow.executions.get_for_update.return_value.execution_id,
        task_request_id=uow.tasks.get_for_reconciliation.return_value.task_id,
        terminal_id=ids["terminal"],
    )
    with patch(
        "al1s.execution.prestart_failure.settle_delivery_failure",
        return_value=(ended_attempt, ended_execution),
    ) as settle, patch("al1s.execution.prestart_failure.record_delivery_event"):
        first = _settle(service, ids)
        assert first.report.error_code == "offline_permit_expired"
        assert first.report.lease_id is None
        assert first.attempt.status is AttemptStatus.ENDED
        assert first.execution.status is ExecutionStatus.ENDED
        uow.packages.cancel.assert_called_once()
        uow.offline_permits.revoke.assert_called_once()
        uow.terminal_reports.add.assert_called_once()
        settle.assert_called_once()
        uow.commit.assert_called_once()

        uow.terminal_reports.get.return_value = first.report
        uow.attempts.get_for_update.return_value = ended_attempt
        uow.executions.get_for_update.return_value = ended_execution
        second = _settle(service, ids)
        assert second.report == first.report
        uow.packages.cancel.assert_called_once()
        settle.assert_called_once()
        uow.commit.assert_called_once()


def test_valid_permit_and_wrong_binding_cannot_be_settled() -> None:
    service, uow, ids = _case(expired=False)
    with pytest.raises(ConflictError, match="still valid"):
        _settle(service, ids)
    uow.packages.cancel.assert_not_called()
    uow.commit.assert_not_called()

    uow.offline_permits.get_by_attempt_for_update.return_value.permit_id = uuid4()
    with pytest.raises(ConflictError, match="invalid"):
        _settle(service, ids)
    uow.packages.cancel.assert_not_called()


def test_pending_cancellation_keeps_original_settlement_path() -> None:
    service, uow, ids = _case()
    uow.executions.get_for_update.return_value.cancel_requested_at = NOW
    with pytest.raises(ConflictError, match="no longer queued"):
        _settle(service, ids)
    uow.packages.cancel.assert_not_called()
    uow.commit.assert_not_called()


def test_platform_cancelled_prestart_is_confirmed_without_settling_again() -> None:
    service, uow, ids = _case()
    uow.tasks.get_for_reconciliation.return_value.lifecycle_status = TaskLifecycleStatus.CANCELLED
    uow.attempts.get_for_update.return_value.status = AttemptStatus.CANCELLED
    uow.executions.get_for_update.return_value.status = ExecutionStatus.CANCELLED

    first = _settle(service, ids)
    assert first.report.disposition.value == "stale"
    assert first.report.error_code == "task_cancelled"
    assert first.attempt.status is AttemptStatus.CANCELLED
    assert first.execution.status is ExecutionStatus.CANCELLED
    uow.packages.cancel.assert_not_called()
    uow.offline_permits.revoke.assert_not_called()
    uow.terminal_reports.add.assert_called_once()
    uow.commit.assert_called_once()

    uow.terminal_reports.get.return_value = first.report
    second = _settle(service, ids)
    assert second.report == first.report
    uow.terminal_reports.add.assert_called_once()
    uow.commit.assert_called_once()


def test_cancelled_but_started_attempt_is_not_prestart_confirmation() -> None:
    service, uow, ids = _case()
    uow.tasks.get_for_reconciliation.return_value.lifecycle_status = TaskLifecycleStatus.CANCELLED
    uow.attempts.get_for_update.return_value.status = AttemptStatus.CANCELLED
    uow.attempts.get_for_update.return_value.started_at = NOW
    uow.executions.get_for_update.return_value.status = ExecutionStatus.CANCELLED
    with pytest.raises(ConflictError, match="no longer queued"):
        _settle(service, ids)
    uow.terminal_reports.add.assert_not_called()


def test_prestart_locks_task_before_execution_and_attempt() -> None:
    service, uow, ids = _case(expired=False)
    with pytest.raises(ConflictError, match="still valid"):
        _settle(service, ids)
    calls = [str(call) for call in uow.mock_calls]
    task_lock = next(i for i, call in enumerate(calls) if "tasks.get_for_reconciliation(" in call)
    assert task_lock < next(
        i for i, call in enumerate(calls) if "executions.get_for_update(" in call
    ) < next(i for i, call in enumerate(calls) if "attempts.get_for_update(" in call)
