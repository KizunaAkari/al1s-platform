from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel

from al1s.api.terminal_auth import authenticated_terminal_id
from al1s.app.service_state import service_state

router = APIRouter()


class TerminalMqttSessionResponse(BaseModel):
    session_id: UUID
    broker_host: str
    broker_port: int
    tls_enabled: bool
    client_id: str
    username: str
    password: str
    topic: str
    qos: int
    issued_at: datetime
    expires_at: datetime


@router.post("/mqtt-sessions", response_model=TerminalMqttSessionResponse)
def issue_mqtt_session(
    request: Request,
    authorization: str | None = Header(default=None),
) -> TerminalMqttSessionResponse:
    terminal_id = authenticated_terminal_id(request, authorization)
    service = service_state(request).terminal_mqtt_sessions
    grant = service.issue(terminal_id)
    return TerminalMqttSessionResponse(
        session_id=grant.session_id,
        broker_host=grant.broker_host,
        broker_port=grant.broker_port,
        tls_enabled=grant.tls_enabled,
        client_id=grant.client_id,
        username=grant.username,
        password=grant.password,
        topic=grant.topic,
        qos=grant.qos,
        issued_at=grant.issued_at,
        expires_at=grant.expires_at,
    )
