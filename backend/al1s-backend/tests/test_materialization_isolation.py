from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.execution.errors import ConflictError
from al1s.execution.scheduling_materialization import ScheduleMaterialization
from al1s.execution.scheduling_types import CapabilityRequirements
from al1s.infrastructure.execution_worker import ExecutionRuntimeWorker


def test_offline_candidate_does_not_rollback_a_healthy_candidate():
    owner, unavailable, available = uuid4(), uuid4(), uuid4()
    candidates = [
        SimpleNamespace(
            occurrence=SimpleNamespace(occurrence_id=uuid4()),
            task=SimpleNamespace(
                requested_terminal_id=terminal,
                requested_target_device_id=None,
                source_module="test",
                logical_content_id=str(terminal),
            ),
        )
        for terminal in (unavailable, available)
    ]
    definition = SimpleNamespace(capability_requirements=CapabilityRequirements(), blobs=())
    registry = MagicMock()
    registry.resolve_candidates.return_value = {
        c.occurrence.occurrence_id: definition for c in candidates
    }
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.occurrences.claim_due.return_value = candidates
    uow.executions.list_eligible_resources.return_value = [
        SimpleNamespace(
            terminal_id=available,
            managed_target_device_ids=[],
            capability=SimpleNamespace(
                protocol_version=1,
                memory_bytes=10**10,
                storage_available_bytes=10**10,
                provider_keys=[],
                architecture="x64",
                accelerator_type=None,
            ),
        )
    ]
    uow.snapshots.ready_blob_ids.return_value = set()
    runtime = ScheduleMaterialization(lambda: uow, registry, now=lambda: datetime.now(UTC))
    runtime._add_materialized_execution = MagicMock(return_value="healthy-result")
    result = runtime.materialize_due(worker_id=owner, correlation_id=uuid4())
    assert result == ["healthy-result"]
    uow.occurrences.mark_materialized.assert_called_once()
    assert (
        uow.occurrences.mark_materialized.call_args.args[0]
        == candidates[1].occurrence.occurrence_id
    )
    assert uow.executions.list_eligible_resources.call_count == 1
    assert uow.snapshots.ready_blob_ids.call_count == 1
    assert uow.commit.call_count == 2


def test_non_resource_definition_errors_remain_visible_and_timeout_maintenance_runs():
    scheduling, timeouts = MagicMock(), MagicMock()
    scheduling.materialize_due.side_effect = ConflictError(
        "execution_definition_hash_mismatch", "bad"
    )
    worker = ExecutionRuntimeWorker(scheduling, timeouts)
    with pytest.raises(ConflictError) as error:
        worker.run_once()
    assert error.value.code == "execution_definition_hash_mismatch"
    timeouts.request_timed_out_cancellation.assert_called_once()
    timeouts.force_expired_cleanup.assert_called_once()
