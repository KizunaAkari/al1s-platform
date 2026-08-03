import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ImportError:  # allows minimal diagnostics before installing requirements
    def load_dotenv(*_args, **_kwargs):
        return False

load_dotenv(".env")


def env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "maa-control-center")
    database_path: str = os.getenv("DATABASE_PATH", "./data/control-center.db")
    screen_dir: str = os.getenv("SCREEN_DIR", "./data/screens")
    recording_dir: str = os.getenv("RECORDING_DIR", "./data/recordings")
    agent_token: str = os.getenv("AGENT_TOKEN", "change-me")
    heartbeat_timeout_seconds: int = int(os.getenv("HEARTBEAT_TIMEOUT_SECONDS", "45"))
    storage_warning_bytes: int = int(float(os.getenv("STORAGE_WARNING_GB", "5")) * 1024 ** 3)
    maintenance_interval_seconds: int = int(os.getenv("MAINTENANCE_INTERVAL_SECONDS", "15"))
    timezone: str = os.getenv("TZ", "Asia/Shanghai")
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from: str = os.getenv("SMTP_FROM", "")
    smtp_starttls: bool = env_flag("SMTP_STARTTLS", True)
    smtp_ssl: bool = env_flag("SMTP_SSL", False)
    failure_email_to: tuple[str, ...] = tuple(
        item.strip() for item in os.getenv("FAILURE_EMAIL_TO", "").split(",") if item.strip()
    )
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    notification_key_path: str = os.getenv("NOTIFICATION_KEY_PATH", "./data/notification.key")
    frontend_dist: str = os.getenv("FRONTEND_DIST", "./frontend/dist")
    terminal_artifact_dir: str = os.getenv("TERMINAL_ARTIFACT_DIR", "./data/terminal-deploy")
    terminal_deploy_public_url: str = os.getenv("TERMINAL_DEPLOY_PUBLIC_URL", "").rstrip("/")
    terminal_deployer_port: int = int(os.getenv("TERMINAL_DEPLOYER_PORT", "8767"))
    terminal_deploy_token: str = os.getenv("TERMINAL_DEPLOY_TOKEN", "") or agent_token
    terminal_image_tag: str = os.getenv("TERMINAL_IMAGE_TAG", "al1s-terminal-agent:npu-arm64")
    cors_origins: tuple[str, ...] = tuple(
        item.strip() for item in os.getenv(
            "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",") if item.strip()
    )


settings = Settings()
