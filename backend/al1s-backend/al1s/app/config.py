from functools import lru_cache
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from al1s.execution.host_management import HostManagerConnection


def _is_public_secret(value: str, minimum_bytes: int) -> bool:
    lowered = value.lower()
    return (
        len(value.encode("utf-8")) < minimum_bytes
        or lowered.startswith(("change-me", "replace-"))
        or lowered.startswith("al1s-local-")
        or "-local-change-me" in lowered
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AL1S_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    service_name: str = "al1s-platform-backend"
    service_version: str = "0.1.0"
    platform_public_url: str = ""

    @field_validator("platform_public_url")
    @classmethod
    def public_url_is_origin(cls, value: str) -> str:
        from al1s.notifications.task_links import validate_public_url

        return validate_public_url(value)

    frontend_directory: str | None = None
    environment: str = "development"
    admin_password: SecretStr = SecretStr("")
    admin_cookie_secure: bool = True
    llbot_webui_url: str = ""
    bot_control_url: str = ""
    bot_control_token: SecretStr = SecretStr("")
    host_managers: dict[UUID, HostManagerConnection] = {}
    terminal_heartbeat_timeout_seconds: int = Field(default=90, ge=30, le=3600)
    database_url: str = "postgresql+psycopg://al1s:change-me@localhost:5432/al1s"
    database_pool_size: int = Field(default=5, ge=1, le=100)
    database_max_overflow: int = Field(default=5, ge=0, le=100)
    database_pool_timeout_seconds: float = Field(default=1.0, gt=0, le=10)
    database_statement_timeout_ms: int = Field(default=5000, ge=1, le=60000)
    database_lock_timeout_ms: int = Field(default=1000, ge=1, le=10000)
    database_idle_transaction_timeout_ms: int = Field(default=15000, ge=1, le=120000)
    database_batch_statement_timeout_ms: int = Field(default=60000, ge=1, le=180000)
    s3_endpoint_url: str = "http://localhost:8333"
    s3_public_endpoint_url: str | None = None
    s3_bucket: str = "al1s-platform"
    s3_access_key: str = "al1s-local"
    s3_secret_key: SecretStr = SecretStr("change-me")
    offline_permit_signing_key: SecretStr = SecretStr(
        "change-me-offline-permit-signing-key-at-least-32-bytes"
    )
    offline_permit_ttl_seconds: int = 24 * 60 * 60
    notification_master_key: SecretStr = SecretStr(
        "change-me-notification-master-key-at-least-32-bytes"
    )
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_public_host: str = "localhost"
    mqtt_public_port: int = 1883
    mqtt_tls_enabled: bool = False
    mqtt_public_tls_enabled: bool = False
    mqtt_username: str = "al1s-admin"
    mqtt_password: SecretStr = SecretStr("change-me")
    mqtt_publisher_username: str = "al1s-platform-publisher"
    mqtt_publisher_password: SecretStr = SecretStr("change-me-publisher")
    mqtt_session_signing_key: SecretStr = SecretStr(
        "change-me-mqtt-session-signing-key-at-least-32-bytes"
    )
    mqtt_session_ttl_seconds: int = 60 * 60
    worker_interval_seconds: float = 1.0
    probe_timeout_seconds: float = 2.0
    cors_origins: str = "http://localhost:8180,http://localhost:5173"

    @model_validator(mode="after")
    def require_production_secrets(self) -> "Settings":
        if self.environment.lower() != "production":
            return self
        if _is_public_secret(self.offline_permit_signing_key.get_secret_value(), 32):
            raise ValueError(
                "production requires a private offline permit signing key of at least 32 bytes"
            )
        for label, value, minimum in (
            ("notification master key", self.notification_master_key, 32),
            ("MQTT session signing key", self.mqtt_session_signing_key, 32),
            ("S3 secret key", self.s3_secret_key, 16),
            ("MQTT password", self.mqtt_password, 16),
            ("MQTT publisher password", self.mqtt_publisher_password, 16),
        ):
            if _is_public_secret(value.get_secret_value(), minimum):
                raise ValueError(
                    f"production requires a private {label} of at least {minimum} bytes"
                )
        if self.bot_control_url and _is_public_secret(
            self.bot_control_token.get_secret_value(), 32
        ):
            raise ValueError("production requires a private Bot control token of at least 32 bytes")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
