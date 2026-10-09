import asyncio
import hmac
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from al1s.api.database_errors import install_database_error_handlers, internal_error_response
from al1s.api.meme_workshop import router
from al1s.bots.errors import BotDomainError
from al1s.bots.service import BotService
from al1s.meme_workshop.qq_worker import QqWorker
from al1s.meme_workshop.service import MemeWorkshopService


def create_runtime_app(
    workshop: MemeWorkshopService,
    bots: BotService,
    control_token: str,
    qq_worker: QqWorker | None = None,
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
        return response

    @app.get("/health/live")
    def health() -> dict[str, bool]:
        return {"live": True}

    @app.exception_handler(BotDomainError)
    async def bot_error(request: Request, error: BotDomainError) -> JSONResponse:
        return JSONResponse(
            {"code": error.code, "message": error.message}, status_code=error.status_code
        )

    app.add_exception_handler(Exception, internal_error_response)
    install_database_error_handlers(app)
    return app
