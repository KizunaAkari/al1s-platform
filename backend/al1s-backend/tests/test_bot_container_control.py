from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from al1s.bots.container_control import ContainerCommand, ControlError, DockerControl
from al1s.bots.control_client import ControlClient
from al1s.bots.control_proxy import create_proxy


def inspect(running=True, started="2026-09-15T01:00:00Z"):
    return {
        "Id": "a" * 64, "Config": {"Env": ["TOKEN=must-not-leak"]},
        "State": {"Running": running, "StartedAt": started, "FinishedAt": "",
                  "Status": "running" if running else "exited", "Health": {"Status": "healthy"}},
    }


@pytest.fixture
def control():
    value = DockerControl({"qq": "al1s-llbot"}, "/unused-test-socket")
    value._request = Mock(return_value=inspect())
    return value


def test_only_allowlisted_aliases_and_fields(control):
    state = control.state("qq")
    assert "must-not-leak" not in state.model_dump_json()
    with pytest.raises(ControlError, match="not_registered"):
        control.state("discord")
    with pytest.raises(ValueError):
        DockerControl({"qq": "../../other"}, "/unused")
    with pytest.raises(ValueError):
        DockerControl({"platform": "al1s-platform"}, "/unused")


def test_stale_revision_never_mutates(control):
    with pytest.raises(ControlError, match="state_changed"):
        control.command("qq", ContainerCommand(action="restart", revision="0" * 64))
    assert all(call.args[0] == "GET" for call in control._request.call_args_list)


def test_restart_requires_new_start_evidence(control):
    revision = control.state("qq").revision
    with pytest.raises(ControlError, match="restart_unconfirmed"):
        control.command("qq", ContainerCommand(action="restart", revision=revision))


def test_restart_uses_instance_id_and_old_revision_cannot_repeat(control):
    revision = control.state("qq").revision
    control._request.side_effect = [inspect(), {}, inspect(started="new")]
    state = control.command("qq", ContainerCommand(action="restart", revision=revision))
    assert state.revision != revision
    assert control._request.call_args_list[-2].args == ("POST", "a" * 64, "restart?t=10")
    control._request.side_effect = None
    control._request.return_value = inspect(started="new")
    with pytest.raises(ControlError, match="state_changed"):
        control.command("qq", ContainerCommand(action="restart", revision=revision))


def test_busy_target_never_mutates(control):
    control.locks["qq"].acquire()
    try:
        with pytest.raises(ControlError, match="in_progress"):
            control.command("qq", ContainerCommand(action="stop", revision="0" * 64))
        control._request.assert_not_called()
    finally:
        control.locks["qq"].release()


def test_start_already_running_is_noop(control):
    revision = control.state("qq").revision
    control.command("qq", ContainerCommand(action="start", revision=revision))
    assert all(call.args[0] == "GET" for call in control._request.call_args_list)


def test_proxy_auth_alias_and_action_validation(control):
    client = TestClient(create_proxy("x" * 32, control))
    assert client.get("/containers/qq").status_code == 401
    headers = {"Authorization": "Bearer " + "x" * 32}
    assert client.get("/containers/platform", headers=headers).status_code == 422
    assert client.post("/containers/qq", headers=headers,
                       json={"action": "exec", "revision": "0" * 64}).status_code == 422
    assert client.post("/containers/qq", headers=headers,
                       json={"action": "stop", "revision": "0" * 64, "command": "ls"}
                       ).status_code == 422
    assert client.get("/containers/qq", headers=headers).status_code == 200


@pytest.mark.parametrize("url", ["", "file:///tmp/a", "http://user:pass@localhost",
                                  "http://localhost/?token=x"])
def test_client_rejects_invalid_configuration(url):
    with pytest.raises(ControlError, match="not_configured"):
        ControlClient(url, "x" * 32)
