"""Task clocks must advance even when MQTT or notification transports are down."""

import signal
from datetime import UTC, datetime
from threading import Event
from typing import cast
from uuid import uuid4

import structlog
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.lineup_repository import LineupRepository
from al1s.adapters.postgres.maa_unit_of_work import MaaSqlAlchemyUnitOfWork
from al1s.adapters.postgres.scheduling_unit_of_work import SchedulingSqlAlchemyUnitOfWork
from al1s.app.config import get_settings
from al1s.app.logging import configure_logging
from al1s.execution.definitions import ExecutionDefinitionRegistry
from al1s.execution.scheduling_service import ExecutionSchedulingService, SchedulingUowFactory
from al1s.execution.timeout_service import ExecutionTimeoutService
from al1s.infrastructure.database import create_database_engine, create_session_factory
from al1s.infrastructure.execution_worker import ExecutionRuntimeWorker
from al1s.lineup.batch_definition import LineupBatchDefinitionProvider
from al1s.lineup.definition import LineupDefinitionProvider
from al1s.maa.definition_provider import MaaExecutionDefinitionProvider, MaaUowFactory


def build_runtime_worker(sessions: sessionmaker[Session]) -> ExecutionRuntimeWorker:
    uow = cast(SchedulingUowFactory, lambda: SchedulingSqlAlchemyUnitOfWork(sessions))
    definitions = ExecutionDefinitionRegistry()
    definitions.register("lineup", LineupDefinitionProvider(LineupRepository(sessions)))
    definitions.register("lineup_batch", LineupBatchDefinitionProvider(LineupRepository(sessions)))
    definitions.register(
        "maa",
        MaaExecutionDefinitionProvider(
            cast(MaaUowFactory, lambda: MaaSqlAlchemyUnitOfWork(sessions)),
        ),
    )
    return ExecutionRuntimeWorker(
        ExecutionSchedulingService(uow, definitions),
        ExecutionTimeoutService(uow, now=lambda: datetime.now(UTC)),
        worker_id=uuid4(),
    )


def main() -> None:
    configure_logging()
    settings = get_settings()
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    worker = build_runtime_worker(sessions)
    try:
        while not stop.is_set():
            try:
                result = worker.run_once()
                if result.changed:
                    structlog.get_logger().info("scheduler_cycle", changed=result.changed)
            except Exception as exc:
                structlog.get_logger().error("scheduler_retry", error_type=type(exc).__name__)
            stop.wait(settings.worker_interval_seconds)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
