"""Real cancellation, rollback and pool reuse in an explicitly disposable database."""

from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, TimeoutError
from sqlalchemy.orm import Session

from al1s.adapters.postgres.maa_models import MaaApplicationRow
from al1s.adapters.postgres.maa_unit_of_work import MaaSqlAlchemyUnitOfWork
from al1s.app.config import Settings
from al1s.infrastructure.database import (
    create_batch_session_factory,
    create_database_engine,
    create_session_factory,
)
from al1s.maa.mutation_service import MaaMutationService, MutationOutcome
from tests.integration.maa_support import clean_maa_tables as clean_maa_tables
from tests.integration.maa_support import engine as engine

pytestmark = pytest.mark.integration


@pytest.fixture
def budget_engine(engine):
    settings = Settings(
        _env_file=None,
        database_url=engine.url.render_as_string(hide_password=False),
        database_pool_size=1,
        database_max_overflow=0,
        database_pool_timeout_seconds=0.05,
        database_statement_timeout_ms=200,
        database_lock_timeout_ms=50,
        database_batch_statement_timeout_ms=400,
    )
    bounded = create_database_engine(settings)
    yield bounded, settings
    bounded.dispose()


def test_pool_exhaustion_exits_and_recovers(budget_engine):
    bounded, _ = budget_engine
    with bounded.connect():
        started = monotonic()
        with pytest.raises(TimeoutError):
            bounded.connect()
        assert 0.04 <= monotonic() - started < 1
    with bounded.connect() as connection:
        assert connection.scalar(select(1)) == 1
    assert bounded.pool.checkedout() == 0


def test_statement_timeout_rolls_back_mutation_and_receipt_then_same_key_can_succeed(budget_engine):
    bounded, _ = budget_engine
    sessions = create_session_factory(bounded)
    service = MaaMutationService(lambda: MaaSqlAlchemyUnitOfWork(sessions))
    application_id = uuid4()
    slow = True

    def mutate(uow):
        session = uow._require_session()
        session.add(
            MaaApplicationRow(
                id=application_id,
                package_name="com.test.budget",
                display_name="Budget",
                row_version=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        session.flush()
        if slow:
            session.execute(text("SELECT pg_sleep(1)"))
        return MutationOutcome("maa_application", application_id, 200, {"id": str(application_id)})

    with pytest.raises(DBAPIError) as failure:
        service.execute(
            operation="budget-test", idempotency_key="original", request_payload={}, mutate=mutate
        )
    assert failure.value.orig.sqlstate == "57014"
    with sessions() as session:
        assert session.get(MaaApplicationRow, application_id) is None
    slow = False
    first = service.execute(
        operation="budget-test", idempotency_key="original", request_payload={}, mutate=mutate
    )
    replay = service.execute(
        operation="budget-test", idempotency_key="original", request_payload={}, mutate=mutate
    )
    assert not first.replayed and replay.replayed
    assert first.receipt.receipt_id == replay.receipt.receipt_id
    assert bounded.pool.checkedout() == 0


def test_lock_timeout_rolls_back_and_connection_remains_usable(engine, budget_engine):
    bounded, _ = budget_engine
    application_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            MaaApplicationRow(
                id=application_id,
                package_name="com.test.lock",
                display_name="Original",
                row_version=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    with Session(engine) as holder, holder.begin():
        holder.scalar(
            select(MaaApplicationRow.id)
            .where(MaaApplicationRow.id == application_id)
            .with_for_update()
        )
        with Session(bounded) as writer, pytest.raises(DBAPIError) as failure:
            writer.execute(
                update(MaaApplicationRow)
                .where(MaaApplicationRow.id == application_id)
                .values(display_name="Uncommitted")
            )
        assert failure.value.orig.sqlstate == "55P03"
    with Session(bounded) as writer, writer.begin():
        assert writer.get(MaaApplicationRow, application_id).display_name == "Original"
        writer.execute(
            update(MaaApplicationRow)
            .where(MaaApplicationRow.id == application_id)
            .values(display_name="Recovered")
        )
    assert bounded.pool.checkedout() == 0


@pytest.mark.parametrize("commit", [True, False])
def test_archive_budget_does_not_leak_on_shared_connection(budget_engine, commit):
    bounded, settings = budget_engine
    batches = create_batch_session_factory(bounded, settings)
    with batches() as session:
        assert session.scalar(text("SHOW statement_timeout")) == "400ms"
        session.execute(text("SELECT pg_sleep(0.25)"))
        (session.commit if commit else session.rollback)()
    with create_session_factory(bounded)() as session:
        assert session.scalar(text("SHOW statement_timeout")) == "200ms"
        assert session.scalar(text("SHOW lock_timeout")) == "50ms"
        assert session.scalar(text("SHOW idle_in_transaction_session_timeout")) == "15s"
    assert bounded.pool.checkedout() == 0
