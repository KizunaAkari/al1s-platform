from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import Mock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine, create_engine, event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.bot_models import BotServiceRow
from al1s.adapters.postgres.notification_models import (
    EncryptedSecretRow,
    NotificationAttemptRow,
    NotificationChannelRow,
    NotificationDeliveryRow,
    NotificationIntentRow,
    NotificationRouteRow,
)
from al1s.adapters.postgres.notification_unit_of_work import NotificationSqlAlchemyUnitOfWork
from al1s.app.config import Settings
from al1s.app.factory import create_app
from al1s.bots.types import BotServiceKind
from al1s.infrastructure.readiness import ReadinessResult
from al1s.kernel.types import ClaimedOutboxEvent
from al1s.notifications.dispatcher import NotificationDispatcher
from al1s.notifications.errors import NotificationAdapterError, NotificationDomainError
from al1s.notifications.event_publisher import NotificationEventPublisher
from al1s.notifications.ports import NotificationUnitOfWork
from al1s.notifications.query_service import NotificationQueryService
from al1s.notifications.security import FernetSecretCipher
from al1s.notifications.service import NotificationService
from al1s.notifications.types import (
    AdapterReceipt,
    AttemptOutcome,
    ChannelKind,
    DeliveryOutcome,
    DeliveryStatus,
    NotificationKind,
    NotificationMessage,
    SecretChange,
)

pytestmark = pytest.mark.integration


class ScriptedAdapter:
    def __init__(self, *, retryable_failures: int = 0) -> None:
        self.retryable_failures = retryable_failures
        self.messages: list[NotificationMessage] = []
        self.idempotency_keys: list[str] = []

    def send(self, message: NotificationMessage, *, idempotency_key: str) -> AdapterReceipt:
        self.messages.append(message)
        self.idempotency_keys.append(idempotency_key)
        if self.retryable_failures:
            self.retryable_failures -= 1
            raise NotificationAdapterError("provider_timeout", retryable=True)
        return AdapterReceipt(provider_message_id=f"provider-{message.delivery_id}")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    database_url = os.getenv("AL1S_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("AL1S_TEST_DATABASE_URL is required for integration tests")
    database_engine = create_engine(database_url, pool_pre_ping=True)
    with database_engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    yield database_engine
    database_engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(engine: Engine) -> Iterator[None]:
    table_names = (
        "notification_attempts, notification_deliveries, notification_intents, "
        "notification_routes, notification_channels, encrypted_secrets, "
        "bot_services, "
        "audit_logs, outbox_events"
    )
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {table_names} CASCADE"))
    yield
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {table_names} CASCADE"))


def _notification_service(
    engine: Engine, clock: list[datetime]
) -> tuple[NotificationService, FernetSecretCipher]:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def uow_factory() -> NotificationUnitOfWork:
        return cast(NotificationUnitOfWork, NotificationSqlAlchemyUnitOfWork(sessions))

    cipher = FernetSecretCipher("stage7-notification-master-key-at-least-32-bytes")
    return NotificationService(uow_factory, cipher, now=lambda: clock[0]), cipher


def _dispatcher(
    engine: Engine,
    clock: list[datetime],
    cipher: FernetSecretCipher,
    adapter: ScriptedAdapter,
) -> NotificationDispatcher:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def uow_factory() -> NotificationUnitOfWork:
        return cast(NotificationUnitOfWork, NotificationSqlAlchemyUnitOfWork(sessions))

    return NotificationDispatcher(
        uow_factory=uow_factory,
        adapters={ChannelKind.QQ: adapter},
        cipher=cipher,
        worker_id="notification-test-worker",
        now=lambda: clock[0],
    )


def _create_bot_service(engine: Engine, kind: BotServiceKind) -> UUID:
    service_id = uuid4()
    now = datetime(2026, 9, 5, 8, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            BotServiceRow(
                id=service_id,
                kind=kind.value,
                name=f"{kind.value}-{service_id}",
                enabled=True,
                row_version=1,
                created_at=now,
                updated_at=now,
            )
        )
    return service_id


def _create_qq_route(
    engine: Engine,
    service: NotificationService,
    *,
    name: str = "QQ notifications",
    kind: NotificationKind = NotificationKind.CONDITIONAL_SKIP,
) -> None:
    bot_service_id = _create_bot_service(engine, BotServiceKind.ONEBOT_GATEWAY)
    channel = service.create_channel(
        kind=ChannelKind.QQ,
        name=name,
        settings={},
        secret=None,
        bot_service_id=bot_service_id,
        actor_id=uuid4(),
        correlation_id=uuid4(),
    )
    service.create_route(
        notification_kind=kind,
        channel_id=channel.channel_id,
        targets=("qq-group:123456",),
        template_key=f"{kind.value}.v1",
        actor_id=uuid4(),
        correlation_id=uuid4(),
    )


def test_operation_failure_route_persists_and_replays_once(engine: Engine) -> None:
    clock = [datetime(2026, 9, 21, tzinfo=UTC)]
    service, _ = _notification_service(engine, clock)
    _create_qq_route(engine, service, kind=NotificationKind.OPERATION_FAILURE)
    args = dict(
        source_event_id=uuid4(),
        notification_kind=NotificationKind.OPERATION_FAILURE,
        source_type="maa_import_batch",
        source_id=uuid4(),
        correlation_id=uuid4(),
        payload={"summary": "Import failed"},
        occurred_at=clock[0],
    )
    first = service.materialize_intent(**args)
    assert service.materialize_intent(**args).intent_id == first.intent_id
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(NotificationDeliveryRow)) == 1


