from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from al1s.adapters.postgres.delivery_models import TerminalReportRow
from al1s.adapters.postgres.execution_models import TerminalCapabilityProfileRow, TerminalRow
from al1s.adapters.postgres.lineup_batch_commands import LineupBatchCommands
from al1s.adapters.postgres.lineup_models import LineupRecognitionRow
from al1s.adapters.postgres.lineup_record_reads import refresh_attention
from al1s.adapters.postgres.lineup_repository import LineupRepository
from al1s.adapters.postgres.lineup_workspace_queries import LineupWorkspaceQueries
from al1s.adapters.postgres.scheduling_models import ExecutionRow, PlanOccurrenceRow
from al1s.execution.delivery_types import (
    PackageReceiptDisposition,
    TerminalReportDisposition,
    TerminalResultKind,
)
from al1s.execution.errors import ConflictError
from al1s.execution.scheduling_types import ExecutionResult
from al1s.infrastructure.scheduler_worker import build_runtime_worker
from tests.integration.execution_support import _delivery_services, _prepare_terminal, _services
from tests.integration.execution_support import clean_tables as clean_tables
from tests.integration.execution_support import engine as engine
from tests.integration.test_lineup_workspace import setup
from tests.test_lineup_partial import partial_result

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("unavailable", ["offline", "capability"])
def test_real_scheduler_leaves_unavailable_peer_waiting_across_claim_expiry(engine, unavailable):
    sessions, first, ids, _ = setup(engine, 2)
    resources, _ = _services(engine, [datetime.now(UTC)])
    second, _ = _prepare_terminal(resources)
    with sessions.begin() as session:
        for profile in session.scalars(select(TerminalCapabilityProfileRow)):
            profile.provider_keys = ["maa", "lineup-recognition-v1"]
    commands = LineupBatchCommands(sessions)
    a = commands.create([ids[0]], first, {}, "unavailable")
    b = commands.create([ids[1]], second, {}, "available")
    with sessions.begin() as session:
        if unavailable == "offline":
            session.get(TerminalRow, first).service_status = "offline"
        else:
            session.scalar(
                select(TerminalCapabilityProfileRow).where(
                    TerminalCapabilityProfileRow.terminal_id == first
                )
            ).memory_bytes = 1
    worker = build_runtime_worker(sessions)
    with ThreadPoolExecutor(max_workers=2) as pool:
        changed = list(
            pool.map(lambda _: build_runtime_worker(sessions).run_once().materialized, range(2))
        )
    assert sum(changed) == 1
    assert worker.run_once().materialized == 0
    with sessions.begin() as session:
        session.execute(
            update(PlanOccurrenceRow)
            .where(PlanOccurrenceRow.task_request_id == a)
            .values(materialization_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert worker.run_once().materialized == 0
    with sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExecutionRow)
                .where(ExecutionRow.task_request_id == b)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExecutionRow)
                .where(ExecutionRow.task_request_id == a)
            )
            == 0
        )


