import os
from contextlib import ExitStack
from typing import cast

import uvicorn
from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from al1s.adapters.postgres.bot_unit_of_work import BotSqlAlchemyUnitOfWork
from al1s.adapters.s3.blob_store import S3BlobStore
from al1s.app.config import Settings
from al1s.app.logging import configure_logging
from al1s.app.meme_services import meme_services
from al1s.bots.ports import BotUnitOfWork
from al1s.bots.service import BotService
from al1s.infrastructure.database import create_database_engine, create_session_factory
from al1s.meme_workshop.runtime import create_runtime_app
from al1s.meme_workshop.runtime_roles import RuntimeRole, make_qq_worker, render_limit
from al1s.secrets.security import FernetSecretCipher


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AL1S_MEME_", env_file=None, extra="ignore", hide_input_in_errors=True
    )
    backend_token: SecretStr
    database_url: str
    master_key: SecretStr
    s3_endpoint_url: str
    s3_bucket: str = "al1s-platform"
    s3_access_key: str
    s3_secret_key: SecretStr
    role: RuntimeRole = "combined"

    @field_validator("backend_token", "master_key")
    @classmethod
    def private_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw.encode()) < 32 or raw.lower().startswith(("change-me", "replace-")):
            raise ValueError("private_runtime_key_required")
        return value


def run() -> None:
    if not os.environ.get("AL1S_MEME_BACKEND_TOKEN", "").strip():
        print("meme_backend_disabled_no_configuration", flush=True)
        return
    runtime = RuntimeSettings()
    settings = Settings(
        _env_file=None,
        environment="development",
        database_url=runtime.database_url,
        notification_master_key=runtime.master_key,
        s3_endpoint_url=runtime.s3_endpoint_url,
        s3_bucket=runtime.s3_bucket,
        s3_access_key=runtime.s3_access_key,
        s3_secret_key=runtime.s3_secret_key,
    )
    configure_logging()
    with ExitStack() as owned:
        engine = create_database_engine(settings)
        owned.callback(engine.dispose)
        sessions = create_session_factory(engine)
        objects = S3BlobStore(settings)
        owned.callback(objects.close)
        cipher = FernetSecretCipher(runtime.master_key.get_secret_value())
        bots = BotService(
            lambda: cast(BotUnitOfWork, BotSqlAlchemyUnitOfWork(sessions)), cipher, cipher
        )
        workshop = meme_services(sessions, objects, render_limit=render_limit(runtime.role))
        app = create_runtime_app(
            workshop,
            bots,
            runtime.backend_token.get_secret_value(),
            (None if os.environ.get("AL1S_QQ_IDENTITY_BLOCKED") == "1"
             else make_qq_worker(runtime.role, workshop, cipher)),
            role=runtime.role,
        )
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=3101,
            access_log=False,
            log_level="warning",
            timeout_graceful_shutdown=25,
        )


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(f"meme_backend_start_failed:{type(error).__name__}", flush=True)
        raise SystemExit(78 if isinstance(error, (ValidationError, ValueError)) else 1) from None