def test_failure_event_routes_both_channels_and_replay_does_not_resend(engine: Engine) -> None:
    clock = [datetime(2026, 9, 9, tzinfo=UTC)]
    service, cipher = _notification_service(engine, clock)
    bot_id = _create_bot_service(engine, BotServiceKind.ONEBOT_GATEWAY)
    for kind in (ChannelKind.QQ, ChannelKind.SMTP):
        channel = service.create_channel(
            kind=kind,
            name=f"Simulation {kind.value}",
            settings={}
            if kind is ChannelKind.QQ
            else {
                "host": "smtp.example.test",
                "port": 465,
                "security": "ssl",
                "from_address": "owner@example.test",
            },
            secret=None,
            bot_service_id=bot_id if kind is ChannelKind.QQ else None,
            actor_id=uuid4(),
            correlation_id=uuid4(),
        )
        service.create_route(
            notification_kind=NotificationKind.SCRIPT_FAILURE,
            channel_id=channel.channel_id,
            targets=("qq-group:123",) if kind is ChannelKind.QQ else ("owner@example.test",),
            template_key="script_failure.v1",
            actor_id=uuid4(),
            correlation_id=uuid4(),
        )
        if kind is ChannelKind.QQ:
            service.create_route(
                notification_kind=NotificationKind.CONDITIONAL_SKIP,
                channel_id=channel.channel_id,
                targets=("qq-group:123",),
                template_key="conditional_skip.v1",
                actor_id=uuid4(),
                correlation_id=uuid4(),
            )
    event = ClaimedOutboxEvent(
        event_id=uuid4(),
        event_type="execution.ended.v1",
        schema_version=1,
        aggregate_type="execution",
        aggregate_id=uuid4(),
        correlation_id=uuid4(),
        occurred_at=clock[0],
        payload={"result_kind": "failure"},
        attempt_count=1,
        row_version=1,
    )
    downstream = Mock()
    publisher = NotificationEventPublisher(downstream, service)
    publisher.publish(event)
    publisher.publish(event)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    qq, smtp = ScriptedAdapter(), ScriptedAdapter()
    dispatcher = NotificationDispatcher(
        uow_factory=lambda: NotificationSqlAlchemyUnitOfWork(sessions),
        adapters={ChannelKind.QQ: qq, ChannelKind.SMTP: smtp},
        cipher=cipher,
        worker_id="simulation",
        now=lambda: clock[0],
    )
    assert dispatcher.dispatch_once().sent == 2
    publisher.publish(event)
    assert dispatcher.dispatch_once().claimed == 0
    assert len(qq.messages) == len(smtp.messages) == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(NotificationIntentRow)) == 1
        assert session.scalar(select(func.count()).select_from(NotificationDeliveryRow)) == 2
    skip = ClaimedOutboxEvent(
        event_id=uuid4(),
        event_type="execution.conditional_skip.v1",
        schema_version=1,
        aggregate_type="execution",
        aggregate_id=event.aggregate_id,
        correlation_id=uuid4(),
        occurred_at=clock[0],
        payload={"module_number": 3, "module_step_number": 2},
        attempt_count=1,
        row_version=1,
    )
    publisher.publish(skip)
    publisher.publish(skip)
    assert dispatcher.dispatch_once().sent == 1
    assert len(smtp.messages) == 1
    assert len(qq.messages) == 2
    assert "组合第3步" in qq.messages[-1].body
    assert "脚本第2步" in qq.messages[-1].body


def test_waiting_for_image_does_not_consume_send_attempt(engine: Engine) -> None:
    from al1s.execution.notification_evidence import EvidencePending

    clock = [datetime(2026, 9, 20, tzinfo=UTC)]
    service, cipher = _notification_service(engine, clock)
    _create_qq_route(engine, service)
    service.materialize_intent(
        source_event_id=uuid4(),
        notification_kind=NotificationKind.CONDITIONAL_SKIP,
        source_type="execution",
        source_id=uuid4(),
        correlation_id=uuid4(),
        occurred_at=clock[0],
        payload={"summary": "Skip", "capture_id": str(uuid4())},
    )
    adapter = ScriptedAdapter()
    dispatcher = _dispatcher(engine, clock, cipher, adapter)
    evidence = Mock(side_effect=EvidencePending())
    dispatcher._execution_evidence = evidence
    for _ in range(4):
        result = dispatcher.dispatch_once()
        assert result.claimed == 1 and result.sent == result.dead_lettered == result.stale == 0
        with Session(engine) as session:
            item = session.scalar(select(NotificationDeliveryRow))
            assert item.status == "pending" and item.attempt_count == 0
            assert session.scalar(select(func.count()).select_from(NotificationAttemptRow)) == 0
        assert not adapter.messages
        clock[0] += timedelta(minutes=1)
    evidence.side_effect = None
    evidence.return_value = b"\x89PNG\r\n\x1a\nmock"
    assert dispatcher.dispatch_once().sent == 1
    assert adapter.messages[0].image_png == evidence.return_value
    assert "image_png" not in adapter.messages[0].payload
    with Session(engine) as session:
        item = session.scalar(select(NotificationDeliveryRow))
        assert item.attempt_count == 1 and item.status == "sent"
        assert session.scalar(select(func.count()).select_from(NotificationAttemptRow)) == 1