def test_legacy_review_refreshes_attention_and_preserves_raw_evidence_and_cas(engine):
    sessions, terminal, ids, scheduling = setup(engine, 1)
    task = LineupBatchCommands(sessions).create(ids, terminal, {}, "review")
    item = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
    scheduling.start_attempt(item.attempt.attempt_id, correlation_id=uuid4())
    scheduling.complete_attempt(
        item.attempt.attempt_id,
        result=ExecutionResult.SUCCESS,
        error_code=None,
        retryable=False,
        correlation_id=uuid4(),
    )
    raw = deepcopy(partial_result())
    for slot in raw["slots"]:
        if slot["present"]:
            slot["box"][0] = slot["index"] * 10
            slot.update(score=0.3, margin=0.01)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        report = TerminalReportRow(
            terminal_id=terminal,
            report_id=uuid4(),
            report_kind="attempt_result",
            disposition="accepted",
            attempt_id=item.attempt.attempt_id,
            payload_hash="a" * 64,
            occurred_at=now,
            received_at=now,
            result_diagnostic={"lineup": raw},
        )
        session.add(report)
        session.flush()
        record = session.get(LineupRecognitionRow, ids[0])
        refresh_attention(session, [record])
        version = record.row_version
        assert record.needs_attention
    repo, queries = LineupRepository(sessions), LineupWorkspaceQueries(sessions)
    confirmation = [None] * 6 + [slot["image_id"] for slot in raw["slots"][6:]]
    repo.save_review(ids[0], version, confirmation)
    with sessions() as session:
        record = session.get(LineupRecognitionRow, ids[0])
        assert not record.needs_attention
        version = record.row_version
        assert session.scalar(select(TerminalReportRow)).result_diagnostic == {"lineup": raw}
    assert queries.task(task)["summary"]["needs_attention"] == 0
    assert not queries.task(task, category="attention")["items"]
    with pytest.raises(ConflictError):
        repo.save_review(ids[0], version - 1, [None] * 12)
    repo.save_review(ids[0], version, [None] * 12)
    assert queries.task(task)["summary"]["needs_attention"] == 1
    assert len(queries.task(task, category="attention")["items"]) == 1


@pytest.mark.parametrize("missing_key", ["image_id", "ocr_id"])
def test_malformed_lineup_evidence_is_acknowledged_and_replays_without_transaction_failure(
    engine, missing_key
):
    sessions, terminal, ids, scheduling = setup(engine, 1)
    task = LineupBatchCommands(sessions).create(ids, terminal, {}, "malformed-result")
    item = scheduling.materialize_due(worker_id=uuid4(), correlation_id=uuid4())[0]
    now = datetime.now(UTC)
    delivery, _ = _delivery_services(engine, [now])
    command = delivery.list_commands(terminal)[0]
    receipt = delivery.receive_package(
        terminal_id=terminal,
        package_id=command.package_id,
        attempt_id=command.attempt_id,
        report_id=uuid4(),
        command_id=command.command_id,
        disposition=PackageReceiptDisposition.ACCEPTED,
        rejection_code=None,
        diagnostic=None,
        occurred_at=now,
        correlation_id=uuid4(),
    )
    started = delivery.start_attempt(
        terminal_id=terminal,
        attempt_id=command.attempt_id,
        package_id=command.package_id,
        offline_permit_id=receipt.offline_start_permit.permit_id,
        offline_permit_token=receipt.offline_start_permit.token,
        report_id=uuid4(),
        occurred_at=now,
        correlation_id=uuid4(),
    )
    raw = deepcopy(partial_result())
    for slot in raw["slots"]:
        if slot["present"]:
            slot["box"][0] = slot["index"] * 10
    del raw["slots"][0][missing_key]
    result_args = dict(
        diagnostic={"lineup": raw},
        terminal_id=terminal,
        attempt_id=item.attempt.attempt_id,
        package_id=command.package_id,
        report_id=uuid4(),
        lease_id=started.lease.lease_id,
        lease_version=started.lease.row_version,
        result_kind=TerminalResultKind.SUCCESS,
        error_code=None,
        retryable=False,
        occurred_at=now,
        correlation_id=uuid4(),
    )
    first = delivery.receive_result(**result_args)
    replay = delivery.receive_result(**result_args)
    assert first.report.disposition is TerminalReportDisposition.ACCEPTED
    assert replay.report == first.report
    detail = LineupWorkspaceQueries(sessions).task(task)
    assert detail["items"][0]["state"] == "failure"
    assert detail["items"][0]["error_code"] == "lineup_result_invalid"
    assert detail["summary"]["needs_attention"] == 1
    with sessions() as session:
        reports = list(
            session.scalars(
                select(TerminalReportRow).where(TerminalReportRow.report_kind == "attempt_result")
            )
        )
        assert len(reports) == 1
        assert reports[0].result_diagnostic == {"lineup": raw}
