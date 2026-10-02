from sqlalchemy import Connection, Engine, create_engine, event, func, select, text
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker

from al1s.app.config import Settings


def create_database_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        connect_args={
            "connect_timeout": max(1, int(settings.probe_timeout_seconds)),
            "options": (
                f"-c statement_timeout={settings.database_statement_timeout_ms} "
                f"-c lock_timeout={settings.database_lock_timeout_ms} "
                f"-c idle_in_transaction_session_timeout="
                f"{settings.database_idle_transaction_timeout_ms}"
            ),
        },
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_batch_session_factory(engine: Engine, settings: Settings) -> sessionmaker[Session]:
    """Long archive statements share the pool; transaction-local settings cannot leak."""
    factory = create_session_factory(engine)

    @event.listens_for(factory, "after_begin")
    def apply_budget(
        session: Session, transaction: SessionTransaction, connection: Connection
    ) -> None:
        if not transaction.nested:
            connection.execute(
                select(
                    func.set_config(
                        "statement_timeout", str(settings.database_batch_statement_timeout_ms), True
                    ),
                    func.set_config(
                        "idle_in_transaction_session_timeout",
                        str(max(60000, settings.database_idle_transaction_timeout_ms)),
                        True,
                    ),
                )
            )

    return factory


class DatabaseProbe:
    def __init__(self, engine: Engine, *, owns_engine: bool = False) -> None:
        self._engine = engine
        self._owns_engine = owns_engine

    def check(self) -> None:
        with self._engine.connect() as connection:
            value = connection.execute(text("SELECT 1")).scalar_one()
        if value != 1:
            raise RuntimeError("unexpected database probe result")

    def close(self) -> None:
        if self._owns_engine:
            self._engine.dispose()
