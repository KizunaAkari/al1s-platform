from __future__ import annotations

from uuid import UUID

from starlette.requests import HTTPConnection

from al1s.app.service_state import service_state
from al1s.execution.types import TerminalRecord


def authenticated_terminal_id(request: HTTPConnection, authorization: str | None) -> UUID:
    return authenticated_terminal(request, authorization).terminal_id


def authenticated_terminal(request: HTTPConnection, authorization: str | None) -> TerminalRecord:
    credential = ""
    if authorization is not None:
        scheme, separator, value = authorization.partition(" ")
        if separator and scheme.lower() == "bearer":
            credential = value
    resources = service_state(request).execution_resources
    return resources.authenticate_terminal(credential)
