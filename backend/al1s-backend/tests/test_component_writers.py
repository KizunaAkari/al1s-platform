import json
from types import SimpleNamespace

import pytest

from al1s.components.host.writers import capture_writer, writer_action


def deployment(project="al1s-platform", running=True):
    calls = []
    row = {
        "Id": "a" * 64,
        "Image": "sha256:" + "b" * 64,
        "Name": "/al1s-bot-discord",
        "State": {"Running": running},
        "Config": {
            "Labels": {
                "com.docker.compose.project": "al1s-bot",
                "com.docker.compose.service": "discord-worker",
            }
        },
    }

    def run(args, timeout=30):
        calls.append(args)
        if args[1] == "ps":
            return "a" * 12
        if args[1] == "inspect":
            return json.dumps([row])
        return ""

    return (
        SimpleNamespace(settings=SimpleNamespace(project=project), runner=SimpleNamespace(run=run)),
        calls,
        row,
    )


def test_s3_writer_is_stopped_and_restored_only_by_captured_identity():
    source, calls, _ = deployment()
    writer = capture_writer(source)
    writer_action(source, writer, "stop")
    writer_action(source, writer, "start")
    assert ["docker", "stop", "--time", "75", "a" * 64] in calls
    assert ["docker", "start", "a" * 64] in calls


def test_changed_writer_cannot_be_stopped_or_restarted():
    source, calls, row = deployment()
    writer = capture_writer(source)
    row["Image"] = "changed"
    with pytest.raises(ValueError, match="identity"):
        writer_action(source, writer, "stop")
    assert not any(args[1] == "stop" for args in calls)


def test_previously_stopped_writer_and_isolated_project_are_not_started():
    source, calls, _ = deployment(running=False)
    writer_action(source, capture_writer(source), "start")
    assert not any(args[1] == "start" for args in calls)
    isolated, calls, _ = deployment(project="isolated-fixture")
    assert capture_writer(isolated) is None and not calls
