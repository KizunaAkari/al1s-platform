"""Single-admin browser sessions; device credentials remain separate."""

import hashlib
import hmac
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from threading import Lock

from fastapi import APIRouter, FastAPI, Request, Response
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from al1s.app.config import Settings

COOKIE = "al1s_admin"
TTL = 8 * 60 * 60
router = APIRouter(prefix="/api/v1/auth")


class AdminSessions:
    def __init__(self, password: str):
        self._password = hashlib.sha256(password.encode()).digest()
        self.configured = len(password) >= 12
        self._sessions: dict[str, float] = {}
        self._attempts: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._lock = Lock()

    def login(self, password: str, peer: str) -> tuple[int, str | None]:
        now = time.monotonic()
        with self._lock:
            since, count = self._attempts.get(peer, (now, 0))
            if now - since >= 60:
                since, count = now, 0
            self._attempts[peer] = (since, count + 1)
            self._attempts.move_to_end(peer)
            while len(self._attempts) > 1024:
                self._attempts.popitem(last=False)
            if count >= 5:
                return 429, None
            if not self.configured:
                return 503, None
            if not hmac.compare_digest(self._password, hashlib.sha256(password.encode()).digest()):
                return 401, None
            self._sessions = {key: expiry for key, expiry in self._sessions.items() if expiry > now}
            if len(self._sessions) >= 32:
                del self._sessions[min(self._sessions, key=lambda key: self._sessions[key])]
            token = secrets.token_urlsafe(32)
            self._sessions[self._digest(token)] = now + TTL
            return 200, token

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def valid(self, token: str) -> bool:
        with self._lock:
            return self._sessions.get(self._digest(token), 0) > time.monotonic()

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(self._digest(token), None)


def device_route(path: str) -> bool:
    # These endpoints perform their own credential/grant validation.
    return (
        path.startswith("/api/v1/terminal/")
        or path.startswith("/api/v1/bots/worker/")
        or path in {
            "/api/v1/terminals/register", "/api/v1/bots/workers/register",
            "/api/v1/target-devices/discoveries",
            "/api/v1/target-devices/adb-observations",
        }
        or re.fullmatch(
            r"/api/v1/terminals/[0-9a-fA-F-]{36}/(heartbeat|capability-profiles|credentials/rotate)",
            path,
        ) is not None
        or re.fullmatch(
            r"/api/v1/maa/scripts/[0-9a-fA-F-]{36}/quick-test-results", path,
        ) is not None
    )


def error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"code": code, "message": code}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def install_admin_auth(app: FastAPI, settings: Settings) -> None:
    app.state.admin_sessions = AdminSessions(settings.admin_password.get_secret_value())
    app.include_router(router)

    @app.middleware("http")
    async def protect(
        request: Request, call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/appearance/login-background":
            return await call_next(request)
        if (request.method == "GET"
                and path in {"/api/v1/system/health/live", "/api/v1/system/health/ready"}
                and request.client and request.client.host in {"127.0.0.1", "::1"}):
            return await call_next(request)
        if not path.startswith("/api/v1/") or device_route(path):
            return await call_next(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            allowed = {str(request.base_url).rstrip("/"), *settings.cors_origin_list}
            if request.headers.get("x-al1s-csrf") != "1" or (origin and origin not in allowed):
                return error("admin_csrf_rejected", 403)
        if (path != "/api/v1/auth/login"
                and not app.state.admin_sessions.valid(request.cookies.get(COOKIE, ""))):
            return error("admin_login_required", 401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=4096)


@router.post("/login")
def login(body: LoginBody, request: Request) -> Response:
    peer = request.client.host if request.client else "unknown"
    status, token = request.app.state.admin_sessions.login(body.password, peer)
    if token is None:
        return error("admin_login_failed" if status == 401 else "admin_login_unavailable", status)
    response = JSONResponse({"authenticated": True}, headers={"Cache-Control": "no-store"})
    response.set_cookie(COOKIE, token, max_age=TTL, httponly=True, samesite="strict",
                        secure=request.app.state.settings.admin_cookie_secure, path="/api/v1")
    return response


@router.get("/session")
def session() -> dict[str, bool]:
    return {"authenticated": True}


@router.post("/logout", status_code=204)
def logout(request: Request) -> Response:
    request.app.state.admin_sessions.logout(request.cookies.get(COOKIE, ""))
    response = Response(status_code=204)
    response.delete_cookie(COOKIE, path="/api/v1")
    return response
