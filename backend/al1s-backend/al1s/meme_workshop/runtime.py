import asyncio
import hmac
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from al1s.adapters.postgres.quant_settings import QuantSettingsRepository
from al1s.adapters.postgres.trading_reply_repository import TradingReplyRepository
from al1s.api.trading_replies import router as trading_replies_router
from al1s.api.database_errors import install_database_error_handlers, internal_error_response
from al1s.api.meme_workshop import router
from al1s.api.quant import router as quant_router
from al1s.api.freqtrade import router as freqtrade_router
from al1s.api.trading_admin import router as trading_admin_router
from al1s.api.bot_ai import router as ai_router
from al1s.api.bot_drawing import router as drawing_router
from al1s.bots.errors import BotDomainError
from al1s.bots.qq_identity.contracts import QqIdentityError
from al1s.bots.qq_identity.repository import QqIdentityRepository
from al1s.bots.qq_identity.runtime_routes import identity_router
from al1s.bots.service import BotService
from al1s.meme_workshop.qq_worker import QqWorker
from al1s.meme_workshop.runtime_identity import read_runtime_identity
from al1s.meme_workshop.runtime_roles import RuntimeRole
from al1s.meme_workshop.service import MemeWorkshopService
from al1s.quant.gateway import QuantGateway
from al1s.quant.process_client import QuantProcessClient
from al1s.adapters.postgres.trading_admin_repository import TradingAdminRepository
from al1s.trading_admin.service import TradingAdminService


def create_runtime_app(
    workshop: MemeWorkshopService,
    bots: BotService,
    control_token: str,
    qq_worker: QqWorker | None = None,
    *,
    role: RuntimeRole = "combined",
    identity_repository: QqIdentityRepository | None = None,
    quant_settings: QuantSettingsRepository | None = None,
    quant_gateway: QuantGateway | None = None,
    quant_process: QuantProcessClient | QuantGateway | None = None,
    quant_credentials=None,
    freqtrade_repository=None,
    freqtrade_client=None,
    freqtrade_adapter=None,
    ai_repository=None,
    ai_controller=None,
    ai_hub=None,
    drawing_repository=None,
    drawing_configuration=None,
    drawing_media=None,
    drawing_service=None,
    drawing_runner=None,
) -> FastAPI:
    if len(control_token.encode()) < 32:
        raise ValueError("meme_control_token_too_short")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "trading_admin_repository", None) is not None:
            await asyncio.to_thread(app.state.trading_admin_repository.recover)
        local_ai = bool(ai_hub and getattr(ai_hub, 'local_runtime', False))
        if ai_repository is not None and ai_controller is not None:
            await asyncio.to_thread(ai_repository.recover, native_stopped=local_ai)
            ai_controller.start()
            if local_ai:
                await ai_controller.wait_initial()
        if drawing_configuration is not None:
            drawing_configuration.start()
        if drawing_runner is not None:
            drawing_runner.start()
        task = asyncio.create_task(qq_worker.run()) if qq_worker is not None else None
        try:
            yield
        finally:
            if drawing_runner is not None:
                await drawing_runner.close()
            if ai_controller is not None and not local_ai:
                await ai_controller.close()
            if qq_worker is not None:
                await qq_worker.stop()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if drawing_configuration is not None:
                await drawing_configuration.close()
            if ai_controller is not None and local_ai:
                await ai_controller.close()
            if ai_hub is not None:
                await ai_hub.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.meme_workshop, app.state.bot_service = workshop, bots
    app.include_router(router, prefix="/api/v1")
    if role == "qq":
        app.include_router(identity_router(identity_repository))
        if ai_repository is not None:
            app.state.ai_repository = ai_repository
            app.state.ai_controller = ai_controller
            app.include_router(ai_router, prefix="/api/v1")
            if ai_hub is not None:
                app.state.ai_hub = ai_hub
        if drawing_repository is not None and drawing_configuration is not None:
            app.state.drawing_repository = drawing_repository
            app.state.drawing_configuration = drawing_configuration
            app.state.drawing_media = drawing_media
            app.state.drawing_service = drawing_service
            app.state.drawing_runner = drawing_runner
            app.include_router(drawing_router, prefix="/api/v1")
        if quant_settings is not None and quant_gateway is not None:
            app.state.quant_settings = quant_settings
            app.state.trading_replies = TradingReplyRepository(quant_settings.sessions)
            app.include_router(trading_replies_router, prefix="/api/v1")
            app.state.quant_gateway = quant_gateway
            app.state.quant_process = quant_process
            app.state.quant_credentials = quant_credentials
            app.state.quant_worker = qq_worker
            app.include_router(quant_router, prefix="/api/v1")
        if freqtrade_repository is not None and freqtrade_client is not None:
            app.state.freqtrade_repository = freqtrade_repository
            app.state.freqtrade_client = freqtrade_client
            app.state.freqtrade_adapter = freqtrade_adapter
            app.include_router(freqtrade_router, prefix='/api/v1')
        if quant_settings is not None and quant_gateway is not None:
            admin_repository = TradingAdminRepository(quant_settings.sessions)
            app.state.trading_admin_repository = admin_repository
            app.state.trading_admin = TradingAdminService(admin_repository, quant_settings, quant_gateway, quant_process,
                                                          freqtrade_repository, freqtrade_client)
            app.include_router(trading_admin_router, prefix="/api/v1")

    @app.exception_handler(QqIdentityError)
    async def identity_error(_request: Request, error: QqIdentityError) -> JSONResponse:
        return JSONResponse({"code": error.code}, status_code=error.status)

    @app.middleware("http")
    async def protect(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not (request.method == "GET" and request.url.path == "/health/live"):
            supplied = request.headers.get("x-al1s-meme-control", "")
            if not hmac.compare_digest(supplied.encode(), control_token.encode()):
                return JSONResponse({"detail": "meme_control_required"}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-AL1S-Meme-Role"] = role
        return response

    @app.get("/health/live")
    def health() -> dict[str, bool]:
        return {"live": True}

    @app.get("/api/v1/bots/memes/runtime")
    def identity() -> dict[str, object]:
        try:
            metadata = read_runtime_identity()
        except (OSError, ValueError):
            return {"role": role, "runtime_version": None}
        return {**metadata.model_dump(), "role": role}

    @app.exception_handler(BotDomainError)
    async def bot_error(request: Request, error: BotDomainError) -> JSONResponse:
        return JSONResponse(
            {"code": error.code, "message": error.message}, status_code=error.status_code
        )

    app.add_exception_handler(Exception, internal_error_response)
    install_database_error_handlers(app)
    return app
