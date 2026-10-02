from typing import Any
from uuid import UUID, uuid4

from al1s.execution.errors import ConflictError, InvalidRequestError
from al1s.execution.scheduling_service import ExecutionSchedulingService
from al1s.execution.scheduling_types import CreateTaskCommand, TaskType
from al1s.lineup.catalog import catalog, validate_review
from al1s.lineup.images import LineupImages, normalize_image
from al1s.lineup.options import recognition_options
from al1s.lineup.ports import LineupRepositoryPort
from al1s.lineup.task_presentation import lineup_task_name


class LineupService:
    def __init__(self, repository: LineupRepositoryPort, images: LineupImages) -> None:
        self.repository, self.images = repository, images

    def upload(self, name: str, body: bytes) -> dict[str, Any]:
        image, width, height = normalize_image(body)
        blob_id = self.images.stage(image)
        record_id = self.repository.create(
            id=uuid4(),
            blob_id=blob_id,
            name=name.strip()[:160] or "战报图片",
            width=width,
            height=height,
            catalog_version=catalog()["version"],
        )
        return self.detail(record_id)

    def detail(self, record_id: UUID) -> dict[str, Any]:
        return self.repository.enrich([self.repository.get(record_id)])[0]

    def page(self, cursor: UUID | None) -> dict[str, Any]:
        rows = self.repository.page(cursor)
        return dict(
            items=self.repository.enrich(rows[:20]),
            next_cursor=str(rows[19]["id"]) if len(rows) > 20 else None,
        )

    def batch_details(self, record_ids: list[UUID]) -> dict[str, Any]:
        if not 1 <= len(record_ids) <= 100:
            raise InvalidRequestError("lineup_batch_size", "每次查询需提供 1 至 100 个记录 ID")
        keys = list(dict.fromkeys(record_ids))
        rows = self.repository.get_many(keys)
        found = {row["id"] for row in rows}
        return dict(
            items=self.repository.enrich(rows),
            missing_ids=[key for key in keys if key not in found],
        )

    def run(
        self,
        record_id: UUID,
        terminal_id: UUID,
        scheduling: ExecutionSchedulingService,
        correlation: UUID,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        options = recognition_options(options or {})
        record = self.repository.get(record_id)
        if record["task_id"]:
            return self.detail(record_id)
        terminals = self.repository.terminals()
        if not any(t["terminal_id"] == terminal_id and t["available"] for t in terminals):
            raise InvalidRequestError(
                "lineup_terminal_unavailable", "请选择在线且支持阵容识别的终端"
            )
        if record["catalog_version"] != catalog()["version"]:
            raise ConflictError("lineup_catalog_changed", "资料版本已变化。请重新上传图片")
        task = scheduling.create_task(
            CreateTaskCommand(
                idempotency_key=f"lineup:{record_id}",
                name=lineup_task_name(1),
                task_type=TaskType.SINGLE,
                source_module="lineup",
                logical_content_id=str(record_id),
                parameters=options,
                requested_terminal_id=terminal_id,
                requested_target_device_id=None,
                timeout_seconds=180,
                max_retries=0,
                record_video=False,
            ),
            correlation_id=correlation,
        )
        self.repository.attach_task(record_id, task.task.task_id, terminal_id)
        return self.detail(record_id)

    def review(self, record_id: UUID, version: int, ids: list[int | None]) -> dict[str, Any]:
        validate_review(ids)
        detail = self.detail(record_id)
        if detail["result"] is None or detail["state"] != "success":
            raise ConflictError("lineup_not_ready", "识别完成后才可保存核对结果")
        teams = detail["result"].get("teams", ["attack", "defense"])
        for index, sid in enumerate(ids):
            if sid is not None and (
                ("attack" if index < 6 else "defense") not in teams
                or detail["result"]["slots"][index].get("present") is False
            ):
                raise InvalidRequestError("lineup_side_absent", "不能为未提供的阵容添加学生")
        self.repository.save_review(record_id, version, ids)
        return self.detail(record_id)
