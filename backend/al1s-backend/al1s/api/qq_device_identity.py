from uuid import UUID

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from al1s.app.service_state import service_state
from al1s.bots.container_control import ControlError
from al1s.bots.qq_identity.contracts import QqIdentityError, QqIdentitySave
from al1s.bots.qq_identity.control import QqIdentityControlClient
from al1s.bots.qq_identity.runtime_routes import RequestHeader, RevisionHeader

router = APIRouter(prefix="/bots/qq/device-identity")


def call(request: Request, body: QqIdentitySave | None = None,
         revision: str = "", request_id: UUID | None = None) -> JSONResponse:
    try:
        settings = service_state(request).settings
        client = QqIdentityControlClient(
            settings.bot_control_url, settings.bot_control_token.get_secret_value()
        )
        if body is None:
            state = client.read()
        else:
            assert request_id is not None
            state = client.save(body, revision, request_id)
        return JSONResponse(state.model_dump(mode="json"), headers={"Cache-Control": "no-store"})
    except (QqIdentityError, ControlError) as error:
        return JSONResponse({"code": error.code}, status_code=error.status,
                            headers={"Cache-Control": "no-store"})


@router.get("")
def read(request: Request) -> JSONResponse:
    return call(request)


@router.put("")
def save(request: Request, body: QqIdentitySave, revision: RevisionHeader,
         request_id: RequestHeader) -> JSONResponse:
    return call(request, body, revision.strip('"'), request_id)
