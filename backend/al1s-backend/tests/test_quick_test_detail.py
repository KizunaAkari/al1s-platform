from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from al1s.api.maa import router
from al1s.app.admin_auth import install_admin_auth
from al1s.app.config import Settings
from al1s.maa.errors import MaaDomainError
from al1s.maa.quick_test_service import MaaQuickTestService
from al1s.maa.types import QuickTestSessionStatus


def fixture(status=QuickTestSessionStatus.ISSUED):
    session = SimpleNamespace(
        script_id=uuid4(), session_id=uuid4(), status=status,
        qualification_receipt_id=uuid4() if status is QuickTestSessionStatus.COMPLETED else None,
    )
    uow = SimpleNamespace(quick_tests=Mock(), qualifications=Mock())
    uow.quick_tests.get.return_value = session
    service = MaaQuickTestService(lambda: nullcontext(uow), Mock(), Mock())
    return service, uow, session


def test_pending_uses_one_read_and_no_mutation():
    service, uow, session = fixture()
    detail = service.get_detail(script_id=session.script_id, session_id=session.session_id)
    assert detail.session == session and detail.receipt is None
    uow.quick_tests.get.assert_called_once_with(session.session_id, for_update=False)
    uow.qualifications.get.assert_not_called()
    assert len(uow.quick_tests.mock_calls) == 1


def test_completed_fetches_exact_receipt():
    service, uow, session = fixture(QuickTestSessionStatus.COMPLETED)
    detail = service.get_detail(script_id=session.script_id, session_id=session.session_id)
    assert detail.receipt == uow.qualifications.get.return_value
    uow.qualifications.get.assert_called_once_with(session.qualification_receipt_id)


@pytest.mark.parametrize("missing", [False, True])
def test_wrong_script_or_missing_session_is_not_found(missing):
    service, uow, session = fixture()
    if missing:
        uow.quick_tests.get.return_value = None
    with pytest.raises(MaaDomainError):
        service.get_detail(script_id=uuid4(), session_id=session.session_id)
    uow.qualifications.get.assert_not_called()


def test_missing_receipt_does_not_report_success():
    service, uow, session = fixture(QuickTestSessionStatus.COMPLETED)
    uow.qualifications.get.return_value = None
    with pytest.raises(MaaDomainError):
        service.get_detail(script_id=session.script_id, session_id=session.session_id)


def test_result_query_requires_admin_and_never_returns_raw_diagnostics():
    from datetime import UTC, datetime

    app = FastAPI()
    settings = Settings(admin_password="test-admin-password", admin_cookie_secure=False)
    app.state.settings = settings
    install_admin_auth(app, settings)
    app.include_router(router, prefix="/api/v1/maa")
    script_id, session_id = uuid4(), uuid4()
    session = SimpleNamespace(
        session_id=session_id, script_version_id=uuid4(), status=QuickTestSessionStatus.COMPLETED,
        terminal_id=uuid4(), target_device_id=uuid4(), expires_at=datetime.now(UTC),
        completed_at=datetime.now(UTC), definition={"manifest": {}},
        started_at=None, cancel_requested_at=None,
    )
    app.state.maa_quick_tests = Mock()
    app.state.maa_quick_tests.get_detail.return_value = SimpleNamespace(
        session=session, receipt=SimpleNamespace(
            status="failed", error_code="recognition_failed",
            diagnostic={"failed_step": {"number": 2}, "private_payload": "not-for-browser"},
        ),
    )
    client = TestClient(app)
    url = f"/api/v1/maa/scripts/{script_id}/quick-tests/{session_id}"
    assert client.get(url).status_code == 401
    assert client.post(url + "/stop", headers={"X-AL1S-CSRF": "1"}).status_code == 401
    assert client.get(url + "/events").status_code == 401
    app.state.maa_quick_tests.get_detail.assert_not_called()
    assert client.post("/api/v1/auth/login", headers={"X-AL1S-CSRF": "1"},
                       json={"password": "test-admin-password"}).status_code == 200
    response = client.get(url)
    assert response.status_code == 200
    assert response.json()["failed_step_number"] == 2
    assert "private_payload" not in response.text
    assert "definition" not in response.json()
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["failure_detail"] is None
    session.definition["manifest"].update({"debug_step_number": 7, "definitions": {"entry": {
        "script_name": "开始脚本", "steps": [{}], "independent_rules": [{"name": "通知"}],
    }}})
    app.state.maa_quick_tests.get_detail.return_value.receipt.diagnostic.update({
        "definition_key": "entry", "recognition_failure": {
            "step_index": 0, "rule_index": 0, "stage": "click_target",
            "algorithm": "TemplateMatch", "best_score": 0.84406, "threshold": 0.85,
            "consecutive_misses": 9, "raw": "not-for-browser",
        },
    })
    projected = client.get(url)
    assert projected.json()["failure_detail"]["step_number"] == 7
    assert projected.json()["failure_detail"]["rule_name"] == "通知"
    assert projected.json()["failed_step_number"] == 7
    assert "not-for-browser" not in projected.text
    app.state.maa_quick_tests.request_cancel.return_value = session
    stopped = client.post(url + "/stop", headers={"X-AL1S-CSRF": "1"})
    assert stopped.status_code == 200
    app.state.maa_quick_tests.request_cancel.assert_called_once_with(
        script_id=script_id, session_id=session_id
    )
    app.state.maa_quick_tests.list_events.return_value = [SimpleNamespace(
        sequence=1, kind="started", step_number=None, code=None,
        created_at=datetime.now(UTC),
    )]
    events = client.get(url + "/events")
    assert events.status_code == 200
    assert events.json()["items"][0]["kind"] == "started"
    app.state.maa_quick_tests.list_events.return_value = [SimpleNamespace(
        sequence=2, kind="log", step_number=None, code="maa_rule_started:entry:0",
        rule_name="通知", created_at=datetime.now(UTC),
    )]
    rule_events = client.get(url + "/events")
    assert rule_events.json()["items"][0]["step_number"] is None
    assert rule_events.json()["items"][0]["rule_name"] == "通知"
    assert "definition" not in rule_events.text
