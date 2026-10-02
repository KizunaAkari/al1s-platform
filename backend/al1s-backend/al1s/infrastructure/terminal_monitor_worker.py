"""Bounded host probes run outside the Maa scheduling process."""

import signal
from collections.abc import Callable
from threading import Event
from typing import cast

import structlog

from al1s.adapters.postgres.execution_unit_of_work import ExecutionSqlAlchemyUnitOfWork
from al1s.adapters.postgres.maintenance_repository import MaintenanceRepository
from al1s.app.config import get_settings
from al1s.app.logging import configure_logging
from al1s.execution.maintenance_service import MaintenanceService
from al1s.execution.ports import ExecutionUnitOfWork
from al1s.execution.terminal_health import TerminalHealthMonitor
from al1s.infrastructure.database import create_database_engine, create_session_factory


def main() -> None:
    configure_logging()
    settings = get_settings().model_copy(
        update={"database_pool_size": 2, "database_max_overflow": 0}
    )
    stop = Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    health = TerminalHealthMonitor(
        cast(Callable[[], ExecutionUnitOfWork], lambda: ExecutionSqlAlchemyUnitOfWork(sessions)),
        timeout_seconds=settings.terminal_heartbeat_timeout_seconds,
    )
    maintenance = MaintenanceService(MaintenanceRepository(sessions), settings.host_managers)
    logger = structlog.get_logger()
    try:
        while not stop.is_set():
            for name, operation in (
                ("heartbeat", health.check_once),
                ("maintenance", maintenance.poll_once),
            ):
                try:
                    operation()
                except Exception as exc:
                    logger.error(
                        "terminal_monitor_failed", operation=name, error=type(exc).__name__
                    )
            stop.wait(5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
