"""Independent bounded retention/GC loop; S3 calls never block task scheduling."""

import signal
from datetime import UTC, datetime
from threading import Event
from typing import cast

import structlog

from al1s.adapters.postgres import execution_models as execution_models
from al1s.adapters.postgres.artifact_staging_cleanup import PostgresArtifactStagingCleanup
from al1s.adapters.postgres.failure_review import PostgresFailureReview
from al1s.adapters.postgres.quick_test_media import PostgresQuickTestMedia
from al1s.adapters.postgres.storage_gc_repository import PostgresStorageGcRepository
from al1s.adapters.postgres.technical_retention import MODULE_OWNERS, PostgresTechnicalRetention
from al1s.adapters.postgres.unit_of_work import SqlAlchemyUnitOfWork
from al1s.adapters.s3.blob_store import S3BlobStore
from al1s.app.config import get_settings
from al1s.app.logging import configure_logging
from al1s.infrastructure.database import create_database_engine, create_session_factory
from al1s.kernel.gc_worker import GcWorker
from al1s.kernel.ports import UnitOfWork
from al1s.maa.artifact_staging_cleanup import ArtifactStagingCleanup


def main() -> None:
    configure_logging()
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    settings = get_settings()
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    store = S3BlobStore(settings)
    review = PostgresFailureReview(sessions)
    worker = GcWorker(
        uow_factory=lambda: cast(UnitOfWork, SqlAlchemyUnitOfWork(sessions)),
        blob_store=store,
        worker_id="media-gc",
        batch_size=20,
    )
    manual_gc = PostgresStorageGcRepository(sessions)
    staging_cleanup = ArtifactStagingCleanup(PostgresArtifactStagingCleanup(sessions), store)
    try:
        while not stop.is_set():
            try:
                review.expire(datetime.now(UTC))
                PostgresQuickTestMedia(sessions).expire(datetime.now(UTC))
                manual_request = manual_gc.claim(datetime.now(UTC))
                if manual_request is not None:
                    try:
                        result = worker.run_once()
                    except Exception as exc:
                        manual_gc.settle(
                            manual_request, datetime.now(UTC), None, type(exc).__name__,
                        )
                        raise
                    manual_gc.settle(manual_request, datetime.now(UTC), result)
                else:
                    worker.run_once()
            except Exception as exc:
                structlog.get_logger().error("media_cleanup_failed", error_type=type(exc).__name__)
            try:
                staging_cleanup.run_once()
            except Exception as exc:
                structlog.get_logger().error(
                    "artifact_staging_cleanup_failed", error_type=type(exc).__name__
                )
            # Separate transaction/failure boundary: a storage outage must not
            # prevent bounded technical-detail cleanup (or vice versa).
            try:
                with sessions.begin() as session:
                    for owner in MODULE_OWNERS:
                        PostgresTechnicalRetention(session, owner_module=owner).redact(
                            datetime.now(UTC),
                        )
            except Exception as exc:
                structlog.get_logger().error(
                    "technical_cleanup_failed", error_type=type(exc).__name__,
                )
            stop.wait(30)
    finally:
        store.close()
        engine.dispose()


if __name__ == "__main__":
    main()
