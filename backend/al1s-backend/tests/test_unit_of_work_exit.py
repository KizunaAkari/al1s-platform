"""Protect the platform's explicit-commit transaction contract."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from al1s.adapters.postgres.unit_of_work import SqlAlchemyUnitOfWork


def _unit_of_work() -> tuple[SqlAlchemyUnitOfWork, MagicMock]:
    session = MagicMock(spec=Session)
    session.in_transaction.return_value = True
    return SqlAlchemyUnitOfWork(lambda: session), session


def test_normal_exit_without_commit_rolls_back() -> None:
    uow, session = _unit_of_work()
    with uow:
        pass
    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()
    session.close.assert_called_once_with()


def test_explicit_commit_survives_normal_exit() -> None:
    uow, session = _unit_of_work()
    session.in_transaction.return_value = False
    with uow:
        uow.commit()
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()


def test_exception_or_early_return_does_not_commit() -> None:
    uow, session = _unit_of_work()

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


def test_caught_exception_still_requires_explicit_commit() -> None:
    uow, session = _unit_of_work()
    with uow:
        try:
            raise ValueError("handled")
        except ValueError:
            pass
    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()
