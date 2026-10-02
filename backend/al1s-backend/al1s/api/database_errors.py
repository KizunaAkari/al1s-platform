"""Expose only retryable database budget failures, never SQL or bound values."""

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, TimeoutError


def _busy(request: Request) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "1"},
        content={
            "code": "database_busy",
            "message": "数据库繁忙, 请稍后刷新核对结果并重试原操作。",
            "request_id": getattr(request.state, "request_id", "unknown"),
        },
    )


def internal_error_response(request: Request, exc: Exception) -> JSONResponse:
    structlog.get_logger().error("unhandled_request_error", error=type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={
            "code": "internal_error",
            "message": "Unexpected server error",
            "request_id": getattr(request.state, "request_id", "unknown"),
        },
    )


def install_database_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(TimeoutError)
    async def pool_timeout(request: Request, exc: TimeoutError) -> JSONResponse:
        return _busy(request)

    @app.exception_handler(DBAPIError)
    async def database_error(request: Request, exc: DBAPIError) -> JSONResponse:
        if getattr(exc.orig, "sqlstate", None) in {"57014", "55P03"}:
            return _busy(request)
        # Keep the existing generic error/logging policy for other failures.
        return internal_error_response(request, exc)