def test_smtp_secret_is_encrypted_and_route_matrix_is_enforced(engine: Engine) -> None:
    clock = [datetime(2026, 9, 5, 8, tzinfo=UTC)]
    service, _cipher = _notification_service(engine, clock)
    channel = service.create_channel(
        kind=ChannelKind.SMTP,
        name="Primary email",
        settings={
            "host": "smtp.example.test",
            "port": 465,
            "from_address": "al1s@example.test",
            "security": "ssl",
        },
        secret="smtp-app-password",
        bot_service_id=None,
        actor_id=uuid4(),
        correlation_id=uuid4(),
    )

    with Session(engine) as session:
        row = session.get(NotificationChannelRow, channel.channel_id)
        assert row is not None
        secret = session.get(EncryptedSecretRow, row.secret_id)
        assert secret is not None
        assert "smtp-app-password" not in secret.ciphertext

    with pytest.raises(NotificationDomainError) as error:
        service.create_route(
            notification_kind=NotificationKind.CONDITIONAL_SKIP,
            channel_id=channel.channel_id,
            targets=("owner@example.test",),
            template_key="conditional_skip.v1",
            actor_id=uuid4(),
            correlation_id=uuid4(),
        )
    assert error.value.code == "notification_route_channel_forbidden"


def test_bot_backed_channels_require_a_matching_typed_service(engine: Engine) -> None:
    clock = [datetime(2026, 9, 5, 8, 30, tzinfo=UTC)]
    service, _cipher = _notification_service(engine, clock)
    gateway_id = _create_bot_service(engine, BotServiceKind.ONEBOT_GATEWAY)
    bridge_id = _create_bot_service(engine, BotServiceKind.DISCORD_BRIDGE)

    with pytest.raises(NotificationDomainError) as missing:
        service.create_channel(
            kind=ChannelKind.QQ,
            name="Missing Bot binding",
            settings={},
            secret=None,
            bot_service_id=None,
            actor_id=uuid4(),
            correlation_id=uuid4(),
        )
    assert missing.value.code == "bot_service_binding_required"

    with pytest.raises(NotificationDomainError) as wrong_kind:
        service.create_channel(
            kind=ChannelKind.QQ,
            name="Wrong Bot binding",
            settings={},
            secret=None,
            bot_service_id=bridge_id,
            actor_id=uuid4(),
            correlation_id=uuid4(),
        )
    assert wrong_kind.value.code == "invalid_bot_service_binding"

    channel = service.create_channel(
        kind=ChannelKind.QQ,
        name="Typed OneBot channel",
        settings={},
        secret=None,
        bot_service_id=gateway_id,
        actor_id=uuid4(),
        correlation_id=uuid4(),
    )
    assert channel.bot_service_id == gateway_id

    with pytest.raises(NotificationDomainError) as smtp_binding:
        service.create_channel(
            kind=ChannelKind.SMTP,
            name="SMTP with Bot",
            settings={
                "host": "smtp.example.test",
                "port": 465,
                "from_address": "al1s@example.test",
                "security": "ssl",
            },
            secret=None,
            bot_service_id=gateway_id,
            actor_id=uuid4(),
            correlation_id=uuid4(),
        )
    assert smtp_binding.value.code == "smtp_channel_cannot_bind_bot"


def test_intent_materialization_is_idempotent_and_query_count_is_bounded(engine: Engine) -> None:
    clock = [datetime(2026, 9, 5, 9, tzinfo=UTC)]
    service, _cipher = _notification_service(engine, clock)
    for index in range(10):
        _create_qq_route(engine, service, name=f"QQ route {index}")
    source_event_id = uuid4()
    query_count = 0
    source_id, correlation_id = uuid4(), uuid4()

    def count_query(*_args: object, **_kwargs: object) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        first = service.materialize_intent(
            source_event_id=source_event_id,
            notification_kind=NotificationKind.CONDITIONAL_SKIP,
            source_type="execution_step",
            source_id=source_id,
            correlation_id=correlation_id,
            payload={"summary": "组合脚本 #3 · 第 54 步触发条件跳过"},
            occurred_at=clock[0],
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_query)

    replay = service.materialize_intent(
        source_event_id=source_event_id,
        notification_kind=NotificationKind.CONDITIONAL_SKIP,
        source_type="execution_step",
        source_id=source_id,
        correlation_id=correlation_id,
        payload={"summary": "组合脚本 #3 · 第 54 步触发条件跳过"},
        occurred_at=clock[0],
    )

    assert first.intent_id == replay.intent_id
    assert query_count == 4
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(NotificationIntentRow)) == 1
        assert session.scalar(select(func.count()).select_from(NotificationDeliveryRow)) == 10


