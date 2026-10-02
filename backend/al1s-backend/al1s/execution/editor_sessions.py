"""Editor control-plane rules, separate from formal attempts and their results."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from al1s.execution.errors import ConflictError, InvalidRequestError, NotFoundError
from al1s.secrets.security import FernetSecretCipher, SecretUnavailableError


class EditorStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"
    EXPIRED = "expired"


FINAL_EDITOR_STATES = {EditorStatus.CLOSED, EditorStatus.FAILED, EditorStatus.EXPIRED}


@dataclass(frozen=True)
class EditorSession:
    session_id: UUID
    terminal_id: UUID
    device_id: UUID
    request_id: UUID
    status: EditorStatus
    created_at: datetime
    create_deadline: datetime
    updated_at: datetime
    row_version: int = 1
    ciphertext: str | None = None
    key_id: str | None = None
    terminal_instance_id: UUID | None = None
    error_code: str | None = None


def change(session: EditorSession, now: datetime, **fields: Any) -> EditorSession:
    return replace(session, updated_at=now, row_version=session.row_version + 1, **fields)


def expire_pending(session: EditorSession, now: datetime) -> EditorSession:
    if session.status is EditorStatus.PENDING and now >= session.create_deadline:
        return change(session, now, status=EditorStatus.EXPIRED)
    return session


def request_close(session: EditorSession, now: datetime) -> EditorSession:
    session = expire_pending(session, now)
    if session.status is EditorStatus.PENDING:
        return change(session, now, status=EditorStatus.CLOSED)
    if session.status is EditorStatus.ACTIVE:
        return change(session, now, status=EditorStatus.CLOSING)
    return session


def validate_connection(connection: dict[str, str]) -> None:
    required = {"transport", "session_token", "video_ws_url", "control_ws_url", "scrcpy_version"}
    optional = {"screenshot_ws_url", "foreground_ws_url", "app_icon_ws_url", "ocr_ws_url"}
    if (not required <= set(connection) or set(connection) - required - optional
            or connection["transport"] != "scrcpy-managed-v1"):
        raise InvalidRequestError("invalid_editor_connection", "Unsupported editor connection")
    token = connection["session_token"]
    if not 32 <= len(token) <= 128 or not all(
        c.isascii() and (c.isalnum() or c in "-_") for c in token
    ):
        raise InvalidRequestError("invalid_editor_token", "Invalid editor token")
    origins = set()
    channels = ["video", "control"]
    if "screenshot_ws_url" in connection:
        channels.append("screenshot")
    if "foreground_ws_url" in connection:
        channels.append("foreground")
    if "app_icon_ws_url" in connection:
        channels.append("app_icon")
    if "ocr_ws_url" in connection:
        channels.append("ocr")
    for channel in channels:
        raw = connection[f"{channel}_ws_url"]
        if len(raw) > 2048 or any(ord(c) < 33 for c in raw):
            raise InvalidRequestError("invalid_editor_url", "Invalid editor URL")
        try:
            url = urlsplit(raw)
            _ = url.port
        except ValueError as exc:
            raise InvalidRequestError("invalid_editor_url", "Invalid editor URL") from exc
        if (
            url.scheme not in {"ws", "wss"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path != f"/scrcpy/{token}/{channel.replace('_', '-')}"
        ):
            raise InvalidRequestError("invalid_editor_url", "Invalid editor URL")
        origins.add((url.scheme, url.netloc))
    if len(origins) != 1 or connection["scrcpy_version"] != "3.3.4":
        raise InvalidRequestError("invalid_editor_connection", "Inconsistent editor connection")


def open_connection(session: EditorSession, cipher: FernetSecretCipher) -> dict[str, str] | None:
    if session.status is not EditorStatus.ACTIVE:
        return None
    if session.ciphertext is None or session.key_id is None:
        raise SecretUnavailableError("Editor connection unavailable")
    body = json.loads(cipher.decrypt(session.ciphertext, session.key_id))
    identity = [str(session.session_id), str(session.terminal_id), str(session.device_id)]
    if body.get("identity") != identity:
        raise SecretUnavailableError("Editor connection identity mismatch")
    connection: dict[str, str] = body["connection"]
    validate_connection(connection)
    return connection


def report_session(
    session: EditorSession,
    terminal_id: UUID,
    instance_id: UUID,
    status: EditorStatus,
    now: datetime,
    cipher: FernetSecretCipher,
    connection: dict[str, str] | None = None,
) -> EditorSession:
    if session.terminal_id != terminal_id:
        raise NotFoundError("editor_session")
    session = expire_pending(session, now)
    if session.status in FINAL_EDITOR_STATES:
        return session
    if status is EditorStatus.ACTIVE:
        return _activate(session, instance_id, now, cipher, connection)
    if status not in {EditorStatus.CLOSED, EditorStatus.FAILED}:
        raise InvalidRequestError("invalid_editor_report", "Invalid editor report state")
    if session.status is EditorStatus.CLOSING and status is not EditorStatus.CLOSED:
        return session
    return change(
        session,
        now,
        status=status,
        ciphertext=None,
        key_id=None,
        error_code="terminal_editor_failed" if status is EditorStatus.FAILED else None,
    )


def _activate(
    session: EditorSession,
    instance_id: UUID,
    now: datetime,
    cipher: FernetSecretCipher,
    connection: dict[str, str] | None,
) -> EditorSession:
    if session.status is EditorStatus.CLOSING:
        return session
    if connection is None:
        raise InvalidRequestError("editor_connection_required", "Connection is required")
    validate_connection(connection)
    if session.status is EditorStatus.ACTIVE:
        if (
            session.terminal_instance_id != instance_id
            or open_connection(session, cipher) != connection
        ):
            raise ConflictError("editor_report_conflict", "Editor connection already activated")
        return session
    body = json.dumps(
        {
            "identity": [str(session.session_id), str(session.terminal_id), str(session.device_id)],
            "connection": connection,
        },
        separators=(",", ":"),
    )
    return change(
        session,
        now,
        status=EditorStatus.ACTIVE,
        ciphertext=cipher.encrypt(body),
        key_id=cipher.key_id,
        terminal_instance_id=instance_id,
    )
