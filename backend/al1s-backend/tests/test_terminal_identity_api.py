from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from al1s.api.terminal_identity import router
from al1s.execution.types import TerminalAcceptanceStatus


def test_identity_only_returns_authenticated_subject_and_current_acceptance():
    terminal = SimpleNamespace(terminal_id=uuid4(), row_version=8,
                               acceptance_status=TerminalAcceptanceStatus.DRAINING)
    app = FastAPI()
    app.include_router(router)
    seen = []
    def authenticate(token):
        seen.append(token)
        return terminal
    app.state.execution_resources = SimpleNamespace(authenticate_terminal=authenticate)
    response = TestClient(app).get("/terminal/identity", headers={"Authorization": "Bearer test-only"})
    assert response.json() == {"terminal_id": str(terminal.terminal_id), "row_version": 8,
                               "acceptance_status": "draining"}
    assert seen == ["test-only"]
