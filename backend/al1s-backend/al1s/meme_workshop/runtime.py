import asyncio
import hmac
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from al1s.adapters.postgres.quant_settings import QuantSettingsRepository
from al1s.api.database_errors import install_database_error_handlers, internal_error_response
from al1s.api.meme_workshop import router
from al1s.api.quant import router as quant_router
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
) -> FastAPI:
    if len(control_token.encode()) < 32:
        raise ValueError("meme_control_token_too_short")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(qq_worker.run()) if qq_worker is not None else None
        try:
            yield
        finally:
            if qq_worker is not None:
                await qq_worker.stop()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.meme_workshop, app.state.bot_service = workshop, bots
    app.include_router(router, prefix="/api/v1")
    if role == "qq":
        app.include_router(identity_router(identity_repository))
        if quant_settings is not None and quant_gateway is not None:
            app.state.quant_settings = quant_settings
            app.state.quant_gateway = quant_gateway
            app.state.quant_process = quant_process
            app.state.quant_credentials = quant_credentials
            app.state.quant_worker = qq_worker
            app.include_router(quant_router, prefix="/api/v1")

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
