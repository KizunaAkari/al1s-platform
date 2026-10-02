from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.models import AuditLogRow, BlobObjectRow, GcJobRow, OutboxEventRow
from al1s.adapters.postgres.repositories import PostgresKernelMaintenanceReader
from al1s.adapters.postgres.unit_of_work import SqlAlchemyUnitOfWork
from al1s.kernel.gc_worker import GcWorker
from al1s.kernel.outbox_dispatcher import OutboxDispatcher
from al1s.kernel.types import (
    BlobObjectHead,
    ClaimedOutboxEvent,
    FailedDelivery,
    NewAuditEntry,
    NewBlob,
    NewOutboxEvent,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('bulk', [False, True])
def test_immediate_cleanup_advances_delayed_recovery_job(engine: Engine, bulk: bool) -> None:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    blob_id, now = uuid4(), datetime.now(UTC)
    with SqlAlchemyUnitOfWork(sessions) as uow:
        uow.blobs.add_pending(NewBlob(blob_id, 'b' * 64, 4, 'image/png', f'review/{blob_id}'))
        assert uow.gc_jobs.schedule(uuid4(), blob_id, now + timedelta(hours=24))
        if bulk:
            assert uow.gc_jobs.schedule_many([(uuid4(), blob_id), (uuid4(), blob_id)], now) == 1
        else:
            assert uow.gc_jobs.schedule(uuid4(), blob_id, now)
        uow.commit()
    with sessions() as session:
        jobs = session.scalars(select(GcJobRow).where(GcJobRow.blob_id == blob_id)).all()
        assert len(jobs) == 1
        assert jobs[0].available_at == now


def test_pending_blob_and_recovery_job_commit_together(engine: Engine) -> None:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    blob_id = uuid4()
    with SqlAlchemyUnitOfWork(sessions) as uow:
        uow.blobs.add_pending(NewBlob(blob_id, 'a' * 64, 4, 'image/png', f'review/{blob_id}'))
        assert uow.gc_jobs.schedule(uuid4(), blob_id, datetime.now(UTC) + timedelta(hours=24))
        uow.commit()
    with sessions() as session:
        assert session.get(BlobObjectRow, blob_id) is not None
        count = session.scalar(
            select(func.count()).select_from(GcJobRow).where(GcJobRow.blob_id == blob_id)
        )
        assert count == 1


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
def clean_kernel_tables(engine: Engine) -> Iterator[None]:
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE audit_logs, gc_jobs, inbox_receipts, "
                "outbox_events, blob_objects CASCADE"
            )
        )
    yield
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE audit_logs, gc_jobs, inbox_receipts, "
                "outbox_events, blob_objects CASCADE"
            )
        )


def _event(event_id: UUID, occurred_at: datetime) -> NewOutboxEvent:
    return NewOutboxEvent(
        event_id=event_id,
        event_type="kernel.test.v1",
        schema_version=1,
        aggregate_type="kernel_test",
        aggregate_id=uuid4(),
        correlation_id=uuid4(),
        occurred_at=occurred_at,
        payload={"value": str(event_id)},
    )


def test_unit_of_work_commits_and_rolls_back_atomically(engine: Engine) -> None:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    committed_blob = uuid4()
    committed_event = uuid4()
    committed_audit = uuid4()

    with SqlAlchemyUnitOfWork(sessions) as uow:
        uow.blobs.add_pending(
            NewBlob(
                blob_id=committed_blob,
                sha256="a" * 64,
                size_bytes=12,
                media_type="image/png",
                object_key=f"kernel-test/{committed_blob}",
            )
        )
        uow.outbox.add(_event(committed_event, now))
        uow.audit.add(
            NewAuditEntry(
                audit_id=committed_audit,
                actor_type="system",
                actor_id=None,
                action="kernel.test",
                target_type="blob",
                target_id=committed_blob,
                correlation_id=uuid4(),
                details={"result": "committed"},
            )
        )
        uow.commit()

    rolled_back_blob = uuid4()
    rolled_back_event = uuid4()
    with (
        pytest.raises(RuntimeError, match="force rollback"),
        SqlAlchemyUnitOfWork(sessions) as uow,
    ):
        uow.blobs.add_pending(
            NewBlob(
                blob_id=rolled_back_blob,
                sha256="b" * 64,
                size_bytes=4,
                media_type="application/octet-stream",
                object_key=f"kernel-test/{rolled_back_blob}",
            )
        )
        uow.outbox.add(_event(rolled_back_event, now))
        raise RuntimeError("force rollback")

    with Session(engine) as session:
        assert session.get(BlobObjectRow, committed_blob) is not None
        assert session.get(OutboxEventRow, committed_event) is not None
        assert session.get(AuditLogRow, committed_audit) is not None
        assert session.get(BlobObjectRow, rolled_back_blob) is None
        assert session.get(OutboxEventRow, rolled_back_event) is None


