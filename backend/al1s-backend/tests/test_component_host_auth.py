from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from al1s.components.host.app import create_host
from al1s.components.status_capability import StatusCapability
from tests.test_component_host_drivers import settings


def test_host_mutation_identity_and_operation_scoped_readonly_status_are_separate(tmp_path):
    config = settings(tmp_path)
    app = create_host(config)
    identity = uuid4()
    token = StatusCapability("x" * 32).issue(identity, datetime.now(UTC) + timedelta(minutes=45))
    with TestClient(app) as api:
        assert api.get("/inventory").status_code == 401
        assert api.post("/operations", json={"operation_id": str(identity)},
                        headers={"Authorization": "Bearer " + token}).status_code == 401
        status = api.get(f"/status/{identity}", headers={"Authorization": "Bearer " + token,
                                                        "Origin": "https://platform.local"})
        assert status.status_code == 200 and status.json()["state"] == "unknown"
        assert "payload" not in status.json()
        assert api.get(f"/status/{uuid4()}", headers={"Authorization": "Bearer " + token}).status_code == 401
        assert api.get(f"/status/{identity}", headers={"Authorization": "Bearer " + token,
                                                      "Origin": "https://other.local"}).status_code == 403
