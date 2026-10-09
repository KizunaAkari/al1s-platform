"""Read the authenticated terminal's current version; never another terminal's identity."""
from fastapi import APIRouter, Header, Request

from al1s.api.terminal_auth import authenticated_terminal

router = APIRouter()


@router.get("/terminal/identity")
def identity(request: Request, authorization: str | None = Header(None)) -> dict[str,object]:
    terminal = authenticated_terminal(request, authorization)
    return {"terminal_id": terminal.terminal_id, "row_version": terminal.row_version,
            "acceptance_status": terminal.acceptance_status.value}
