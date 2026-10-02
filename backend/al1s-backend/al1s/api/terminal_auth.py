from __future__ import annotations

from uuid import UUID

from fastapi import Request

from al1s.app.service_state import service_state


def authenticated_terminal_id(request: Request, authorization: str | None) -> UUID:
    credential = ""
    if authorization is not None:
        scheme, separator, value = authorization.partition(" ")
        if separator and scheme.lower() == "bearer":
            credential = value
    resources = service_state(request).execution_resources
    return resources.authenticate_terminal(credential).terminal_id
