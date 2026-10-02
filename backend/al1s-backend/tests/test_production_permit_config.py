import pytest
from pydantic import ValidationError

from al1s.app.config import Settings

PRIVATE_SECRETS = {
    "notification_master_key": "notification-private-secret-" * 2,
    "mqtt_session_signing_key": "mqtt-session-private-secret-" * 2,
    "s3_secret_key": "s3-private-secret-123456",
    "mqtt_password": "mqtt-private-secret-123456",
    "mqtt_publisher_password": "mqtt-publisher-private-secret-123456",
}


@pytest.mark.parametrize(
    "key", ["", "short", "change-me-offline-permit-signing-key-at-least-32-bytes"]
)
def test_production_rejects_public_or_missing_permit_key(key):
    with pytest.raises(ValidationError, match="private offline permit"):
        Settings(_env_file=None, environment="production", offline_permit_signing_key=key)


def test_production_accepts_explicit_private_permit_key():
    Settings(
        _env_file=None, environment="production", offline_permit_signing_key="a1b2c3d4" * 8,
        **PRIVATE_SECRETS,
    )


@pytest.mark.parametrize("field", sorted(PRIVATE_SECRETS))
def test_production_rejects_public_and_short_other_secrets(field):
    values = {**PRIVATE_SECRETS, field: "short"}
    with pytest.raises(ValidationError, match="production requires a private"):
        Settings(
            _env_file=None, environment="production", offline_permit_signing_key="a1b2c3d4" * 8,
            **values,
        )


@pytest.mark.parametrize("field", sorted(PRIVATE_SECRETS))
def test_production_rejects_example_placeholders(field):
    values = {**PRIVATE_SECRETS, field: "change-me-test-fixture-placeholder-long-enough-to-pass-length"}
    with pytest.raises(ValidationError, match="production requires a private"):
        Settings(
            _env_file=None, environment="production", offline_permit_signing_key="a1b2c3d4" * 8,
            **values,
        )


def test_production_checks_bot_control_token_only_when_enabled():
    common = {
        "_env_file": None, "environment": "production",
        "offline_permit_signing_key": "a1b2c3d4" * 8,
        **PRIVATE_SECRETS,
    }
    Settings(**common)
    with pytest.raises(ValidationError, match="Bot control token"):
        Settings(**common, bot_control_url="http://bot-control:8000")
    Settings(**common, bot_control_url="http://bot-control:8000",
             bot_control_token="private-bot-control-token-1234567890")


def test_invalid_configuration_does_not_print_other_secrets():
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, environment='production', offline_permit_signing_key='short',
                 admin_password='do-not-include-this-password')
    assert 'do-not-include-this-password' not in str(caught.value)
