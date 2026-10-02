from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID

from al1s.execution.notification_evidence import EvidencePending
from al1s.notifications.errors import NotificationAdapterError
from al1s.notifications.lease import DeliveryLease
from al1s.notifications.ports import (
    NotificationChannelAdapter,
    NotificationUnitOfWork,
)
from al1s.notifications.task_links import task_link, validate_public_url
from al1s.notifications.types import (
    AttemptOutcome,
    ChannelKind,
    ClaimedNotificationDelivery,
    DeliveryOutcome,
    NotificationDispatchResult,
    NotificationMessage,
)
from al1s.secrets.ports import SecretCipher


class NotificationDispatcher:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], NotificationUnitOfWork],
        adapters: Mapping[ChannelKind, NotificationChannelAdapter],
        cipher: SecretCipher,
        worker_id: str,
        lease_duration: timedelta = timedelta(seconds=30),
        batch_size: int = 50,
        max_attempts: int = 5,
        base_retry_delay: timedelta = timedelta(seconds=5),
        max_retry_delay: timedelta = timedelta(minutes=15),
        now: Callable[[], datetime] | None = None,
        review_body: Callable[[UUID], str] | None = None,
        platform_public_url: str = "",
        execution_evidence: Callable[[dict[str, object]], bytes | None] | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id is required")
        if not 1 <= batch_size <= 50:
            raise ValueError("batch_size must be between 1 and 50")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if lease_duration.total_seconds() <= 0:
            raise ValueError("lease_duration must be positive")
        self._uow_factory = uow_factory
        self._adapters = dict(adapters)
        self._cipher = cipher
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._base_retry_delay = base_retry_delay
        self._max_retry_delay = max_retry_delay
        self._now = now or (lambda: datetime.now(UTC))
        self._review_body = review_body
        self._public_url = validate_public_url(platform_public_url)
        self._execution_evidence = execution_evidence

    def dispatch_once(self) -> NotificationDispatchResult:
        claim_time = self._now()
        with self._uow_factory() as uow:
            claimed = uow.deliveries.claim_batch(
                worker_id=self._worker_id,
                now=claim_time,
                lease_duration=self._lease_duration,
                limit=self._batch_size,
                max_attempts=self._max_attempts,
            )
            uow.commit()
        if not claimed:
            return NotificationDispatchResult(0, 0, 0, 0, 0)
        outcomes = []
        with DeliveryLease(
            self._uow_factory,
            claimed,
            worker_id=self._worker_id,
            duration=self._lease_duration,
            now=self._now,
        ) as lease:
            for item in claimed:
                if not lease.valid:
                    break
                outcomes.append(self._send(item))
        with self._uow_factory() as uow:
            waiting = [o for o in outcomes if o.evidence_pending]
            deferred = (
                uow.deliveries.defer_evidence(waiting, worker_id=self._worker_id, now=self._now())
                if waiting
                else 0
            )
            sent, failed, dead_lettered = uow.deliveries.settle_batch(
                [o for o in outcomes if not o.evidence_pending],
                worker_id=self._worker_id,
                now=self._now(),
                max_attempts=self._max_attempts,
                base_retry_delay=self._base_retry_delay,
                max_retry_delay=self._max_retry_delay,
            )
            uow.commit()
        settled = sent + failed + dead_lettered + deferred
        return NotificationDispatchResult(
            claimed=len(claimed),
            sent=sent,
            failed=failed,
            dead_lettered=dead_lettered,
            stale=len(claimed) - settled,
        )

    def _send(self, delivery: ClaimedNotificationDelivery) -> DeliveryOutcome:
        try:
            adapter = self._adapters[delivery.channel_kind]
            secret = (
                self._cipher.decrypt(delivery.encrypted_secret, delivery.secret_key_id)
                if delivery.encrypted_secret and delivery.secret_key_id
                else None
            )
            title, body = _render(delivery.template_key, delivery.payload)
            image = None
            if (
                delivery.channel_kind is ChannelKind.QQ
                and delivery.template_key == "conditional_skip.v1"
            ):
                if delivery.payload.get("capture_id") and self._execution_evidence is None:
                    raise NotificationAdapterError("evidence_reader_unavailable", retryable=True)
                try:
                    image = (
                        self._execution_evidence(delivery.payload)
                        if self._execution_evidence
                        else None
                    )
                except EvidencePending:
                    return DeliveryOutcome(
                        delivery_id=delivery.delivery_id,
                        row_version=delivery.row_version,
                        attempt_count=delivery.attempt_count,
                        outcome=AttemptOutcome.FAILED,
                        retryable=True,
                        started_at=delivery.started_at,
                        evidence_pending=True,
                    )
                if image is None:
                    body += "\n触发截图未生成、已清理或不可用，请查看任务详情。"  # noqa: RUF001
            if delivery.template_key in {"script_failure.v1", "conditional_skip.v1"}:
                link = task_link(self._public_url, delivery.payload)
                if link:
                    body += f"\n任务详情：{link}"  # noqa: RUF001
            if delivery.template_key == "forward.v1":
                if self._review_body is None:
                    raise NotificationAdapterError("review_reader_unavailable", retryable=True)
                body = self._review_body(UUID(str(delivery.payload.get("review_id"))))
            receipt = adapter.send(
                NotificationMessage(
                    delivery_id=delivery.delivery_id,
                    kind=delivery.notification_kind,
                    targets=delivery.targets,
                    template_key=delivery.template_key,
                    title=title,
                    body=body,
                    payload=dict(delivery.payload),
                    channel_settings=dict(delivery.channel_settings),
                    bot_service_id=delivery.bot_service_id,
                    secret=secret,
                    image_png=image,
                ),
                idempotency_key=str(delivery.delivery_id),
            )
            return DeliveryOutcome(
                delivery_id=delivery.delivery_id,
                row_version=delivery.row_version,
                attempt_count=delivery.attempt_count,
                outcome=AttemptOutcome.SENT,
                retryable=False,
                provider_message_id=receipt.provider_message_id,
                started_at=delivery.started_at,
            )
        except NotificationAdapterError as exc:
            return _failed_outcome(delivery, type(exc).__name__, exc.code, exc.retryable)
        except Exception as exc:
            return _failed_outcome(delivery, type(exc).__name__, "adapter_unavailable", True)


def _failed_outcome(
    delivery: ClaimedNotificationDelivery,
    error_type: str,
    error_code: str,
    retryable: bool,
) -> DeliveryOutcome:
    return DeliveryOutcome(
        delivery_id=delivery.delivery_id,
        row_version=delivery.row_version,
        attempt_count=delivery.attempt_count,
        outcome=AttemptOutcome.FAILED,
        retryable=retryable,
        error_type=error_type[:100],
        error_code=error_code[:100],
        started_at=delivery.started_at,
    )


def _render(template_key: str, payload: dict[str, object]) -> tuple[str, str]:
    allowed = {
        "conditional_skip.v1": "条件跳过",
        "script_failure.v1": "脚本执行失败",
        "storage_low.v1": "存储空间不足",
        "terminal_alert.v1": "终端异常",
        "operation_failure.v1": "后台操作失败",
        "test.v1": "AL-1S 测试通知",
        "forward.v1": "消息转发",
    }
    title = allowed.get(template_key)
    if title is None:
        raise NotificationAdapterError("template_not_supported", retryable=False)
    summary = payload.get("summary")
    body = str(summary) if isinstance(summary, str) else title
    return title, body
