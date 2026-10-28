from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from al1s.adapters.postgres.setup_check_repository import SetupCheckRepository
from al1s.execution.errors import ConflictError
from al1s.execution.terminal_setup_checks import SetupFacts
from tests.test_device_path_database import database, terminal  # noqa: F401


def test_manual_and_daily_coalesce_and_only_completion_refreshes_date(database):
    sessions, _ = database
    now = datetime.now(UTC)
    endpoint = terminal("android", now)
    with sessions.begin() as tx:
        tx.add(endpoint)
    repository = SetupCheckRepository(sessions)
    first = repository.request(endpoint.id, False, now)
    assert first.completed_at is None
    assert repository.request(endpoint.id, True, now).request_id == first.request_id
    instance = uuid4()
    claimed = repository.claim(endpoint.id, first.request_id, instance, now)
    assert claimed.completed_at is None
    assert repository.request(endpoint.id, False, now + timedelta(days=2)).request_id == first.request_id
    facts = SetupFacts(False, True, True, True, False)
    completed = repository.complete(endpoint.id, first.request_id, instance, facts, now)
    assert completed.completed_at == now
    assert repository.complete(endpoint.id, first.request_id, instance, facts, now + timedelta(seconds=1)) == completed
    assert repository.request(endpoint.id, False, now + timedelta(hours=23)) is None
    with pytest.raises(ConflictError):
        repository.complete(endpoint.id, first.request_id, instance, replace(facts, secure_lock=True), now)
    next_check = repository.request(endpoint.id, False, now + timedelta(hours=24))
    assert next_check.request_id != first.request_id


def test_other_instance_cannot_report_or_duplicate_inflight_check(database):
    sessions, _ = database
    now = datetime.now(UTC)
    endpoint = terminal("android", now)
    with sessions.begin() as tx:
        tx.add(endpoint)
    repository = SetupCheckRepository(sessions)
    request = repository.request(endpoint.id, True, now)
    repository.claim(endpoint.id, request.request_id, uuid4(), now)
    with pytest.raises(ConflictError):
        repository.claim(endpoint.id, request.request_id, uuid4(), now)
    with pytest.raises(ConflictError):
        repository.complete(endpoint.id, request.request_id, uuid4(), SetupFacts(False, True, True, True, False), now)
