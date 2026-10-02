"""Resolve a bounded set of batch occurrences into existing single-image packages."""

from typing import Any
from uuid import UUID

from al1s.execution.errors import InvalidRequestError
from al1s.execution.scheduling_types import ResolvedExecutionDefinition
from al1s.lineup.definition import LineupDefinitionProvider


class LineupBatchDefinitionProvider(LineupDefinitionProvider):
    def resolve(
        self, logical_content_id: str, parameters: dict[str, Any]
    ) -> ResolvedExecutionDefinition:
        raise InvalidRequestError("lineup_batch_endpoint_required", "请通过阵容上传入口创建批任务")

    def resolve_occurrences(self, ids: list[UUID]) -> dict[UUID, ResolvedExecutionDefinition]:
        records = self.repository.occurrence_records(ids)
        return {
            r["occurrence_id"]: self.definition_for_record(r, r["options"], r["model_version"])
            for r in records
        }
