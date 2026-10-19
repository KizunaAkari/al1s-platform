"""Optional private control process; never mounted into the public platform."""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header
from starlette.responses import JSONResponse

from al1s.bots.container_control import (
    Alias,
    ContainerCommand,
    ContainerState,
    ControlError,
    DockerControl,
)
from al1s.bots.qq_identity.contracts import QqIdentityError, QqIdentitySave, QqIdentityState
from al1s.bots.qq_identity.control import QqIdentityGateway
from al1s.bots.qq_identity.runtime_routes import RequestHeader, RevisionHeader
from al1s.components.host.bot_maintenance import BotMaintenanceGuard
from al1s.meme_workshop.transport import MemeBackendClient


def create_proxy(token: str, control: DockerControl, *,
                 identity_gateway: QqIdentityGateway | None = None) -> FastAPI:
    if len(token) < 32:
        raise ValueError("control_token_requires_32_characters")

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if not hmac.compare_digest((authorization or "").encode(), f"Bearer {token}".encode()):
            raise ControlError("control_unauthorized", 401)

    app = FastAPI(
        docs_url=None, redoc_url=None, openapi_url=None, dependencies=[Depends(authorize)]
    )

    @app.exception_handler(ControlError)
    async def error_handler(_request: object, error: ControlError) -> JSONResponse:
        return JSONResponse({"code": error.code}, status_code=error.status)

    @app.get("/containers/{alias}")
    def state(alias: Alias) -> ContainerState:
        return control.state(alias)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(QqIdentityError)
    async def identity_error(_request: object, error: QqIdentityError) -> JSONResponse:
        return JSONResponse({"code": error.code}, status_code=error.status,
                            headers={"Cache-Control": "no-store"})

    @app.get("/qq/device-identity")
    def identity_read() -> QqIdentityState:
        if identity_gateway is None:
            raise QqIdentityError("qq_identity_gateway_unconfigured", 503)
        return identity_gateway.read()

    @app.put("/qq/device-identity")
    def identity_save(body: QqIdentitySave, revision: RevisionHeader,
                      request_id: RequestHeader) -> QqIdentityState:
        if identity_gateway is None:
            raise QqIdentityError("qq_identity_gateway_unconfigured", 503)
        return identity_gateway.save(body, revision.strip('"'), request_id)

    @app.post("/containers/{alias}")
    def command(alias: Alias, body: ContainerCommand) -> ContainerState:
        logger = logging.getLogger("al1s.bot_control")
        try:
            result = control.command(alias, body)
        except ControlError as exc:
            logger.warning("bot_control alias=%s action=%s result=%s", alias, body.action, exc.code)
            raise
        logger.info("bot_control alias=%s action=%s result=confirmed", alias, body.action)
        return result

    return app


def from_environment() -> FastAPI:
    directory = os.environ.get("AL1S_BOT_MAINTENANCE_DIRECTORY", "")
    maintenance = BotMaintenanceGuard(Path(directory)) if directory else None
    targets = {
        alias: value
        for alias, key in (
            ("qq", "AL1S_CONTROL_QQ_CONTAINER"),
            ("discord", "AL1S_CONTROL_DISCORD_CONTAINER"),
        )
        if (value := os.environ.get(key))
    }
    return create_proxy(
        os.environ.get("AL1S_BOT_CONTROL_TOKEN", ""),
        DockerControl(
            targets,
            "/var/run/docker.sock",
            os.environ.get("AL1S_CONTROL_DOCKER_VERSION", "1.45"),
            maintenance=maintenance,
        ),
        identity_gateway=QqIdentityGateway(
            MemeBackendClient(os.environ.get("AL1S_MEME_QQ_BACKEND_URL", ""),
                              os.environ.get("AL1S_MEME_BACKEND_TOKEN", ""), expected_role="qq"),
            maintenance,
        ),
    )
