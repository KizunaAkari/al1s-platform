import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI

from al1s.app import factory
from al1s.app.config import Settings
from al1s.app.service_state import (
    PlatformServiceState,
    install_service_state,
    validate_service_state,
)


def test_initialization_failure_closes_owned_engine_and_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock()
    readiness = MagicMock()
    monkeypatch.setattr(factory, "create_database_engine", lambda _settings: engine)
    monkeypatch.setattr(factory, "create_session_factory", lambda _engine: MagicMock())
    monkeypatch.setattr(factory, "build_readiness_service", lambda *_args, **_kwargs: readiness)

    def fail_blob_store(_settings: Settings) -> None:
        raise OSError("simulated object storage initialization failure")

    monkeypatch.setattr(factory, "S3BlobStore", fail_blob_store)
    app = factory.create_app(Settings())

    async def start() -> None:
        async with app.router.lifespan_context(app):
            pytest.fail("lifespan unexpectedly started")

    with pytest.raises(OSError, match="simulated object storage"):
        asyncio.run(start())
    readiness.close.assert_called_once_with()
    engine.dispose.assert_called_once_with()


def test_service_state_validation_names_missing_dependency() -> None:
    with pytest.raises(RuntimeError, match="settings"):
        validate_service_state(FastAPI())


def test_partial_typed_assembly_is_not_published() -> None:
    app = FastAPI()
    with pytest.raises(RuntimeError, match="settings"):
        install_service_state(app, PlatformServiceState())
    assert not hasattr(app.state, "services")
