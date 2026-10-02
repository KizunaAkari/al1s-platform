from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from al1s.execution.errors import ConflictError
from al1s.execution.scheduling_base import SchedulingContext
from al1s.execution.scheduling_helpers import (
    _select_resource,
    _transition,
)
from al1s.execution.scheduling_ports import SchedulingUnitOfWork
from al1s.execution.scheduling_types import (
    AttemptStatus,
    EligibleExecutionResource,
    ExecutionAttemptRecord,
    ExecutionRecord,
    ExecutionSnapshotRecord,
    ExecutionStatus,
    MaterializationCandidate,
    MaterializedExecution,
    OccurrenceStatus,
    PlanOccurrenceRecord,
    PreparedMaterialization,
    ResolvedExecutionDefinition,
)


class ScheduleMaterialization(SchedulingContext):
    def materialize_due(
        self,
        *,
        worker_id: UUID,
        correlation_id: UUID,
        limit: int = 50,
        claim_ttl: timedelta = timedelta(minutes=2),
    ) -> list[MaterializedExecution]:
        now = self._now()
        with self._uow_factory() as uow:
            claimed = uow.occurrences.claim_due(
                worker_id=worker_id,
                now=now,
                lease_duration=claim_ttl,
                limit=limit,
            )
            uow.commit()
        if not claimed:
            return []
        definitions = self._definitions.resolve_candidates(claimed)
        with self._uow_factory() as uow:
            resources = uow.executions.list_eligible_resources(
                requested_terminal_ids={
                    item.task.requested_terminal_id
                    for item in claimed
                    if item.task.requested_terminal_id is not None
                },
                requested_target_device_ids={
                    item.task.requested_target_device_id
                    for item in claimed
                    if item.task.requested_target_device_id is not None
                },
                include_unrestricted=any(
                    item.task.requested_terminal_id is None
                    and item.task.requested_target_device_id is None
                    for item in claimed
                ),
                limit=100,
            )
            prepared = self._prepare_available(claimed, definitions, resources)
            all_blob_ids = {blob.blob_id for item in prepared for blob in item.definition.blobs}
            missing_blobs = all_blob_ids - uow.snapshots.ready_blob_ids(all_blob_ids)
            if missing_blobs:
                raise ConflictError(
                    "execution_snapshot_blob_unavailable",
                    "One or more execution snapshot resources are unavailable",
                )
            results: list[MaterializedExecution] = []
            for item in prepared:
                occurrence = uow.occurrences.mark_materialized(item.occurrence_id, worker_id, now)
                if occurrence is None:
                    continue
                result = self._add_materialized_execution(
                    uow, item, occurrence, correlation_id, now
                )
                results.append(result)
            uow.commit()
        return results

    def _prepare_available(
        self,
        claimed: list[MaterializationCandidate],
        definitions: dict[UUID, ResolvedExecutionDefinition],
        resources: list[EligibleExecutionResource],
    ) -> list[PreparedMaterialization]:
        prepared = []
        for candidate in claimed:
            try:
                prepared.append(self._prepare_materialization(candidate, definitions, resources))
            except ConflictError as error:
                if error.code != "eligible_terminal_unavailable":
                    raise
                # Keep this occurrence's existing claim lease as a bounded backoff;
                # peers can materialize now and later batches can pass it until expiry.
        return prepared

    def _prepare_materialization(
        self,
        candidate: MaterializationCandidate,
        definitions: dict[UUID, ResolvedExecutionDefinition],
        resources: list[EligibleExecutionResource],
    ) -> PreparedMaterialization:
        definition = definitions[candidate.occurrence.occurrence_id]
        resource, target_id = _select_resource(
            candidate.task,
            definition.capability_requirements,
            resources,
        )
        self._definitions.authorize_target(
            candidate.task.source_module,
            candidate.task.logical_content_id,
            target_id,
        )
        return PreparedMaterialization(
            occurrence_id=candidate.occurrence.occurrence_id,
            task=candidate.task,
            definition=definition,
            terminal_id=resource.terminal_id,
            target_device_id=target_id,
        )

    def _add_materialized_execution(
        self,
        uow: SchedulingUnitOfWork,
        prepared: PreparedMaterialization,
        occurrence: PlanOccurrenceRecord,
        correlation_id: UUID,
        now: datetime,
    ) -> MaterializedExecution:
        execution = ExecutionRecord(
            execution_id=uuid4(),
            task_request_id=prepared.task.task_id,
            occurrence_id=occurrence.occurrence_id,
            terminal_id=prepared.terminal_id,
            target_device_id=prepared.target_device_id,
            status=ExecutionStatus.WAITING,
            result=None,
            timeout_at=None,
            cancel_requested_at=None,
            cancel_reason=None,
            cancel_deadline_at=None,
            cancel_delivery_delayed=False,
            cancel_delivery_note=None,
            created_at=now,
            queued_at=None,
            started_at=None,
            ended_at=None,
            row_version=1,
        )
        attempt = ExecutionAttemptRecord(
            attempt_id=uuid4(),
            execution_id=execution.execution_id,
            attempt_no=1,
            status=AttemptStatus.QUEUED,
            result=None,
            available_at=now,
            enqueued_at=now,
            started_at=None,
            ended_at=None,
            error_code=None,
            retryable=None,
            failure_phase=None,
            lease_id=None,
            row_version=1,
        )
        snapshot = ExecutionSnapshotRecord(
            snapshot_id=uuid4(),
            execution_id=execution.execution_id,
            source_module=prepared.task.source_module,
            logical_content_id=prepared.task.logical_content_id,
            revision_id=prepared.definition.revision_id,
            schema_version=prepared.definition.schema_version,
            manifest_hash=prepared.definition.manifest_hash,
            manifest=dict(prepared.definition.manifest),
            parameters=dict(prepared.task.parameters),
            capability_requirements=prepared.definition.capability_requirements,
            terminal_id=prepared.terminal_id,
            target_device_id=prepared.target_device_id,
            timeout_seconds=prepared.task.timeout_seconds,
            max_retries=prepared.task.max_retries,
            record_video=prepared.task.record_video,
            created_at=now,
        )
        uow.executions.add(execution)
        # Attempts and snapshots both reference the execution.  Keep this
        # transaction atomic while making the FK boundary explicit instead of
        # relying on SQLAlchemy to infer an order without relationships.
        uow.flush()
        uow.attempts.add(attempt)
        uow.snapshots.add(snapshot, prepared.definition.blobs)
        self._add_delivery_for_attempt(
            uow,
            attempt=attempt,
            snapshot=snapshot,
            correlation_id=correlation_id,
            now=now,
        )
        uow.transitions.add_many(
            [
                _transition(
                    "plan_occurrence",
                    occurrence.occurrence_id,
                    OccurrenceStatus.PLANNED.value,
                    occurrence.status.value,
                    "execution_materialized",
                    correlation_id,
                    now,
                ),
                _transition(
                    "execution",
                    execution.execution_id,
                    None,
                    execution.status.value,
                    "execution_materialized",
                    correlation_id,
                    now,
                ),
                _transition(
                    "execution_attempt",
                    attempt.attempt_id,
                    None,
                    attempt.status.value,
                    "initial_attempt_queued",
                    correlation_id,
                    now,
                ),
            ]
        )
        self._record(
            uow,
            event_type="execution.materialized.v1",
            aggregate_type="execution",
            aggregate_id=execution.execution_id,
            correlation_id=correlation_id,
            occurred_at=now,
            payload={
                "execution_id": str(execution.execution_id),
                "occurrence_id": str(occurrence.occurrence_id),
                "snapshot_id": str(snapshot.snapshot_id),
                "terminal_id": str(execution.terminal_id),
                "target_device_id": (
                    str(execution.target_device_id) if execution.target_device_id else None
                ),
            },
            action="execution.materialize",
            reason_code="execution_materialized",
        )
        return MaterializedExecution(occurrence, execution, attempt, snapshot)
