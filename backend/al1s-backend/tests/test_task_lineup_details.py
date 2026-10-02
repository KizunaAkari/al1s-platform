from uuid import uuid4

from al1s.api.task_failure_details import TaskDetailsResponse
from al1s.execution.failure_details import failure_details


def test_successful_lineup_payload_does_not_generate_an_empty_script_diagnostic():
    assert failure_details({"success": True, "lineup": {"model_version": "lineup-v4"}}) == ()


def test_lineup_record_reference_is_exposed_and_old_detail_pages_still_work():
    record_id = uuid4()
    page = TaskDetailsResponse.model_validate(
        {"items": [], "next_cursor": None, "lineup_record_id": record_id}
    )
    assert page.model_dump(mode="json")["lineup_record_id"] == str(record_id)
    assert (
        TaskDetailsResponse.model_validate({"items": [], "next_cursor": None}).lineup_record_id
        is None
    )


def test_real_errors_and_screenshot_references_are_not_hidden():
    image = uuid4()
    details = failure_details(
        {"error": "inference failed", "failure_screenshot": {"artifact_id": str(image)}}
    )
    assert len(details) == 1
    assert details[0].message == "inference failed"
    assert details[0].screenshot_id == image


def test_attempt_page_uses_one_owned_join_without_per_row_reads():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from sqlalchemy.dialects import postgresql

    from al1s.adapters.postgres.failure_review import PostgresFailureReview

    queries = []
    record_id = uuid4()
    attempt = SimpleNamespace(
        id=uuid4(),
        execution_id=uuid4(),
        attempt_no=1,
        status="ended",
        result="success",
        error_code=None,
        failure_phase=None,
        ended_at=None,
    )

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execute(self, query):
            queries.append(query)
            return SimpleNamespace(all=lambda: [(attempt, record_id, "lineup")])

        def scalars(self, query):
            queries.append(query)
            return SimpleNamespace(all=lambda: [])

    page = PostgresFailureReview(lambda: Session()).list_attempts(uuid4(), None, datetime.now(UTC))
    assert page["lineup_record_id"] == record_id
    assert len(queries) == 2
    sql = str(
        queries[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "LEFT OUTER JOIN lineup_recognitions" in sql
    assert "lineup_recognitions.task_id = task_requests.id" in sql
    assert "task_requests.source_module = 'lineup'" in sql
    assert "LIMIT 21" in sql
