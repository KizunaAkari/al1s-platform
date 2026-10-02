"""Project accepted execution failures into idempotent notification intents."""
# User-facing Chinese notification punctuation is intentional.
# ruff: noqa: RUF001

from al1s.kernel.ports import EventPublisher
from al1s.kernel.types import ClaimedOutboxEvent
from al1s.notifications.service import NotificationService
from al1s.notifications.types import NotificationKind


def _skip_condition(payload: dict[str, object]) -> str:
    if payload.get("mode") in {"recognition_failure", "execution_failure"}:
        phase = "识别失败" if payload["mode"] == "recognition_failure" else "执行失败"
        return phase + "，已按配置跳过"
    if payload.get("mode") == "image":
        return f"图片命中，匹配阈值{payload.get('threshold', '未记录')}"
    if payload.get("mode") == "numeric":
        operator = {"gt": "大于", "lt": "小于"}.get(str(payload.get("operator")), "比较")
        return f"识别数值{operator}{payload.get('value', '未记录')}"
    return "条件已命中（旧报告未记录条件参数）"


class NotificationEventPublisher:
    def __init__(self, downstream: EventPublisher, notifications: NotificationService) -> None:
        self._downstream = downstream
        self._notifications = notifications

    def publish(self, event: ClaimedOutboxEvent) -> None:
        if (event.schema_version == 1 and event.aggregate_type == "maa_import_batch"
                and event.event_type == "maa.archive-import.failed.v1"
                and event.payload.get("error_code") != "archive_import_cancelled"):
            self._notifications.materialize_intent(
                source_event_id=event.event_id,
                notification_kind=NotificationKind.OPERATION_FAILURE,
                source_type="maa_import_batch", source_id=event.aggregate_id,
                correlation_id=event.correlation_id, occurred_at=event.occurred_at,
                payload={"summary": f"脚本归档导入失败或超时；批次：{event.aggregate_id}；"
                         "具体原因请查看批次详情"},
            )
        if (event.schema_version == 1 and event.aggregate_type == "terminal"
                and event.event_type == "terminal.storage_low.v1"):
            self._notifications.materialize_intent(
                source_event_id=event.event_id, notification_kind=NotificationKind.STORAGE_LOW,
                source_type="terminal", source_id=event.aggregate_id,
                correlation_id=event.correlation_id, occurred_at=event.occurred_at,
                payload={"summary": f"终端临时文件磁盘可用空间低于20%；终端：{event.aggregate_id}；"
                         f"可用：{event.payload.get('available_bytes')}字节；总容量：{event.payload.get('total_bytes')}字节"},
            )
        if (
            event.schema_version == 1
            and event.aggregate_type == "terminal"
            and event.event_type in {"terminal.offline.v1", "terminal.maintenance_failed.v1"}
        ):
            self._notifications.materialize_intent(
                source_event_id=event.event_id,
                notification_kind=NotificationKind.TERMINAL_ALERT,
                source_type="terminal",
                source_id=event.aggregate_id,
                correlation_id=event.correlation_id,
                occurred_at=event.occurred_at,
                payload={
                    "summary": (
                        f"终端心跳超时，已标记离线；终端：{event.aggregate_id}"
                        if event.event_type == "terminal.offline.v1"
                        else f"终端维护失败或结果未确认；终端：{event.aggregate_id}；"
                        f"原因：{event.payload.get('reason', 'unknown')}"
                    )
                },
            )
        if (
            event.schema_version == 1
            and event.aggregate_type == "execution"
            and event.event_type == "execution.conditional_skip.v1"
        ):
            module = event.payload.get("module_number", "?")
            step = event.payload.get("module_step_number", event.payload.get("step_number", "?"))
            self._notifications.materialize_intent(
                source_event_id=event.event_id,
                notification_kind=NotificationKind.CONDITIONAL_SKIP,
                source_type="execution",
                source_id=event.aggregate_id,
                correlation_id=event.correlation_id,
                occurred_at=event.occurred_at,
                payload={
                    "summary": (
                        f"条件跳过；组合第{module}步，脚本第{step}步；"
                        f"执行记录：{event.aggregate_id}；"
                        f"条件：{_skip_condition(event.payload)}；"
                        f"跳转：{event.payload.get('target_step_number', '下一步')}"
                    ),
                    **{
                        key: event.payload[key]
                        for key in ("task_id", "terminal_id", "attempt_id", "capture_id")
                        if key in event.payload
                    },
                    "execution_id": str(event.aggregate_id),
                },
            )
        if (
            event.schema_version == 1
            and event.aggregate_type == "execution"
            and event.event_type in {"execution.ended.v1", "execution.retry_queued.v1"}
            and event.payload.get("result_kind") == "failure"
        ):
            self._notifications.materialize_intent(
                source_event_id=event.event_id,
                notification_kind=NotificationKind.SCRIPT_FAILURE,
                source_type="execution",
                source_id=event.aggregate_id,
                correlation_id=event.correlation_id,
                occurred_at=event.occurred_at,
                payload={
                    "summary": f"脚本执行失败；执行记录：{event.aggregate_id}",
                    "execution_id": str(event.aggregate_id),
                    **{
                        key: event.payload[key]
                        for key in ("task_id", "attempt_id")
                        if key in event.payload
                    },
                },
            )
        # If publishing fails, Outbox replays; materialization is idempotent.
        self._downstream.publish(event)
