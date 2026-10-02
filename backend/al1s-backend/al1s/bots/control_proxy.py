"""Optional private control process; never mounted into the public platform."""
from __future__ import annotations

import hmac
import logging
import os
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


def create_proxy(token: str, control: DockerControl) -> FastAPI:
    if len(token) < 32:
        raise ValueError("control_token_requires_32_characters")

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if not hmac.compare_digest((authorization or "").encode(), f"Bearer {token}".encode()):
            raise ControlError("control_unauthorized", 401)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(authorize)])

    @app.exception_handler(ControlError)
    async def error_handler(_request: object, error: ControlError) -> JSONResponse:
        return JSONResponse({"code": error.code}, status_code=error.status)

    @app.get("/containers/{alias}")
    def state(alias: Alias) -> ContainerState:
        return control.state(alias)

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
    targets = {alias: value for alias, key in (
        ("qq", "AL1S_CONTROL_QQ_CONTAINER"), ("discord", "AL1S_CONTROL_DISCORD_CONTAINER"),
    ) if (value := os.environ.get(key))}
    return create_proxy(os.environ.get("AL1S_BOT_CONTROL_TOKEN", ""), DockerControl(
        targets, "/var/run/docker.sock", os.environ.get("AL1S_CONTROL_DOCKER_VERSION", "1.45"),
    ))
