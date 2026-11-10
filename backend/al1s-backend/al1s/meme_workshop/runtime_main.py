import os
from contextlib import ExitStack
from pathlib import Path
from typing import cast

import uvicorn
from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from al1s.adapters.postgres.bot_unit_of_work import BotSqlAlchemyUnitOfWork
from al1s.adapters.postgres.quant_replies import QuantReplies
from al1s.adapters.postgres.quant_settings import QuantSettingsRepository
from al1s.adapters.postgres.quant_credentials import QuantCredentials
from al1s.adapters.s3.blob_store import S3BlobStore
from al1s.app.config import Settings
from al1s.app.logging import configure_logging
from al1s.app.meme_services import meme_services
from al1s.bots.ports import BotUnitOfWork
from al1s.bots.service import BotService
from al1s.infrastructure.database import create_database_engine, create_session_factory
from al1s.meme_workshop.qq_worker import QqWorker
from al1s.meme_workshop.runtime import create_runtime_app
from al1s.meme_workshop.runtime_identity import read_runtime_identity
from al1s.meme_workshop.runtime_roles import RuntimeRole, make_qq_worker, render_limit
from al1s.quant.gateway import QuantGateway
from al1s.quant.credential_key import derive_quant_credential_key
from al1s.quant.process_client import QuantProcessClient
from al1s.adapters.postgres.freqtrade_repository import FreqtradeRepository
from al1s.freqtrade.client import FreqtradeClient, control_key
from al1s.freqtrade.qq_adapter import FreqtradeQqAdapter
from al1s.quant.qq_dispatch import QuantQqDispatcher
from al1s.secrets.security import FernetSecretCipher
from al1s.adapters.postgres.bot_ai_repository import BotAiRepository
from al1s.adapters.postgres.bot_drawing_repository import BotDrawingRepository
from al1s.bot_drawing.configuration import DrawingConfiguration
from al1s.bot_drawing.media import DrawingMediaStore
from al1s.bot_drawing.prompt_model import DrawingPromptModel
from al1s.bot_drawing.moderation import MODEL_REVISION, WDAuditor
from al1s.bot_drawing.runner import DrawingTaskRunner
from al1s.bot_drawing.service import DrawingService
from al1s.adapters.postgres.bot_ai_history import AiHistoryRepository
from al1s.bot_ai.configuration import ConfigController
from al1s.bot_ai.local_runtime import LocalAiRuntime
from al1s.bot_ai.media import MediaManager
from al1s.bot_ai.model_manager import LocalModelManager
from al1s.bot_ai.sender import AiSender
from al1s.bot_ai.service import BotAiService
from al1s.bot_ai.sticker_library import StickerLibrary, read_capacity


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


