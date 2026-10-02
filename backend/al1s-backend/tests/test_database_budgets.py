from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, TimeoutError

from al1s.api.database_errors import install_database_error_handlers
from al1s.app.config import Settings
from al1s.infrastructure import database


def test_engine_keeps_capacity_and_configures_separate_wait_budgets(monkeypatch):
    create = Mock()
    monkeypatch.setattr(database, "create_engine", create)
    database.create_database_engine(Settings(_env_file=None))
    options = create.call_args.kwargs
    assert options["pool_size"] == options["max_overflow"] == 5
    assert options["pool_timeout"] == 1
    assert options["connect_args"] == {
        "connect_timeout": 2,
        "options": "-c statement_timeout=5000 -c lock_timeout=1000 "
        "-c idle_in_transaction_session_timeout=15000",
    }


@pytest.mark.parametrize(
    "setting,value",
    [
        ("database_pool_size", 0),
        ("database_max_overflow", -1),
        ("database_pool_timeout_seconds", 0),
        ("database_statement_timeout_ms", 0),
        ("database_lock_timeout_ms", -1),
        ("database_batch_statement_timeout_ms", 0),
    ],
)
def test_invalid_budgets_cannot_disable_limits(setting, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{setting: value})


@pytest.mark.parametrize(
    "sqlstate,status,code",
    [
        ("57014", 503, "database_busy"),
        ("55P03", 503, "database_busy"),
        ("23505", 500, "internal_error"),
        (None, 500, "internal_error"),
    ],
)
def test_database_errors_are_retryable_only_for_budget_failures(sqlstate, status, code):
    class DriverError(Exception):
        pass

    original = DriverError("private database error")
    original.sqlstate = sqlstate
    app = FastAPI()
    install_database_error_handlers(app)

    @app.get("/failure")
    def fail():
        raise DBAPIError("SQL containing secrets", {"secret": "private-value"}, original)

    response = TestClient(app).get("/failure")
    assert response.status_code == status
    assert response.json()["code"] == code
    assert "private" not in response.text and "SQL" not in response.text
    if status == 503:
        assert response.headers["Retry-After"] == "1"


def test_pool_exhaustion_is_a_safe_retryable_503():
    app = FastAPI()
    install_database_error_handlers(app)

    @app.get("/failure")
    def fail():
        raise TimeoutError("private pool details")

    response = TestClient(app).get("/failure")
    assert response.status_code == 503
    assert response.json()["code"] == "database_busy"
    assert "private" not in response.text
