from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import TypeAdapter, ValidationError
import pytest

from al1s.adapters.postgres.failure_review import PostgresFailureReview
from al1s.api import task_failure_details
from al1s.execution.failure_review_service import FailureReviewService


def test_filters_by_owned_task_execution_and_cursor_in_one_bounded_query():
    task, execution, cursor = uuid4(), uuid4(), uuid4()
    session = MagicMock()
    session.execute.return_value.all.return_value = []
    sessions = MagicMock()
    sessions.return_value.__enter__.return_value = session
    result = PostgresFailureReview(sessions).list_attempts(task, cursor, datetime.now(UTC), execution_id=execution)
    query = session.execute.call_args.args[0]
    compiled = query.compile()
    assert {task, execution, cursor}.issubset(set(compiled.params.values()))
    assert query._limit_clause.value == 21
    assert 'task_requests.deleted_at IS NULL' in str(query)
    assert result['items'] == []
    session.execute.assert_called_once()
    session.scalars.assert_not_called()


def test_service_preserves_the_old_call_and_forwards_an_explicit_execution():
    repository = MagicMock()
    now = datetime.now(UTC)
    service = FailureReviewService(repository, MagicMock(), clock=lambda: now)
    task, execution = uuid4(), uuid4()
    service.details(task, None)
    repository.list_attempts.assert_called_with(task, None, now)
    service.details(task, None, execution_id=execution)
    repository.list_attempts.assert_called_with(task, None, now, execution_id=execution)


def test_api_accepts_an_optional_typed_execution_and_rejects_invalid_ids(monkeypatch):
    service = SimpleNamespace(details=MagicMock(return_value={'items': [], 'next_cursor': None}))
    monkeypatch.setattr(task_failure_details, '_service', lambda *_: service)
    app = FastAPI()
    app.include_router(task_failure_details.router, prefix='/tasks')
    task, execution = uuid4(), uuid4()
    response = Response()
    payload = task_failure_details.details(task, None, response, execution_id=execution, authorization=None)
    assert payload.items == []
    service.details.assert_called_with(task, None, execution_id=execution)
    assert response.headers['cache-control'] == 'no-store'
    parameter = next(item for item in app.openapi()['paths']['/tasks/{task_id}/details']['get']['parameters']
                     if item['name'] == 'execution_id')
    assert parameter['required'] is False
    assert parameter['schema']['anyOf'][0]['format'] == 'uuid'
    with pytest.raises(ValidationError):
        TypeAdapter(type(execution) | None).validate_python('invalid')