def test_dispatcher_retries_then_sends_with_stable_idempotency_key(engine: Engine) -> None:
    clock = [datetime(2026, 9, 5, 10, tzinfo=UTC)]
    service, cipher = _notification_service(engine, clock)
    _create_qq_route(engine, service)
    service.materialize_intent(
        source_event_id=uuid4(),
        notification_kind=NotificationKind.CONDITIONAL_SKIP,
        source_type="execution_step",
        source_id=uuid4(),
        correlation_id=uuid4(),
        payload={"summary": "已跳过国服pcr-领取体力 · 第 02 步"},
        occurred_at=clock[0],
    )
    adapter = ScriptedAdapter(retryable_failures=1)
    dispatcher = _dispatcher(engine, clock, cipher, adapter)

    first = dispatcher.dispatch_once()
    assert (first.claimed, first.failed, first.sent) == (1, 1, 0)
    clock[0] += timedelta(seconds=4)
    assert dispatcher.dispatch_once().claimed == 0
    clock[0] += timedelta(seconds=1)
    second = dispatcher.dispatch_once()

    assert (second.claimed, second.failed, second.sent) == (1, 0, 1)
    assert adapter.idempotency_keys[0] == adapter.idempotency_keys[1]
    assert adapter.messages[-1].title == "条件跳过"
    assert (
        adapter.messages[-1].body
        == "已跳过国服pcr-领取体力 · 第 02 步\n触发截图未生成、已清理或不可用，请查看任务详情。"  # noqa: RUF001 - exact UI copy
    )
    with Session(engine) as session:
        delivery = session.scalar(select(NotificationDeliveryRow))
        assert delivery is not None
        assert delivery.status == DeliveryStatus.SENT.value
        assert delivery.attempt_count == 2
        assert delivery.sent_at == clock[0]
        attempts = session.scalars(
            select(NotificationAttemptRow).order_by(NotificationAttemptRow.attempt_no)
        ).all()
        assert [attempt.outcome for attempt in attempts] == ["failed", "sent"]


def test_expired_delivery_lease_fences_late_settlement(engine: Engine) -> None:
    clock = [datetime(2026, 9, 5, 10, 15, tzinfo=UTC)]
    service, _cipher = _notification_service(engine, clock)
    _create_qq_route(engine, service)
    service.materialize_intent(
        source_event_id=uuid4(),
        notification_kind=NotificationKind.CONDITIONAL_SKIP,
        source_type="execution_step",
        source_id=uuid4(),
        correlation_id=uuid4(),
        payload={"summary": "lease fencing"},
        occurred_at=clock[0],
    )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    first_uow = NotificationSqlAlchemyUnitOfWork(sessions)
    with first_uow:
        first = first_uow.deliveries.claim_batch(
            worker_id="worker-a",
            now=clock[0],
            lease_duration=timedelta(seconds=30),
            limit=1,
        )[0]
        first_uow.commit()
    clock[0] += timedelta(seconds=20)
    with NotificationSqlAlchemyUnitOfWork(sessions) as renewal_uow:
        assert (
            renewal_uow.deliveries.renew_batch(
                [first],
                worker_id="wrong-worker",
                now=clock[0],
                lease_duration=timedelta(seconds=30),
            )
            == 0
        )
        assert (
            renewal_uow.deliveries.renew_batch(
                [first],
                worker_id="worker-a",
                now=clock[0],
                lease_duration=timedelta(seconds=30),
            )
            == 1
        )
        renewal_uow.commit()
    clock[0] += timedelta(seconds=31)
    with NotificationSqlAlchemyUnitOfWork(sessions) as expired_uow:
        assert (
            expired_uow.deliveries.renew_batch(
                [first],
                worker_id="worker-a",
                now=clock[0],
                lease_duration=timedelta(seconds=30),
            )
            == 0
        )
        expired_uow.commit()
    second_uow = NotificationSqlAlchemyUnitOfWork(sessions)
    with second_uow:
        second = second_uow.deliveries.claim_batch(
            worker_id="worker-b",
            now=clock[0],
            lease_duration=timedelta(seconds=30),
            limit=1,
        )[0]
        second_uow.commit()

    late_uow = NotificationSqlAlchemyUnitOfWork(sessions)
    with late_uow:
        late_result = late_uow.deliveries.settle_batch(
            [
                DeliveryOutcome(
                    delivery_id=first.delivery_id,
                    row_version=first.row_version,
                    attempt_count=first.attempt_count,
                    outcome=AttemptOutcome.SENT,
                    retryable=False,
                )
            ],
            worker_id="worker-a",
            now=clock[0],
            max_attempts=5,
            base_retry_delay=timedelta(seconds=5),
            max_retry_delay=timedelta(minutes=15),
        )
        late_uow.commit()
    assert late_result == (0, 0, 0)

    current_uow = NotificationSqlAlchemyUnitOfWork(sessions)
    with current_uow:
        current_result = current_uow.deliveries.settle_batch(
            [
                DeliveryOutcome(
                    delivery_id=second.delivery_id,
                    row_version=second.row_version,
                    attempt_count=second.attempt_count,
                    outcome=AttemptOutcome.SENT,
                    retryable=False,
                )
            ],
            worker_id="worker-b",
            now=clock[0],
            max_attempts=5,
            base_retry_delay=timedelta(seconds=5),
            max_retry_delay=timedelta(minutes=15),
        )
        current_uow.commit()
    assert current_result == (1, 0, 0)
    with Session(engine) as session:
        delivery = session.scalar(select(NotificationDeliveryRow))
        assert delivery is not None
        assert delivery.status == DeliveryStatus.SENT.value
        attempts = session.scalars(
            select(NotificationAttemptRow).order_by(NotificationAttemptRow.attempt_no)
        ).all()
        assert delivery.attempt_count == 2
        assert len(attempts) == 2
        assert attempts[0].attempt_no == 1
        assert attempts[0].error_code == "delivery_outcome_unknown"
        assert attempts[1].attempt_no == 2
        assert attempts[1].outcome == AttemptOutcome.SENT.value


