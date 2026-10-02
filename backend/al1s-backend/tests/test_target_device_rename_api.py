from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from al1s.api.target_devices import router
from al1s.execution.types import TargetDeviceMode, TargetDeviceRecord


def test_logical_phone_rename_route_passes_version_and_returns_identity() -> None:
    device_id = uuid4()
    request_id = uuid4()
    now = datetime.now(UTC)
    service = Mock()
    service.rename_target_device.return_value = TargetDeviceRecord(
        device_id=device_id,
        display_name="Desk phone",
        platform="android",
        mode=TargetDeviceMode.MOUNTED,
        managing_terminal_id=uuid4(),
        row_version=3,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    app = FastAPI()
    app.state.execution_resources = service

    @app.middleware("http")
    async def request_identity(request: Request, call_next):
        request.state.request_id = str(request_id)
        return await call_next(request)

    app.include_router(router, prefix="/api/v1/target-devices")
    client = TestClient(app)
    response = client.patch(
        f"/api/v1/target-devices/{device_id}/display-name",
        json={"expected_version": 2, "display_name": "  Desk phone  "},
    )
    assert response.status_code == 200
    assert response.json()["device_id"] == str(device_id)
    assert response.json()["display_name"] == "Desk phone"
    assert response.json()["mode"] == "mounted"
    service.rename_target_device.assert_called_once_with(
        device_id=device_id,
        expected_version=2,
        display_name="  Desk phone  ",
        correlation_id=request_id,
    )
    invalid = client.patch(
        f"/api/v1/target-devices/{device_id}/display-name",
        json={"expected_version": 0, "display_name": "Desk phone"},
    )
    assert invalid.status_code == 422
