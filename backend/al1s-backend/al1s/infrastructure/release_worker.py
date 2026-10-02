"""One large-file verifier, isolated from API, task scheduling and notification pools."""

import signal
from threading import Event

import structlog

from al1s.adapters.postgres.release_repository import ReleaseRepository
from al1s.adapters.s3.blob_store import S3BlobStore
from al1s.app.config import get_settings
from al1s.app.logging import configure_logging
from al1s.infrastructure.database import create_database_engine, create_session_factory
from al1s.releases.service import ReleaseService


def main() -> None:
    configure_logging()
    stop = Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    settings = get_settings().model_copy(
        update={"database_pool_size": 1, "database_max_overflow": 0}
    )
    engine = create_database_engine(settings)
    objects = S3BlobStore(settings)
    service = ReleaseService(ReleaseRepository(create_session_factory(engine)), objects)
    try:
        while not stop.is_set():
            try:
                service.verify_once()
                service.cleanup_once()
            except Exception as exc:
                structlog.get_logger().error(
                    "release_verification_failed", error=type(exc).__name__
                )
            stop.wait(5)
    finally:
        objects.close()
        engine.dispose()


if __name__ == "__main__":
    main()
