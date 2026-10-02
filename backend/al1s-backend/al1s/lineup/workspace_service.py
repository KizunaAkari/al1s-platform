"""Upload handoff, batch retry and human-annotation application operations."""

import hashlib
import json
from io import BytesIO
from typing import Any
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

from al1s.execution.errors import InvalidRequestError
from al1s.lineup.options import recognition_options
from al1s.lineup.ports import (
    LineupAnnotationRepositoryPort,
    LineupBatchCommandsPort,
    LineupWorkspaceQueriesPort,
)
from al1s.lineup.service import LineupService


class LineupWorkspaceService:
    def __init__(
        self,
        lineup: LineupService,
        commands: LineupBatchCommandsPort,
        queries: LineupWorkspaceQueriesPort,
        annotations: LineupAnnotationRepositoryPort,
    ) -> None:
        self.lineup, self.commands, self.queries, self.annotations = (
            lineup,
            commands,
            queries,
            annotations,
        )

    def submit(
        self, ids: list[UUID], terminal_id: UUID, options: dict[str, Any], key: str
    ) -> dict[str, Any]:
        if not 1 <= len(ids) <= 200 or len(set(ids)) != len(ids):
            raise InvalidRequestError("lineup_batch_size", "需要 1 至 200 张不重复的图片")
        task = self.commands.create(
            ids,
            terminal_id,
            recognition_options(options),
            "lineup-batch:" + key,
            validate_terminal=lambda: self._terminal(terminal_id),
        )
        return {"task_id": task}

    def retry(self, task_id: UUID, key: str) -> dict[str, Any]:
        task = self.queries.task(task_id)
        new_id = self.commands.create(
            [],
            task["terminal_id"],
            recognition_options(task["options"]),
            "lineup-retry:" + key,
            retry_source=task_id,
            validate_terminal=lambda: self._terminal(task["terminal_id"]),
        )
        return {"task_id": new_id}

    def _terminal(self, terminal_id: UUID) -> None:
        if not any(
            t["terminal_id"] == terminal_id and t["available"]
            for t in self.lineup.repository.terminals()
        ):
            raise InvalidRequestError("lineup_terminal_unavailable", "请选择在线且支持识别的终端")

    def annotation(self, record_id: UUID, task_id: UUID | None = None) -> dict[str, Any]:
        detail = self.lineup.detail(record_id)
        if task_id and detail["task_id"] != task_id:
            raise InvalidRequestError("lineup_task_mismatch", "图片不属于所选任务")
        return detail

    def save_annotation(
        self, record_id: UUID, version: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.annotations.save(record_id, version, payload)
        return self.lineup.detail(record_id)

    def export_annotation(self, record_id: UUID) -> bytes:
        detail = self.lineup.detail(record_id)
        if detail["annotation"] is None:
            raise InvalidRequestError("lineup_annotation_unsaved", "请先保存标注")
        record = self.lineup.repository.get(record_id)
        image = self.lineup.images.read(record["blob_id"])
        manifest = dict(
            schema_version=1,
            name=detail["name"],
            width=detail["width"],
            height=detail["height"],
            image_sha256=hashlib.sha256(image).hexdigest(),
            catalog_version=detail["catalog_version"],
            annotation=detail["annotation"],
        )
        output = BytesIO()
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("image.png", image)
            archive.writestr(
                "annotation.json", json.dumps(manifest, ensure_ascii=False, indent=2, default=str)
            )
        return output.getvalue()
