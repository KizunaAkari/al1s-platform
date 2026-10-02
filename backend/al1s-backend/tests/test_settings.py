from al1s.app.config import Settings


def test_internal_and_public_mqtt_tls_are_configured_independently() -> None:
    settings = Settings(
        mqtt_tls_enabled=False,
        mqtt_public_tls_enabled=True,
    )

    assert settings.mqtt_tls_enabled is False
    assert settings.mqtt_public_tls_enabled is True


def test_notification_master_key_default_has_minimum_required_length() -> None:
    settings = Settings()

    assert len(settings.notification_master_key.get_secret_value()) >= 32
