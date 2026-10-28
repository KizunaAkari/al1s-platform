import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from al1s.bots.qq_headed import pmhq_process
from al1s.bots.qq_headed.health import parse_health
from al1s.bots.qq_headed.pmhq_process import wait_ready
from al1s.bots.qq_headed.startup import prepare_pmhq_config


def test_preparation_preserves_direct_files(tmp_path: Path):
    (tmp_path / "machine_guid.bin").write_bytes(b"x" * 16)
    (tmp_path / ".al1s-qq-identity.json").write_bytes(b"pending")
    path = prepare_pmhq_config(tmp_path, "123456", "secret")
    config = json.loads(path.read_text())
    assert config["default_host"] == "127.0.0.1"
    assert config["auth_token"] == "secret"
    assert not config["headless"]
    assert (tmp_path / ".al1s-qq-identity.json").read_bytes() == b"pending"
    assert (tmp_path / "machine_guid.bin").read_bytes() == b"x" * 16
    assert prepare_pmhq_config(tmp_path, "123456", "secret") == path


def test_health_separates_ready_from_online():
    assert parse_health({"qq_full_version": "3.2.25", "uin": "", "uid": ""}).ready
    assert not parse_health({"qq_full_version": "3.2.25"}).online
    assert parse_health({"qq_full_version": "3.2.25", "uin": 123456,
                         "uid": "u_abc"}).account == "123456"
    with pytest.raises(ValueError):
        parse_health({"uin": "unexpected"})


def test_health_rejects_active_client_version_different_from_frozen_image():
    with pytest.raises(ValueError, match="version"):
        parse_health({"qq_full_version": "3.2.35-99999"}, expected_version="3.2.34-53644")


def test_wait_budget_is_bounded_and_ready_stops_wait():
    calls = []
    assert not wait_ready(lambda: False, attempts=3, sleep=lambda _: calls.append(1))
    assert len(calls) == 3
    assert wait_ready(lambda: True, attempts=3, sleep=lambda _: pytest.fail("unneeded sleep"))


def test_stop_during_startup_cancels_readiness_wait_immediately():
    sleeps = []
    assert not wait_ready(lambda: False, attempts=90, cancelled=lambda: bool(sleeps),
                          sleep=lambda _: sleeps.append(1))
    assert len(sleeps) == 1


def test_exited_launcher_does_not_spawn_another_qq_or_stop_a_foreign_group(monkeypatch):
    process = MagicMock(pid=77)
    process.poll.return_value = 0
    launch = MagicMock(return_value=process)
    owned = MagicMock(side_effect=[True, False])
    stop = MagicMock()
    monkeypatch.setattr(pmhq_process.subprocess, "Popen", launch)
    monkeypatch.setattr(pmhq_process, "display_ready", lambda: True)
    monkeypatch.setattr(pmhq_process, "owned_qq", owned)
    monkeypatch.setattr(pmhq_process, "read_pmhq_health", lambda: SimpleNamespace(ready=True))
    monkeypatch.setattr(pmhq_process, "stop_group", stop)
    monkeypatch.setattr(pmhq_process.signal, "signal", lambda *_: 0)
    assert pmhq_process.run_pmhq(Path("private-config")) == 78
    assert launch.call_count == 1 and owned.call_count == 2
    assert all(call.args == (77,) for call in owned.call_args_list)
    stop.assert_called_once_with(77)


def test_no_native_launch_until_display_is_ready(monkeypatch):
    launch = MagicMock()
    monkeypatch.setattr(pmhq_process.subprocess, "Popen", launch)
    monkeypatch.setattr(pmhq_process, "wait_ready", lambda *_a, **_k: False)
    assert pmhq_process.run_pmhq(Path("private-config")) == 78
    launch.assert_not_called()
