from uuid import uuid4

import pytest

from al1s.execution.diagnostic_events import conditional_skip_metadata
from al1s.notifications.task_links import task_link, validate_public_url


@pytest.mark.parametrize(
    "value",
    [
        "file:///tmp/a",
        "https://user:password@host",
        "https://host/a",
        "https://host?key=secret",
        "https://host#x",
        "https://host:99999",
        "https://host:invalid",
        "https://host\n.evil",
        "https://host name",
    ],
)
def test_public_origin_cannot_embed_credentials_or_paths(value):
    with pytest.raises(ValueError):
        validate_public_url(value)


def test_link_uses_validated_task_identity_only():
    identity = uuid4()
    assert (
        task_link("https://platform.local:8443", {"task_id": str(identity)})
        == f"https://platform.local:8443/tasks/history?task_id={identity}"
    )
    assert task_link("https://host", {"task_id": "../auth"}) is None
    assert task_link("", {"task_id": str(identity)}) is None


def test_skip_event_metadata_has_identity_not_capture_body_or_path():
    identity = uuid4()
    event = {
        "mode": "numeric",
        "operator": "gt",
        "value": 4,
        "target_step_number": 8,
        "capture": {"artifact_id": str(identity), "data_base64": "secret", "path": "secret"},
        "module_name": "not copied",
    }
    result = conditional_skip_metadata({"modules": [{"result": {"conditional_skips": [event]}}]})[0]
    assert result["capture_id"] == str(identity)
    assert result["target_step_number"] == 8
    assert "secret" not in str(result)
    event.update(mode={}, value=10**400)
    result = conditional_skip_metadata({"modules": [{"result": {"conditional_skips": [event]}}]})[0]
    assert "mode" not in result and "value" not in result