def test_test_notification_splits_targets_and_replays_stably(engine: Engine) -> None:
    clock = [datetime(2026, 9, 9, tzinfo=UTC)]
    service, cipher = _notification_service(engine, clock)
    bot_id = _create_bot_service(engine, BotServiceKind.ONEBOT_GATEWAY)
    channel = service.create_channel(
        kind=ChannelKind.QQ,
        name="Target isolation",
        settings={},
        secret=None,
        bot_service_id=bot_id,
        actor_id=uuid4(),
        correlation_id=uuid4(),
    )
    event_id = uuid4()
    arguments = dict(
        source_event_id=event_id,
        channel_id=channel.channel_id,
        targets=("qq-group:123", "qq-private:123", "qq-group:123"),
        summary="Independent recipients",
        correlation_id=uuid4(),
    )
    first = service.enqueue_test(**arguments)
    second = service.enqueue_test(**arguments)
    assert first.delivery_id == second.delivery_id
    with Session(engine) as session:
        rows = session.scalars(select(NotificationDeliveryRow)).all()
        assert len(rows) == 2
        assert {row.target_key for row in rows} == {"qq-group:123", "qq-private:123"}
        assert all(len(row.targets) == 1 for row in rows)

    class TargetAdapter(ScriptedAdapter):
        def send(self, message: NotificationMessage, *, idempotency_key: str) -> AdapterReceipt:
            self.messages.append(message)
            if (
                message.targets == ("qq-private:123",)
                and sum(item.targets == message.targets for item in self.messages) == 1
            ):
                raise NotificationAdapterError("temporary_rejection", retryable=True)
            return AdapterReceipt(provider_message_id=str(message.delivery_id))

    adapter = TargetAdapter()
    dispatcher = _dispatcher(engine, clock, cipher, adapter)
    result = dispatcher.dispatch_once()
    assert (result.sent, result.failed) == (1, 1)
    clock[0] += timedelta(seconds=6)
    assert dispatcher.dispatch_once().sent == 1
    assert [message.targets for message in adapter.messages].count(("qq-group:123",)) == 1
    assert [message.targets for message in adapter.messages].count(("qq-private:123",)) == 2


def test_smtp_crashes_consume_three_attempts_without_fourth_claim(engine: Engine) -> None:
    clock = [datetime(2026, 9, 9, 10, tzinfo=UTC)]
    service, _cipher = _notification_service(engine, clock)
    channel = service.create_channel(
        kind=ChannelKind.SMTP,
        name="Crash budget",
        settings={
            "host": "smtp.example.test",
            "port": 465,
            "from_address": "al1s@example.test",
            "security": "ssl",
        },
        secret=None,
        bot_service_id=None,
        actor_id=uuid4(),
        correlation_id=uuid4(),
    )
    service.create_route(
        notification_kind=NotificationKind.SCRIPT_FAILURE,
        channel_id=channel.channel_id,
        targets=("owner@example.test",),
        template_key="script_failure.v1",
        actor_id=uuid4(),
        correlation_id=uuid4(),
    )
    service.materialize_intent(
        source_event_id=uuid4(),
        notification_kind=NotificationKind.SCRIPT_FAILURE,
        source_type="execution",
        source_id=uuid4(),
        correlation_id=uuid4(),
        payload={"summary": "Crash budget"},
        occurred_at=clock[0],
    )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    for number in range(1, 5):
        with NotificationSqlAlchemyUnitOfWork(sessions) as uow:
            claimed = uow.deliveries.claim_batch(
                worker_id=f"restarted-{number}",
                now=clock[0],
                lease_duration=timedelta(seconds=30),
                limit=1,
            )
            uow.commit()
        assert [item.attempt_count for item in claimed] == ([number] if number < 4 else [])
        clock[0] += timedelta(seconds=31)
    with Session(engine) as session:
        delivery = session.scalar(select(NotificationDeliveryRow))
        assert delivery is not None
        assert delivery.status == DeliveryStatus.DEAD_LETTER.value
        assert delivery.attempt_count == 3
        assert delivery.last_error_code == "delivery_outcome_unknown"
        attempts = session.scalars(
            select(NotificationAttemptRow).order_by(NotificationAttemptRow.attempt_no)
        ).all()
        assert [attempt.attempt_no for attempt in attempts] == [1, 2, 3]
        assert all(attempt.error_code == "delivery_outcome_unknown" for attempt in attempts)


