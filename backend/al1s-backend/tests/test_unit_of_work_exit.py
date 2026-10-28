"""Protect the platform's explicit-commit transaction contract."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from al1s.adapters.postgres.bot_unit_of_work import BotSqlAlchemyUnitOfWork
from al1s.adapters.postgres.execution_unit_of_work import ExecutionSqlAlchemyUnitOfWork
from al1s.adapters.postgres.maa_unit_of_work import MaaSqlAlchemyUnitOfWork
from al1s.adapters.postgres.notification_unit_of_work import NotificationSqlAlchemyUnitOfWork
from al1s.adapters.postgres.scheduling_unit_of_work import SchedulingSqlAlchemyUnitOfWork
from al1s.adapters.postgres.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.parametrize("uow_type", [
    SqlAlchemyUnitOfWork, BotSqlAlchemyUnitOfWork, ExecutionSqlAlchemyUnitOfWork,
    MaaSqlAlchemyUnitOfWork, NotificationSqlAlchemyUnitOfWork, SchedulingSqlAlchemyUnitOfWork,
])


def _unit_of_work(uow_type) -> tuple[SqlAlchemyUnitOfWork, MagicMock]:
    session = MagicMock(spec=Session)
    session.in_transaction.return_value = True
    return uow_type(lambda: session), session


def test_normal_exit_without_commit_rolls_back(uow_type) -> None:
    uow, session = _unit_of_work(uow_type)
    with uow:
        pass
    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()
    session.close.assert_called_once_with()


def test_explicit_commit_survives_normal_exit(uow_type) -> None:
    uow, session = _unit_of_work(uow_type)
    session.in_transaction.return_value = False
    with uow:
        uow.commit()
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()


def test_exception_or_early_return_does_not_commit(uow_type) -> None:
    uow, session = _unit_of_work(uow_type)

    def leave_early() -> None:
        with uow:
            return

    leave_early()
    session.rollback.assert_called_once_with()
    session.reset_mock()
    with pytest.raises(ValueError, match="failed"), uow:
        raise ValueError("failed")
    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()


def test_caught_exception_still_requires_explicit_commit(uow_type) -> None:
    uow, session = _unit_of_work(uow_type)
    with uow:
        try:
            raise ValueError("handled")
        except ValueError:
            pass
    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()


def test_real_session_events_keep_explicit_commit_and_allow_reuse(uow_type) -> None:
    # No engine or SQL: exercise SQLAlchemy's actual transaction lifecycle.
    session = Session()
    outcomes = []
    event.listen(session, "after_commit", lambda _: outcomes.append("commit"))
    event.listen(session, "after_rollback", lambda _: outcomes.append("rollback"))
    uow = uow_type(lambda: session)
    with uow:
        session.begin()
    assert outcomes == ["rollback"]
    assert not session.in_transaction()
    with uow:
        session.begin()
        uow.commit()
    assert outcomes == ["rollback", "commit"]
    with pytest.raises(RuntimeError, match="not active"):
        uow.commit()


def test_reentrant_entry_rejects_without_replacing_active_session(uow_type) -> None:
    uow, session = _unit_of_work(uow_type)
    with uow, pytest.raises(RuntimeError, match="entered twice"):
        uow.__enter__()
    session.close.assert_called_once_with()


def test_rollback_failure_still_closes_and_releases_session(uow_type) -> None:
    uow, session = _unit_of_work(uow_type)
    session.rollback.side_effect = RuntimeError("rollback failed")
    with pytest.raises(RuntimeError, match="rollback failed"), uow:
        pass
    session.close.assert_called_once_with()
    with pytest.raises(RuntimeError, match="not active"):
        uow.commit()
