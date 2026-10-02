from typing import Any
from uuid import UUID

from al1s.execution.definitions import canonical_manifest_hash
from al1s.execution.errors import InvalidRequestError
from al1s.execution.scheduling_types import (
    CapabilityRequirements,
    ResolvedExecutionDefinition,
    SnapshotBlobReference,
)
from al1s.lineup.catalog import CAPABILITY, MODEL_VERSION
from al1s.lineup.options import recognition_options
from al1s.lineup.ports import LineupRepositoryPort


class LineupDefinitionProvider:
    def __init__(self, repository: LineupRepositoryPort) -> None:
        self.repository = repository

    def resolve(
        self, logical_content_id: str, parameters: dict[str, Any]
    ) -> ResolvedExecutionDefinition:
        options = recognition_options(parameters)
        try:
            record_id = UUID(logical_content_id)
        except ValueError as exc:
            raise InvalidRequestError("lineup_id_invalid", "识别记录 ID 无效") from exc
        record = self.repository.get(record_id)
        return self.definition_for_record(record, options)

    @staticmethod
    def definition_for_record(
        record: dict[str, Any], options: dict[str, Any], model_version: str = MODEL_VERSION
    ) -> ResolvedExecutionDefinition:
        manifest = dict(
            schema_version=1,
            executor=CAPABILITY,
            image_resource="input.png",
            catalog_version=record["catalog_version"],
            model_version=model_version,
            **options,
        )
        return ResolvedExecutionDefinition(
            revision_id=f"{model_version}:{record['catalog_version']}",
            schema_version=1,
            manifest_hash=canonical_manifest_hash(manifest),
            manifest=manifest,
            capability_requirements=CapabilityRequirements(
                provider_keys=(CAPABILITY,),
                requires_target_device=False,
                min_memory_bytes=1024 * 1024 * 1024,
                min_storage_bytes=64 * 1024 * 1024,
            ),
            blobs=(SnapshotBlobReference(record["blob_id"], "input.png", "lineup_input"),),
        )

    def authorize_target(self, logical_content_id: str, target_device_id: UUID | None) -> None:
        if target_device_id is not None:
            raise InvalidRequestError("lineup_phone_forbidden", "阵容识别仅指定 Linux 终端")