def test_inbox_receipt_is_idempotent_in_one_transaction(engine: Engine) -> None:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    event_id = uuid4()
    now = datetime.now(UTC)

    with SqlAlchemyUnitOfWork(sessions) as uow:
        assert uow.inbox.record_once("test-consumer", event_id, now)
        assert not uow.inbox.record_once("test-consumer", event_id, now)
        assert uow.inbox.record_once("other-consumer", event_id, now)
        uow.commit()


def test_two_dispatchers_do_not_claim_the_same_events(engine: Engine) -> None:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    event_ids = [uuid4() for _ in range(20)]
    with SqlAlchemyUnitOfWork(sessions) as uow:
        for event_id in event_ids:
            uow.outbox.add(_event(event_id, now))
        uow.commit()

    barrier = Barrier(2)

    def claim(worker_id: str) -> set[UUID]:
        with SqlAlchemyUnitOfWork(sessions) as uow:
            barrier.wait(timeout=5)
            claimed = uow.outbox.claim_batch(
                worker_id=worker_id,
                now=now,
                lease_duration=timedelta(seconds=30),
                limit=10,
            )
            uow.commit()
        return {event.event_id for event in claimed}

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(claim, "worker-a")
        second = executor.submit(claim, "worker-b")
        first_ids = first.result(timeout=10)
        second_ids = second.result(timeout=10)

    assert len(first_ids) == 10
    assert len(second_ids) == 10
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == set(event_ids)


def test_failed_outbox_delivery_retries_then_dead_letters(engine: Engine) -> None:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    event_id = uuid4()
    with SqlAlchemyUnitOfWork(sessions) as uow:
        uow.outbox.add(_event(event_id, now))
        uow.commit()

    with SqlAlchemyUnitOfWork(sessions) as uow:
        claimed = uow.outbox.claim_batch(
            worker_id="worker-a",
            now=now,
            lease_duration=timedelta(seconds=30),
            limit=1,
        )
        assert len(claimed) == 1
        changed = uow.outbox.mark_failed(
            [
                FailedDelivery(
                    event_id=claimed[0].event_id,
                    row_version=claimed[0].row_version,
                    attempt_count=claimed[0].attempt_count,
                    error_type="TimeoutError",
                )
            ],
            worker_id="worker-a",
            failed_at=now,
            max_attempts=1,
            base_retry_delay=timedelta(seconds=1),
            max_retry_delay=timedelta(minutes=1),
        )
        assert changed == 1
        uow.commit()

    with Session(engine) as session:
        row = session.get(OutboxEventRow, event_id)
        assert row is not None
        assert row.status == "dead_letter"
        assert row.attempt_count == 1
        assert row.last_error_type == "TimeoutError"


def test_outbox_retry_is_not_available_before_backoff_expires(engine: Engine) -> None:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    event_id = uuid4()
    with SqlAlchemyUnitOfWork(sessions) as uow:
        uow.outbox.add(_event(event_id, now))
        uow.commit()

    with SqlAlchemyUnitOfWork(sessions) as uow:
        first = uow.outbox.claim_batch(
            worker_id="worker-a",
            now=now,
            lease_duration=timedelta(seconds=30),
            limit=1,
        )[0]
        assert (
            uow.outbox.mark_failed(
                [
                    FailedDelivery(
                        event_id=first.event_id,
                        row_version=first.row_version,
                        attempt_count=first.attempt_count,
                        error_type="TimeoutError",
                    )
                ],
                worker_id="worker-a",
                failed_at=now,
                max_attempts=2,
                base_retry_delay=timedelta(seconds=2),
                max_retry_delay=timedelta(minutes=1),
            )
            == 1
        )
        uow.commit()

    with SqlAlchemyUnitOfWork(sessions) as uow:
        assert not uow.outbox.claim_batch(
            worker_id="worker-b",
            now=now + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
            limit=1,
        )
        retried = uow.outbox.claim_batch(
            worker_id="worker-b",
            now=now + timedelta(seconds=2),
            lease_duration=timedelta(seconds=30),
            limit=1,
        )
        assert len(retried) == 1
        uow.commit()