def test_notification_delivery_queries_remain_bounded_at_one_hundred_thousand_rows(
    engine: Engine,
) -> None:
    now = datetime(2026, 9, 5, 10, 20, tzinfo=UTC)
    service, _cipher = _notification_service(engine, [now])
    bot_service_id = _create_bot_service(engine, BotServiceKind.ONEBOT_GATEWAY)
    channel = service.create_channel(
        kind=ChannelKind.QQ,
        name="Performance channel",
        settings={},
        secret=None,
        bot_service_id=bot_service_id,
        actor_id=uuid4(),
        correlation_id=uuid4(),
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO notification_intents (
                    id, source_event_id, notification_kind, source_type, source_id,
                    correlation_id, schema_version, payload, disposition, occurred_at, created_at
                )
                SELECT
                    gen_random_uuid(), gen_random_uuid(), 'test', 'performance_fixture',
                    gen_random_uuid(), gen_random_uuid(), 1, '{}'::jsonb, 'queued',
                    :now, :now + series_no * interval '1 microsecond'
                FROM generate_series(1, 100000) AS series_no
                """
            ),
            {"now": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO notification_deliveries (
                    id, intent_id, route_id, channel_id, channel_kind, targets,
                    template_key, status, attempt_count, available_at, row_version, created_at
                )
                SELECT
                    gen_random_uuid(), intent_id, NULL, :channel_id, 'qq',
                    '["qq-group:performance"]'::jsonb, 'test.v1',
                    CASE WHEN row_no <= 100 THEN 'pending' ELSE 'dead_letter' END,
                    0, :now, 1, created_at
                FROM (
                    SELECT
                        id AS intent_id,
                        created_at,
                        row_number() OVER (ORDER BY created_at) row_no
                    FROM notification_intents
                    WHERE source_type = 'performance_fixture'
                ) fixture
                """
            ),
            {"channel_id": channel.channel_id, "now": now},
        )
        connection.execute(text("ANALYZE notification_deliveries"))

    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    query_count = 0

    def count_query(*_args: object, **_kwargs: object) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        uow = NotificationSqlAlchemyUnitOfWork(sessions)
        with uow:
            claimed = uow.deliveries.claim_batch(
                worker_id="performance-worker",
                now=now,
                lease_duration=timedelta(seconds=30),
                limit=50,
            )
            uow.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", count_query)
    assert len(claimed) == 50
    assert query_count == 2

    with engine.connect() as connection:
        dispatch_plan = connection.execute(
            text(
                """
                EXPLAIN (FORMAT JSON)
                SELECT id
                FROM notification_deliveries
                WHERE status = 'pending' AND available_at <= :now
                ORDER BY available_at, created_at
                LIMIT 50
                """
            ),
            {"now": now},
        ).scalar_one()
        page_plan = connection.execute(
            text(
                """
                EXPLAIN (FORMAT JSON)
                SELECT id
                FROM notification_deliveries
                ORDER BY created_at DESC, id DESC
                LIMIT 50
                """
            )
        ).scalar_one()
    assert "ix_notification_deliveries_dispatch" in str(dispatch_plan)
    assert "ix_notification_deliveries_created" in str(page_plan)


def test_route_updates_only_affect_future_intents_and_disable_pending_deliveries(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 9, 5, 10, 30, tzinfo=UTC)]
    service, _cipher = _notification_service(engine, clock)
    bot_service_id = _create_bot_service(engine, BotServiceKind.ONEBOT_GATEWAY)
    channel = service.create_channel(
        kind=ChannelKind.QQ,
        name="Snapshot route channel",
        settings={},
        secret=None,
        bot_service_id=bot_service_id,
        actor_id=uuid4(),
        correlation_id=uuid4(),
    )
    route = service.create_route(
        notification_kind=NotificationKind.CONDITIONAL_SKIP,
        channel_id=channel.channel_id,
        targets=("qq-group:old",),
        template_key="conditional_skip.v1",
        actor_id=uuid4(),
        correlation_id=uuid4(),
    )
    old_intent = service.materialize_intent(
        source_event_id=uuid4(),
        notification_kind=NotificationKind.CONDITIONAL_SKIP,
        source_type="execution_step",
        source_id=uuid4(),
        correlation_id=uuid4(),
        payload={"summary": "old route snapshot"},
        occurred_at=clock[0],
    )

    updated = service.update_route(
        route_id=route.route_id,
        expected_version=1,
        targets=("qq-group:new",),
        template_key="conditional_skip.v2",
        enabled=None,
        actor_id=uuid4(),
        correlation_id=uuid4(),
    )
    new_intent = service.materialize_intent(
        source_event_id=uuid4(),
        notification_kind=NotificationKind.CONDITIONAL_SKIP,
        source_type="execution_step",
        source_id=uuid4(),
        correlation_id=uuid4(),
        payload={"summary": "new route snapshot"},
        occurred_at=clock[0],
    )

    assert updated.row_version == 2
    with Session(engine) as session:
        deliveries = session.scalars(
            select(NotificationDeliveryRow).order_by(NotificationDeliveryRow.created_at)
        ).all()
        by_intent = {item.intent_id: item for item in deliveries}
        assert by_intent[old_intent.intent_id].targets == ["qq-group:old"]
        assert by_intent[old_intent.intent_id].template_key == "conditional_skip.v1"
        assert by_intent[new_intent.intent_id].targets == ["qq-group:new"]
        assert by_intent[new_intent.intent_id].template_key == "conditional_skip.v2"

    disabled = service.update_route(
        route_id=route.route_id,
        expected_version=2,
        targets=None,
        template_key=None,
        enabled=False,
        actor_id=uuid4(),
        correlation_id=uuid4(),
    )
    assert disabled.row_version == 3
    with Session(engine) as session:
        assert set(session.scalars(select(NotificationDeliveryRow.status))) == {
            DeliveryStatus.CANCELLED.value
        }
        stored_route = session.get(NotificationRouteRow, route.route_id)
        assert stored_route is not None
        assert stored_route.enabled is False