def make_local_ai(worker, sessions, cipher, quant_process, freqtrade_client, *,
                  media_root=None, sticker_root=None, config_path=None, gpu_queue=None):
    media_root = Path(media_root or os.environ.get('AL1S_AI_MEDIA_ROOT', '/run/al1s-ai/media'))
    sticker_root = Path(sticker_root or os.environ.get('AL1S_AI_STICKER_ROOT',
        '/var/lib/al1s-astrbot/data/plugin_data/astrbot_plugin_stealer'))
    config_path = Path(config_path or os.environ.get('AL1S_AI_STICKER_CONFIG',
        '/var/lib/al1s-astrbot/data/config/astrbot_plugin_stealer_config.json'))
    if media_root.is_symlink():
        raise ValueError('ai_media_directory_invalid')
    media_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sticker_root.exists() and not (sticker_root / 'cache' / 'emoji.db').is_file():
        raise ValueError('ai_sticker_database_missing')
    repository = BotAiRepository(sessions, cipher)
    history = AiHistoryRepository(sessions)
    stickers = StickerLibrary(sticker_root, media_root, capacity=read_capacity(config_path))
    runtime = LocalAiRuntime()
    service = BotAiService(repository, runtime, MediaManager(media_root),
        AiSender(repository, media_root), quant_process, freqtrade_client, worker.send_ai_meme)
    manager = LocalModelManager(runtime, repository, history, media_root, stickers=stickers, gpu_queue=gpu_queue)
    controller = ConfigController(repository, manager, runtime, service)
    service.sticker_importer = controller.import_sticker
    worker._ai_adapter = service
    return repository, controller, runtime


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
        quant_settings = quant_gateway = quant_process = None
        quant_credentials = None
        freqtrade_repository = freqtrade_client = freqtrade_adapter = None
        ai_repository = ai_controller = ai_hub = None
        drawing_repository = drawing_configuration = drawing_media = drawing_service = drawing_runner = None
        worker = (None if os.environ.get("AL1S_QQ_IDENTITY_BLOCKED") == "1"
                  else make_qq_worker(runtime.role, workshop, cipher))
        quant_token = os.environ.get("AL1S_QUANT_CONTROL_TOKEN", "")
        if runtime.role == "qq" and worker is not None and len(quant_token.encode()) >= 32:
            quant_settings, quant_gateway = QuantSettingsRepository(sessions), QuantGateway()
            quant_credentials = QuantCredentials(sessions, FernetSecretCipher(
                derive_quant_credential_key(runtime.master_key.get_secret_value())))
            quant_process = QuantProcessClient("/run/al1s-quant/control.sock", quant_token)
            owned.callback(quant_process.close)
            sender = QuantQqDispatcher(quant_process, QuantReplies(sessions))
            if os.environ.get('AL1S_FREQTRADE_ENABLED') == '1':
                freqtrade_repository = FreqtradeRepository(sessions)
                freqtrade_client = FreqtradeClient(control_key(quant_token))
                freqtrade_adapter = FreqtradeQqAdapter(freqtrade_repository, freqtrade_client,
                                                       QuantReplies(sessions))
            worker = QqWorker(workshop, cipher, quant_repository=quant_settings,
                              quant_gateway=quant_gateway, quant_process=quant_process,
                              quant_dispatch=sender.dispatch, quant_sender=sender,
                              freqtrade_adapter=freqtrade_adapter)
        try:
            identity = read_runtime_identity() if runtime.role == "qq" else None
            ai_capable = bool(identity and identity.qq_ai_v1)
            drawing_capable = bool(identity and identity.qq_comfy_drawing_v1)
        except (OSError, ValueError):
            ai_capable = drawing_capable = False
        if ai_capable or drawing_capable:
            from al1s.bot_ai.gpu_queue import GpuQueue
            gpu_queue = GpuQueue(recovering=bool(drawing_capable and worker is not None))
        if ai_capable:
            if worker is not None:
                ai_repository, ai_controller, ai_hub = make_local_ai(
                    worker, sessions, cipher, quant_process, freqtrade_client, gpu_queue=gpu_queue)
            else:
                ai_repository = BotAiRepository(sessions, cipher)
        if drawing_capable:
            drawing_repository = BotDrawingRepository(sessions, cipher=cipher)
            drawing_configuration = DrawingConfiguration(drawing_repository, ai_repository,
                ai_manager=ai_controller.engine if ai_controller is not None else None, gpu_queue=gpu_queue,
                qq_status=worker.drawing_connection_status if worker is not None else None)
            drawing_root = Path(os.environ.get('AL1S_DRAWING_DATA_ROOT', '/var/lib/al1s-astrbot/data'))
            drawing_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            drawing_media = DrawingMediaStore(drawing_root)
            if worker is not None:
                audit_path = drawing_root / 'al1s-drawing-models' / 'wd-vit-tagger-v3' / MODEL_REVISION
                auditor = WDAuditor(audit_path)
                drawing_sender = (worker._ai_adapter.sender if worker._ai_adapter is not None
                                  else AiSender(drawing_repository, drawing_root))
                drawing_prompt = DrawingPromptModel(drawing_repository)
                drawing_runner = DrawingTaskRunner(drawing_repository, drawing_configuration, drawing_media,
                                                  auditor, drawing_sender, worker.drawing_client,
                                                  gpu_queue=gpu_queue, prompt_model=drawing_prompt)
                drawing_service = DrawingService(drawing_repository, drawing_prompt,
                    drawing_media, drawing_sender, drawing_runner)
                worker._drawing_adapter = drawing_service
        app = create_runtime_app(
            workshop,
            bots,
            runtime.backend_token.get_secret_value(),
            worker,
            role=runtime.role,
            quant_settings=quant_settings, quant_gateway=quant_gateway, quant_process=quant_process,
            quant_credentials=quant_credentials,
            freqtrade_repository=freqtrade_repository, freqtrade_client=freqtrade_client,
            freqtrade_adapter=freqtrade_adapter,
            ai_repository=ai_repository, ai_controller=ai_controller, ai_hub=ai_hub,
            drawing_repository=drawing_repository, drawing_configuration=drawing_configuration,
            drawing_media=drawing_media,
            drawing_service=drawing_service, drawing_runner=drawing_runner,
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
