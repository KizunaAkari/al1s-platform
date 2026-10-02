"""Compatibility imports for focused scheduling persistence modules."""

from al1s.adapters.postgres.scheduling_attempt_repository import (
    PostgresAttemptRepository as PostgresAttemptRepository,
)
from al1s.adapters.postgres.scheduling_content_schedule_impact_repository import (
    PostgresContentScheduleImpactRepository as PostgresContentScheduleImpactRepository,
)
from al1s.adapters.postgres.scheduling_execution_repository import (
    PostgresExecutionRepository as PostgresExecutionRepository,
)
from al1s.adapters.postgres.scheduling_occurrence_repository import (
    PostgresOccurrenceRepository as PostgresOccurrenceRepository,
)
from al1s.adapters.postgres.scheduling_schedule_repository import (
    PostgresScheduleRepository as PostgresScheduleRepository,
)
from al1s.adapters.postgres.scheduling_schedule_revision_repository import (
    PostgresScheduleRevisionRepository as PostgresScheduleRevisionRepository,
)
from al1s.adapters.postgres.scheduling_snapshot_repository import (
    PostgresSnapshotRepository as PostgresSnapshotRepository,
)
from al1s.adapters.postgres.scheduling_task_repository import (
    PostgresTaskRepository as PostgresTaskRepository,
)
from al1s.adapters.postgres.scheduling_task_retry_origin_repository import (
    PostgresTaskRetryOriginRepository as PostgresTaskRetryOriginRepository,
)
from al1s.adapters.postgres.scheduling_transition_repository import (
    PostgresTransitionRepository as PostgresTransitionRepository,
)
