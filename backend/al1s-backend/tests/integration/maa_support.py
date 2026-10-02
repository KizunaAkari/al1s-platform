"""Fixtures and in-memory store for isolated Maa integration tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import BinaryIO, cast
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.maa_models import (
    MaaScriptRow,
    MaaScriptVersionRow,
)
from al1s.adapters.postgres.maa_unit_of_work import MaaSqlAlchemyUnitOfWork
from al1s.kernel.types import BlobObjectHead
from al1s.maa.catalog_service import MaaCatalogQueryService
from al1s.maa.definition_provider import MaaExecutionDefinitionProvider
from al1s.maa.import_command_service import MaaArchiveImportCommandService
from al1s.maa.import_service import MaaArchiveImportService
from al1s.maa.mutation_service import MaaMutationService
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.publication_service import MaaScriptPublicationService
from al1s.maa.quick_test_service import MaaQuickTestService
from al1s.maa.strategy_service import MaaStrategyService
from al1s.maa.types import (
    ApplicationRecord,
    ScriptRecord,
    ScriptStatus,
    ScriptType,
)
from tests.maa_archive_fixture import FixtureScript, build_archive


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
def clean_maa_tables(engine: Engine) -> Iterator[None]:
    table_names = (
        "plan_occurrences, task_schedules, task_requests, "
        "maa_strategy_modules, maa_strategy_versions, maa_strategies, "
        "maa_import_items, maa_import_batches, maa_quick_test_sessions, "
        "maa_mutation_receipts, "
        "maa_script_qualification_receipts, "
        "maa_script_version_blobs, "
        "maa_scripts, maa_script_versions, maa_applications, gc_jobs, "
        "blob_objects, audit_logs, outbox_events, target_devices, terminals"
    )
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {table_names} CASCADE"))
    yield
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {table_names} CASCADE"))


class MemoryBlobStore:
    def __init__(self, *, fail_first_put: bool = False) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.put_count = 0
        self._fail_first_put = fail_first_put

    def put(self, object_key: str, body: bytes | BinaryIO, media_type: str) -> None:
        self.put_count += 1
        if self._fail_first_put:
            self._fail_first_put = False
            raise OSError("simulated object-store outage")
        content = body if isinstance(body, bytes) else body.read()
        self.objects[object_key] = (content, media_type)

    def head(self, object_key: str) -> BlobObjectHead:
        body, media_type = self.objects[object_key]
        return BlobObjectHead(size_bytes=len(body), media_type=media_type, etag=None)

    def get_range(self, object_key: str, start: int, end_inclusive: int) -> bytes:
        return self.objects[object_key][0][start : end_inclusive + 1]

    def delete(self, object_key: str) -> None:
        self.objects.pop(object_key, None)


def _service(engine: Engine, store: MemoryBlobStore) -> MaaArchiveImportService:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def factory() -> MaaUnitOfWork:
        return cast(MaaUnitOfWork, MaaSqlAlchemyUnitOfWork(sessions))

    return MaaArchiveImportService(factory, store)


def _import_commands(engine: Engine, store: MemoryBlobStore) -> MaaArchiveImportCommandService:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def factory() -> MaaUnitOfWork:
        return cast(MaaUnitOfWork, MaaSqlAlchemyUnitOfWork(sessions))

    importer = MaaArchiveImportService(factory, store)
    return MaaArchiveImportCommandService(factory, store, importer)


def _publication_service(engine: Engine) -> MaaScriptPublicationService:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def factory() -> MaaUnitOfWork:
        return cast(MaaUnitOfWork, MaaSqlAlchemyUnitOfWork(sessions))

    return MaaScriptPublicationService(factory)


def _stage_imports_for_editing(engine: Engine) -> None:
    """Publication tests explicitly edit imported executable assets into candidates."""
    with Session(engine) as session:
        rows = session.execute(
            select(MaaScriptRow, MaaScriptVersionRow).join(
                MaaScriptVersionRow, MaaScriptVersionRow.id == MaaScriptRow.current_version_id
            )
        ).all()
        inputs = [(script.id, script.row_version, version.manifest) for script, version in rows]
    publication = _publication_service(engine)
    for script_id, row_version, manifest in inputs:
        publication.save_candidate(
            script_id=script_id,
            expected_script_version=row_version,
            manifest=manifest,
            correlation_id=uuid4(),
        )


def _strategy_service(engine: Engine) -> MaaStrategyService:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def factory() -> MaaUnitOfWork:
        return cast(MaaUnitOfWork, MaaSqlAlchemyUnitOfWork(sessions))

    return MaaStrategyService(factory)


def _definition_provider(engine: Engine) -> MaaExecutionDefinitionProvider:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def factory() -> MaaUnitOfWork:
        return cast(MaaUnitOfWork, MaaSqlAlchemyUnitOfWork(sessions))

    return MaaExecutionDefinitionProvider(factory)


def _quick_test_service(engine: Engine) -> MaaQuickTestService:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def factory() -> MaaUnitOfWork:
        return cast(MaaUnitOfWork, MaaSqlAlchemyUnitOfWork(sessions))

    return MaaQuickTestService(
        factory,
        MaaExecutionDefinitionProvider(factory),
        MaaScriptPublicationService(factory),
    )


def _catalog_service(engine: Engine) -> MaaCatalogQueryService:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def factory() -> MaaUnitOfWork:
        return cast(MaaUnitOfWork, MaaSqlAlchemyUnitOfWork(sessions))

    return MaaCatalogQueryService(factory)


def _mutation_service(engine: Engine) -> MaaMutationService:
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def factory() -> MaaUnitOfWork:
        return cast(MaaUnitOfWork, MaaSqlAlchemyUnitOfWork(sessions))

    return MaaMutationService(factory)


def setup_import(engine, kind="module_process", name="Saved"):
    def factory():
        return MaaSqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))

    now = datetime.now(UTC)
    app = ApplicationRecord(uuid4(), "com.example.game", "Game", now, now, None, 1)
    target = ScriptRecord(
        uuid4(),
        app.application_id,
        name,
        name.casefold(),
        ScriptType(kind),
        ScriptStatus.VALIDATION_PENDING,
        None,
        None,
        now,
        now,
        None,
        1,
    )
    with factory() as uow:
        uow.applications.add_many([app])
        uow.flush()
        uow.scripts.add_many([target])
        uow.commit()
    return MaaArchiveImportService(factory, MemoryBlobStore()), app, target


def bundle(name="Saved", kind="module_process", seconds=1):
    document = {
        "version": 2,
        "script_type": kind,
        "target": {"application_package": "com.example.game"},
        "steps": [{"action": "wait", "seconds": seconds}],
    }
    if kind == "module_start":
        document["steps"].insert(0, {"action": "start"})
        document["steps"].insert(1, {"action": "launch_app", "package": "com.example.game"})
    if kind == "module_end":
        document["cleanup_on_finish"] = True
    return build_archive(FixtureScript(name, document))
