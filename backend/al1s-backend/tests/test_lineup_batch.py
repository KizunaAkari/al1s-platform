from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from al1s.api.lineup import BatchQueryRequest
from al1s.execution.errors import InvalidRequestError
from al1s.lineup.service import LineupService


def test_batch_read_is_bounded_deduplicated_and_preserves_order_without_per_row_calls():
    first, second, missing = uuid4(), uuid4(), uuid4()
    calls = []

    def get_many(keys):
        calls.append(("read", keys))
        return [dict(id=key) for key in keys if key != missing]

    def enrich(rows):
        calls.append(("enrich", rows))
        return rows

    service = LineupService(SimpleNamespace(get_many=get_many, enrich=enrich), None)
    result = service.batch_details([second, first, second, missing])
    assert result == dict(items=[dict(id=second), dict(id=first)], missing_ids=[missing])
    assert [entry[0] for entry in calls] == ["read", "enrich"]
    assert calls[0][1] == [second, first, missing]
    for invalid in ([], [uuid4() for _ in range(101)]):
        with pytest.raises(InvalidRequestError):
            service.batch_details(invalid)
    assert len(calls) == 2


def test_batch_query_rejects_oversized_or_invalid_ids_and_accepts_one_hundred():
    keys = [uuid4() for _ in range(100)]
    assert BatchQueryRequest(record_ids=keys).record_ids == keys
    for keys in ([], [uuid4() for _ in range(101)], ["not-an-id"]):
        with pytest.raises(ValidationError):
            BatchQueryRequest(record_ids=keys)
