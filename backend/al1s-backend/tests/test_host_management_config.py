import pytest
from pydantic import ValidationError

from al1s.execution.host_management import HostCommand, HostHealth, HostManagerConnection


@pytest.mark.parametrize("url", [
    "http://host:8771", "https://user:password@host", "https://host/path", "https://host?token=x",
    " https://host", "https://host\n", "https://host\\other",
])
def test_management_only_uses_explicit_https_origin(url):
    with pytest.raises(ValidationError):
        HostManagerConnection(url=url, token="x" * 32)


def test_management_secret_is_not_displayed():
    connection = HostManagerConnection(url="https://host:8771", token="private-token-value-" * 3)
    assert "private-token-value" not in repr(connection)


def test_health_response_drops_unknown_sensitive_fields():
    result = HostHealth.model_validate({
        "boot_id": "boot", "container_id": "c", "container_started_at": "start",
        "healthy": True, "environment": {"TOKEN": "secret"},
    })
    assert "secret" not in result.model_dump_json()


@pytest.mark.parametrize("field", ["accepted_at", "started_at", "expires_at"])
@pytest.mark.parametrize("invalid", [float("inf"), 1e100, -1])
def test_command_response_rejects_unbounded_deadline(field, invalid):
    from uuid import uuid4

    payload = dict(command_id=uuid4(), action="restart_host", state="accepted",
                   accepted_at=1, started_at=None, expires_at=600,
                   error_code=None, version=1)
    payload[field] = invalid
    with pytest.raises(ValidationError):
        HostCommand.model_validate(payload)