def test_expired_lease_is_reclaimed_and_stale_owner_cannot_ack(engine: Engine) -> None:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    event_id = uuid4()
    with SqlAlchemyUnitOfWork(sessions) as uow:
        uow.outbox.add(_event(event_id, now))
        uow.commit()

    with SqlAlchemyUnitOfWork(sessions) as uow:
        first_claim = uow.outbox.claim_batch(
            worker_id="worker-a",
            now=now,
            lease_duration=timedelta(seconds=1),
            limit=1,
        )
        uow.commit()

    reclaim_time = now + timedelta(seconds=2)
    with SqlAlchemyUnitOfWork(sessions) as uow:
        second_claim = uow.outbox.claim_batch(
            worker_id="worker-b",
            now=reclaim_time,
            lease_duration=timedelta(seconds=30),
            limit=1,
        )
        uow.commit()

    assert len(first_claim) == len(second_claim) == 1
    assert second_claim[0].row_version > first_claim[0].row_version
    with SqlAlchemyUnitOfWork(sessions) as uow:
        assert (
            uow.outbox.mark_published(
                first_claim,
                worker_id="worker-a",
                published_at=reclaim_time,
            )
            == 0
        )
        assert (
            uow.outbox.mark_published(
                second_claim,
                worker_id="worker-b",
                published_at=reclaim_time,
            )
            == 1
        )
        uow.commit()


def test_dispatcher_publishes_outside_claim_transaction_and_batches_results(
    engine: Engine,
) -> None:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    successful_id = uuid4()
    failed_id = uuid4()
    with SqlAlchemyUnitOfWork(sessions) as uow:
        uow.outbox.add(_event(successful_id, now))
        uow.outbox.add(_event(failed_id, now))
        uow.commit()

    class Publisher:
        def __init__(self) -> None:
            self.published: list[UUID] = []

        def publish(self, event: ClaimedOutboxEvent) -> None:
            if event.event_id == failed_id:
                raise TimeoutError
            self.published.append(event.event_id)

    publisher = Publisher()
    dispatcher = OutboxDispatcher(
        uow_factory=lambda: SqlAlchemyUnitOfWork(sessions),
        publisher=publisher,
        worker_id="dispatcher-test",
        batch_size=2,
        max_attempts=1,
        now=lambda: now,
    )

    result = dispatcher.dispatch_once()

    assert result.claimed == 2
    assert result.published == 1
    assert result.failed == 1
    assert result.stale == 0
    assert publisher.published == [successful_id]
    with Session(engine) as session:
        successful = session.get(OutboxEventRow, successful_id)
        failed = session.get(OutboxEventRow, failed_id)
        assert successful is not None and successful.status == "published"
        assert failed is not None and failed.status == "dead_letter"


def test_gc_schedule_is_unique_while_job_is_active(engine: Engine) -> None:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    blob_id = uuid4()
    with SqlAlchemyUnitOfWork(sessions) as uow:
        uow.blobs.add_pending(
            NewBlob(
                blob_id=blob_id,
                sha256="c" * 64,
                size_bytes=10,
                media_type="video/mp4",
                object_key=f"kernel-test/{blob_id}",
            )
        )
        uow.commit()

    with SqlAlchemyUnitOfWork(sessions) as uow:
        assert uow.blobs.mark_ready(blob_id, 1, now)
        uow.commit()

    with SqlAlchemyUnitOfWork(sessions) as uow:
        assert uow.gc_jobs.schedule(uuid4(), blob_id, now)
        assert not uow.gc_jobs.schedule(uuid4(), blob_id, now)
        uow.commit()

    with Session(engine) as session:
        count = session.scalar(select(func.count()).select_from(GcJobRow))
        assert count == 1

    snapshot = PostgresKernelMaintenanceReader(sessions).snapshot()
    assert snapshot.pending_gc_jobs == 1
    assert snapshot.pending_blobs == 0
    assert snapshot.dead_letter_gc_jobs == 0


