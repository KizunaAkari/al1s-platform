from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.models import StorageGcRequestRow
from al1s.adapters.postgres.repositories import PostgresKernelMaintenanceReader
from al1s.adapters.postgres.storage_gc_repository import PostgresStorageGcRepository
from al1s.adapters.postgres.unit_of_work import SqlAlchemyUnitOfWork
from al1s.kernel.types import GcBatchResult, NewBlob

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    url = os.getenv("AL1S_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AL1S_TEST_DATABASE_URL is required")
    database_engine = create_engine(url, pool_pre_ping=True)
    yield database_engine
    database_engine.dispose()


@pytest.fixture(autouse=True)
def clean(engine: Engine) -> Iterator[None]:
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE storage_gc_requests, gc_jobs, blob_objects CASCADE"))
    yield
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE storage_gc_requests, gc_jobs, blob_objects CASCADE"))


def test_manual_request_is_deduplicated_and_fenced(engine: Engine) -> None:
    repository = PostgresStorageGcRepository(sessionmaker(bind=engine, expire_on_commit=False))
    first = repository.submit()
    assert repository.submit().id == first.id
    claim = repository.claim(datetime.now(UTC))
    assert claim is not None and claim.id == first.id
    assert repository.submit().id == first.id
    result = GcBatchResult(claimed=2, deleted=1, failed=1, stale=0)
    assert repository.settle(claim, datetime.now(UTC), result)
    assert not repository.settle(claim, datetime.now(UTC), result)
    latest = repository.latest()
    assert latest and latest.status == "completed"
    assert (latest.claimed, latest.deleted, latest.failed) == (2, 1, 1)
    assert repository.submit().id != first.id


def test_concurrent_submissions_share_one_active_request(engine: Engine) -> None:
    repository = PostgresStorageGcRepository(sessionmaker(bind=engine, expire_on_commit=False))
    barrier = Barrier(2)

    def submit() -> object:
        barrier.wait()
        return repository.submit().id

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(submit)
        second = pool.submit(submit)
        assert first.result(timeout=10) == second.result(timeout=10)


def test_expired_claim_recovers_but_old_worker_cannot_settle(engine: Engine) -> None:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    repository = PostgresStorageGcRepository(sessions)
    record = repository.submit()
    now = datetime.now(UTC)
    first = repository.claim(now)
    assert first and first.id == record.id
    second = repository.claim(now + timedelta(minutes=31))
    assert second and second.attempt_count == 2
    assert not repository.settle(first, now + timedelta(minutes=31), GcBatchResult(0, 0, 0, 0))
    assert repository.settle(second, now + timedelta(minutes=31), GcBatchResult(0, 0, 0, 0))


def test_reclaimable_estimate_excludes_future_jobs(engine: Engine) -> None:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    with SqlAlchemyUnitOfWork(sessions) as uow:
        for size, available in ((7, now - timedelta(minutes=1)), (11, now + timedelta(days=1))):
            blob_id = uuid4()
            uow.blobs.add_pending(
                NewBlob(blob_id, uuid4().hex * 2, size, "image/png", f"gc/{blob_id}")
            )
            assert uow.gc_jobs.schedule(uuid4(), blob_id, available)
        uow.commit()
    snapshot = PostgresKernelMaintenanceReader(sessions).snapshot()
    assert (snapshot.reclaimable_blobs, snapshot.reclaimable_bytes) == (1, 7)
    with sessions() as session:
        assert session.scalar(select(StorageGcRequestRow.id).limit(1)) is None
