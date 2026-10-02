import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from al1s.api.system import router
from al1s.app.modules import platform_modules


def test_information_routes_are_mounted_without_starting_dependencies() -> None:
    from al1s.api.router import api_router

    app = FastAPI()
    app.include_router(api_router)
    paths = app.openapi()["paths"]
    for prefix in ("/api/v1/notifications/", "/api/v1/bots/"):
        assert any(path.startswith(prefix) for path in paths), prefix
    assert "/api/v1/system/modules" in paths


def test_duplicate_registration_rejected() -> None:
    registry = platform_modules()
    with pytest.raises(ValueError, match='Duplicate module'):
        registry.register(registry.entries()[0])


def test_module_contract() -> None:
    app = FastAPI()
    app.state.modules = platform_modules()
    app.include_router(router)
    response = TestClient(app).get('/modules')
    assert response.status_code == 200
    rows = response.json()
    assert [row['id'] for row in rows] == ['platform', 'maa', 'information', 'lineup']
    assert [row['ui_complete'] for row in rows] == [False, False, False, True]
    assert rows[1]['data_owner'] == 'maa'
    assert '/notifications' in rows[2]['routes']