def test_gc_worker_deletes_outside_transaction_and_completes_atomically(
    engine: Engine,
) -> None:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    blob_id = uuid4()
    object_key = f"kernel-test/{blob_id}"
    with SqlAlchemyUnitOfWork(sessions) as uow:
        uow.blobs.add_pending(
            NewBlob(
                blob_id=blob_id,
                sha256="d" * 64,
                size_bytes=3,
                media_type="image/png",
                object_key=object_key,
            )
        )
        uow.commit()
    with SqlAlchemyUnitOfWork(sessions) as uow:
        assert uow.blobs.mark_ready(blob_id, 1, now)
        assert uow.gc_jobs.schedule(uuid4(), blob_id, now)
        uow.commit()

    class BlobStore:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def put(self, object_key: str, body: object, media_type: str) -> None:
            raise NotImplementedError

        def head(self, object_key: str) -> BlobObjectHead:
            raise NotImplementedError

        def get_range(self, object_key: str, start: int, end_inclusive: int) -> bytes:
            raise NotImplementedError

        def delete(self, object_key: str) -> None:
            self.deleted.append(object_key)

    store = BlobStore()
    worker = GcWorker(
        uow_factory=lambda: SqlAlchemyUnitOfWork(sessions),
        blob_store=store,  # type: ignore[arg-type]
        worker_id="gc-test",
        now=lambda: now,
    )

    result = worker.run_once()

    assert result.claimed == result.deleted == 1
    assert result.failed == result.stale == 0
    assert store.deleted == [object_key]
    with Session(engine) as session:
        blob = session.get(BlobObjectRow, blob_id)
        job = session.scalar(select(GcJobRow).where(GcJobRow.blob_id == blob_id))
        assert blob is not None and blob.status == "deleted"
        assert job is not None and job.status == "completed"


def test_gc_worker_dead_letters_after_bounded_delete_failure(engine: Engine) -> None:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    blob_id = uuid4()
    with SqlAlchemyUnitOfWork(sessions) as uow:
        uow.blobs.add_pending(
            NewBlob(
                blob_id=blob_id,
                sha256="e" * 64,
                size_bytes=3,
                media_type="image/png",
                object_key=f"kernel-test/{blob_id}",
            )
        )
        uow.commit()
    with SqlAlchemyUnitOfWork(sessions) as uow:
        assert uow.blobs.mark_ready(blob_id, 1, now)
        assert uow.gc_jobs.schedule(uuid4(), blob_id, now)
        uow.commit()

    class FailingBlobStore:
        def delete(self, object_key: str) -> None:
            raise TimeoutError

    worker = GcWorker(
        uow_factory=lambda: SqlAlchemyUnitOfWork(sessions),
        blob_store=FailingBlobStore(),  # type: ignore[arg-type]
        worker_id="gc-test",
        max_attempts=1,
        now=lambda: now,
    )

    result = worker.run_once()

    assert result.claimed == result.failed == 1
    assert result.deleted == result.stale == 0
    with Session(engine) as session:
        blob = session.get(BlobObjectRow, blob_id)
        job = session.scalar(select(GcJobRow).where(GcJobRow.blob_id == blob_id))
        assert blob is not None and blob.status == "deleting"
        assert job is not None and job.status == "dead_letter"
        assert job.last_error_type == "TimeoutError"


def test_gc_worker_recovers_after_retry_delay(engine: Engine) -> None:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    current_time = [datetime.now(UTC)]
    blob_id = uuid4()
    with SqlAlchemyUnitOfWork(sessions) as uow:
        uow.blobs.add_pending(
            NewBlob(
                blob_id=blob_id,
                sha256="f" * 64,
                size_bytes=3,
                media_type="image/png",
                object_key=f"kernel-test/{blob_id}",
            )
        )
        uow.commit()
    with SqlAlchemyUnitOfWork(sessions) as uow:
        assert uow.blobs.mark_ready(blob_id, 1, current_time[0])
        assert uow.gc_jobs.schedule(uuid4(), blob_id, current_time[0])
        uow.commit()

    class FlakyBlobStore:
        def __init__(self) -> None:
            self.calls = 0

        def delete(self, object_key: str) -> None:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError

    store = FlakyBlobStore()
    worker = GcWorker(
        uow_factory=lambda: SqlAlchemyUnitOfWork(sessions),
        blob_store=store,  # type: ignore[arg-type]
        worker_id="gc-test",
        max_attempts=2,
        base_retry_delay=timedelta(seconds=2),
        now=lambda: current_time[0],
    )

    first = worker.run_once()
    current_time[0] += timedelta(seconds=1)
    too_early = worker.run_once()
    current_time[0] += timedelta(seconds=1)
    recovered = worker.run_once()

    assert first.failed == 1
    assert too_early.claimed == 0
    assert recovered.deleted == 1
    assert store.calls == 2
    with Session(engine) as session:
        blob = session.get(BlobObjectRow, blob_id)
        job = session.scalar(select(GcJobRow).where(GcJobRow.blob_id == blob_id))
        assert blob is not None and blob.status == "deleted"
        assert job is not None and job.status == "completed"