def test_notification_http_contract_is_idempotent_and_never_returns_secrets(
    engine: Engine,
) -> None:
    clock = [datetime(2026, 9, 5, 11, tzinfo=UTC)]
    service, cipher = _notification_service(engine, clock)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def uow_factory() -> NotificationUnitOfWork:
        return cast(NotificationUnitOfWork, NotificationSqlAlchemyUnitOfWork(sessions))

    class Ready:
        def check(self) -> ReadinessResult:
            return ReadinessResult(ready=True, dependencies={})

        def close(self) -> None:
            return

    app = create_app(
        Settings(admin_password="notification-test-password", admin_cookie_secure=False),
        readiness_service=Ready(),  # type: ignore[arg-type]
        notification_service=service,
        notification_query_service=NotificationQueryService(uow_factory),
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport, base_url="http://test", headers={"X-AL1S-CSRF": "1"}
            ) as client,
        ):
            login = await client.post(
                "/api/v1/auth/login", json={"password": "notification-test-password"}
            )
            assert login.status_code == 200
            request = {
                "kind": "smtp",
                "name": "HTTP SMTP",
                "settings": {
                    "host": "smtp.example.test",
                    "port": 465,
                    "from_address": "al1s@example.test",
                    "security": "ssl",
                    "username": "al1s@example.test",
                },
                "secret": "http-app-password",
            }
            first = await client.post(
                "/api/v1/notifications/channels",
                headers={"Idempotency-Key": "http-smtp-channel"},
                json=request,
            )
            replay = await client.post(
                "/api/v1/notifications/channels",
                headers={"Idempotency-Key": "http-smtp-channel"},
                json=request,
            )
            assert first.status_code == replay.status_code == 201
            assert first.json() == replay.json()
            assert first.json()["secret_configured"] is True
            assert "http-app-password" not in first.text
            channel_id = first.json()["channel_id"]

            conflict_request = dict(request)
            conflict_request["name"] = "Different SMTP"
            conflict = await client.post(
                "/api/v1/notifications/channels",
                headers={"Idempotency-Key": "http-smtp-channel"},
                json=conflict_request,
            )
            assert conflict.status_code == 409
            assert conflict.json()["code"] == "idempotency_key_conflict"

            queued = await client.post(
                "/api/v1/notifications/tests",
                headers={"Idempotency-Key": "http-smtp-test"},
                json={
                    "channel_id": channel_id,
                    "targets": ["owner@example.test"],
                    "summary": "Stage 7 API delivery test",
                },
            )
            queued_replay = await client.post(
                "/api/v1/notifications/tests",
                headers={"Idempotency-Key": "http-smtp-test"},
                json={
                    "channel_id": channel_id,
                    "targets": ["owner@example.test"],
                    "summary": "Stage 7 API delivery test",
                },
            )
            assert queued.status_code == queued_replay.status_code == 202
            assert queued.json()["delivery_id"] == queued_replay.json()["delivery_id"]
            assert queued.json()["replayed"] is False
            assert queued_replay.json()["replayed"] is True

            updated = await client.patch(
                f"/api/v1/notifications/channels/{channel_id}",
                headers={"If-Match": '"1"'},
                json={
                    "enabled": False,
                    "secret_change": "set",
                    "secret": "replacement-app-password",
                },
            )
            assert updated.status_code == 200
            assert updated.headers["etag"] == '"2"'
            assert updated.json()["enabled"] is False
            assert updated.json()["secret_configured"] is True
            assert "replacement-app-password" not in updated.text

            stale = await client.patch(
                f"/api/v1/notifications/channels/{channel_id}",
                headers={"If-Match": '"1"'},
                json={"name": "stale update"},
            )
            assert stale.status_code == 409
            assert stale.json()["code"] == "notification_version_conflict"

            channels = await client.get("/api/v1/notifications/channels")
            deliveries = await client.get("/api/v1/notifications/deliveries")
            forwarding = await client.get(
                "/api/v1/notifications/deliveries", params={"notification_kind": "forward"}
            )
            tests_only = await client.get(
                "/api/v1/notifications/deliveries", params={"notification_kind": "test"}
            )
            detail = await client.get(
                f"/api/v1/notifications/deliveries/{queued.json()['delivery_id']}"
            )
            assert channels.status_code == deliveries.status_code == detail.status_code == 200
            assert len(channels.json()["items"]) == 1
            assert len(deliveries.json()["items"]) == 1
            assert forwarding.status_code == tests_only.status_code == 200
            assert forwarding.json()["items"] == []
            assert len(tests_only.json()["items"]) == 1
            assert detail.json()["delivery"]["status"] == "cancelled"
            assert detail.json()["attempts"] == []

    asyncio.run(exercise())

    with Session(engine) as session:
        secret = session.scalar(select(EncryptedSecretRow))
        assert secret is not None
        assert cipher.decrypt(secret.ciphertext, secret.key_id) == "replacement-app-password"
        assert session.scalar(select(func.count()).select_from(EncryptedSecretRow)) == 1


