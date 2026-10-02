from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from al1s.app.service_state import service_state
from al1s.bots.container_control import Alias, ContainerCommand, ControlError
from al1s.bots.control_client import ControlClient

router = APIRouter(prefix="/bots/containers")


def _call(request: Request, alias: Alias, command: ContainerCommand | None = None) -> JSONResponse:
    settings = service_state(request).settings
    try:
        state = ControlClient(
            settings.bot_control_url, settings.bot_control_token.get_secret_value(),
        ).call(alias, command)
    except ControlError as exc:
        return JSONResponse({"code": exc.code}, status_code=exc.status,
                            headers={"Cache-Control": "no-store"})
    return JSONResponse(state.model_dump(), headers={"Cache-Control": "no-store"})


@router.get("/{alias}")
def state(alias: Alias, request: Request) -> JSONResponse:
    return _call(request, alias)


@router.post("/{alias}")
def command(alias: Alias, body: ContainerCommand, request: Request) -> JSONResponse:
    return _call(request, alias, body)
