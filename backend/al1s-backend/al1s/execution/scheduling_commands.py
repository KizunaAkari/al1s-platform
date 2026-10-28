from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from al1s.components.maintenance_guard import admit_new_work
from al1s.execution.errors import ConflictError
from al1s.execution.scheduling_base import SchedulingContext
from al1s.execution.scheduling_helpers import (
    _build_plan,
    _request_hash,
    _transition,
)
from al1s.execution.scheduling_types import (
    CreatedTask,
    CreateTaskCommand,
    TaskLifecycleStatus,
    TaskRequestRecord,
)


class SchedulingCommands(SchedulingContext):
    def create_task(self, command: CreateTaskCommand, *, correlation_id: UUID) -> CreatedTask:
        self._validate_command(command)
        request_hash = _request_hash(command)
        idempotency_key = command.idempotency_key.strip()
        # A replay is answered from the persisted request before resolving the
        # current module definition.  Published content may have moved on (or
        # its provider may be temporarily unavailable), but that must not make
        # an already accepted idempotency key fail on retry.
        with self._uow_factory() as uow:
            existing = uow.tasks.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                return self._idempotent_result(uow, existing, request_hash)
        self._definitions.resolve(
            command.source_module,
            command.logical_content_id,
            command.parameters,
        )
        self._definitions.authorize_target(
            command.source_module,
            command.logical_content_id,
            command.requested_target_device_id,
        )
        now = self._now()
        task_id = uuid4()
        schedule, schedule_revision, revision_times, occurrences = _build_plan(
            command, task_id, now
        )
        task = TaskRequestRecord(
            task_id=task_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            name=command.name.strip(),
            task_type=command.task_type,
            lifecycle_status=TaskLifecycleStatus.ACTIVE,
            source_module=command.source_module.strip(),
            logical_content_id=command.logical_content_id.strip(),
            parameters=dict(command.parameters),
            requested_terminal_id=command.requested_terminal_id,
            requested_target_device_id=command.requested_target_device_id,
            timeout_seconds=command.timeout_seconds,
            max_retries=command.max_retries,
            record_video=command.record_video,
            created_at=now,
            completed_at=None,
            deleted_at=None,
            row_version=1,
        )
        try:
            with self._uow_factory() as uow:
                existing = uow.tasks.find_by_idempotency_key(task.idempotency_key)
                if existing is not None:
                    return self._idempotent_result(uow, existing, request_hash)
                admit_new_work(
                    uow, command.requested_terminal_id, command.requested_target_device_id
                )
                uow.tasks.add(task)
                uow.flush()
                if schedule is not None:
                    uow.schedules.add(schedule)
                    # The persistence rows intentionally do not carry ORM
                    # relationships.  Flush the FK parent before adding its
                    # children so insertion order is explicit and stable.
                    uow.flush()
                    if schedule_revision is not None:
                        uow.schedule_revisions.add(schedule_revision, revision_times)
                uow.occurrences.add_many(occurrences)
                transitions = [
                    _transition(
                        "task_request",
                        task.task_id,
                        None,
                        task.lifecycle_status.value,
                        "task_created",
                        correlation_id,
                        now,
                    )
                ]
                if schedule is not None:
                    transitions.append(
                        _transition(
                            "task_schedule",
                            schedule.schedule_id,
                            None,
                            schedule.status.value,
                            "schedule_created",
                            correlation_id,
                            now,
                        )
                    )
                uow.transitions.add_many(transitions)
                self._record(
                    uow,
                    event_type="execution.task_created.v1",
                    aggregate_type="task_request",
                    aggregate_id=task.task_id,
                    correlation_id=correlation_id,
                    occurred_at=now,
                    payload={
                        "task_id": str(task.task_id),
                        "task_type": task.task_type.value,
                        "occurrence_count": len(occurrences),
                    },
                    action="execution.task.create",
                    reason_code="task_created",
                )
                uow.commit()
        except IntegrityError as exc:
            with self._uow_factory() as uow:
                existing = uow.tasks.find_by_idempotency_key(task.idempotency_key)
                if existing is not None:
                    return self._idempotent_result(uow, existing, request_hash)
            raise ConflictError("task_creation_conflict", "Task creation conflicted") from exc
        return CreatedTask(task, schedule, tuple(occurrences))