def test_channel_rename_and_delete_keep_history_and_remove_routes(engine: Engine) -> None:
    clock = [datetime(2026, 9, 27, 10, tzinfo=UTC)]
    service, _ = _notification_service(engine, clock)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def uow_factory() -> NotificationUnitOfWork:
        return cast(NotificationUnitOfWork, NotificationSqlAlchemyUnitOfWork(sessions))

    class Ready:
        def check(self) -> ReadinessResult:
            return ReadinessResult(ready=True, dependencies={})

        def close(self) -> None:
            return

    app = create_app(
        Settings(admin_password="notification-test-password", admin_cookie_secure=False),
        readiness_service=Ready(),  # type: ignore[arg-type]
        notification_service=service,
        notification_query_service=NotificationQueryService(uow_factory),
    )
    channel = service.create_channel(
        kind=ChannelKind.SMTP,
        name="Before rename",
        settings={
            "host": "smtp.example.test",
            "port": 465,
            "from_address": "al1s@example.test",
            "security": "ssl",
        },
        secret="test-app-password",
        bot_service_id=None,
        actor_id=None,
        correlation_id=uuid4(),
    )
    route = service.create_route(
        notification_kind=NotificationKind.SCRIPT_FAILURE,
        channel_id=channel.channel_id,
        targets=("owner@example.test",),
        template_key="script_failure.v1",
        actor_id=None,
        correlation_id=uuid4(),
    )
    queued = service.enqueue_test(
        source_event_id=uuid4(),
        channel_id=channel.channel_id,
        targets=("owner@example.test",),
        summary="retained history",
        correlation_id=uuid4(),
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport, base_url="http://test", headers={"X-AL1S-CSRF": "1"}
            ) as client,
        ):
            login = await client.post(
                "/api/v1/auth/login", json={"password": "notification-test-password"}
            )
            assert login.status_code == 200
            path = f"/api/v1/notifications/channels/{channel.channel_id}"
            renamed = await client.patch(
                path, headers={"If-Match": '"1"'}, json={"name": "After rename"}
            )
            assert renamed.status_code == 200
            assert renamed.json()["name"] == "After rename"
            assert renamed.json()["row_version"] == 2
            stale = await client.delete(path, headers={"If-Match": '"1"'})
            assert stale.status_code == 409
            assert (await client.get("/api/v1/notifications/routes")).json()["items"][0][
                "route_id"
            ] == str(route.route_id)
            deleted = await client.delete(path, headers={"If-Match": '"2"'})
            assert deleted.status_code == 204
            assert (await client.delete(path, headers={"If-Match": '"2"'})).status_code == 404
            assert (await client.get("/api/v1/notifications/channels")).json()["items"] == []
            assert (await client.get("/api/v1/notifications/routes")).json()["items"] == []
            detail = await client.get(f"/api/v1/notifications/deliveries/{queued.delivery_id}")
            assert detail.status_code == 200
            assert detail.json()["delivery"]["status"] == "cancelled"

    asyncio.run(exercise())
    with Session(engine) as session:
        stored_channel = session.get(NotificationChannelRow, channel.channel_id)
        stored_route = session.get(NotificationRouteRow, route.route_id)
        assert stored_channel is not None and stored_channel.deleted_at is not None
        assert stored_channel.secret_id is None
        assert stored_route is not None and stored_route.deleted_at is not None
        assert session.scalar(select(func.count()).select_from(EncryptedSecretRow)) == 0


def test_channel_rename_rejects_duplicate_active_name(engine: Engine) -> None:
    service, _ = _notification_service(engine, [datetime(2026, 9, 27, 11, tzinfo=UTC)])
    settings = {
        "host": "smtp.example.test",
        "port": 465,
        "from_address": "al1s@example.test",
        "security": "ssl",
    }
    first = service.create_channel(
        kind=ChannelKind.SMTP,
        name="Existing",
        settings=settings,
        secret=None,
        bot_service_id=None,
        actor_id=None,
        correlation_id=uuid4(),
    )
    second = service.create_channel(
        kind=ChannelKind.SMTP,
        name="Rename me",
        settings=settings,
        secret=None,
        bot_service_id=None,
        actor_id=None,
        correlation_id=uuid4(),
    )
    with pytest.raises(NotificationDomainError) as raised:
        service.update_channel(
            channel_id=second.channel_id,
            expected_version=1,
            name=first.name,
            settings=None,
            enabled=None,
            bot_service_id=None,
            secret_change=SecretChange.PRESERVE,
            secret=None,
            actor_id=None,
            correlation_id=uuid4(),
        )
    assert raised.value.code == "notification_channel_conflict"
    with Session(engine) as session:
        stored = session.get(NotificationChannelRow, second.channel_id)
        assert stored is not None and stored.name == "Rename me" and stored.row_version == 1
