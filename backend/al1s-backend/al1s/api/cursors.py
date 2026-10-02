from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from uuid import UUID

from al1s.execution.errors import InvalidRequestError


def encode_timestamp_cursor(timestamp: datetime, identifier: UUID) -> str:
    payload = json.dumps(
        {"v": 1, "at": timestamp.isoformat(), "id": str(identifier)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_timestamp_cursor(cursor: str | None) -> tuple[datetime | None, UUID | None]:
    if cursor is None:
        return None, None
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
        payload = json.loads(raw)
        if payload.get("v") != 1:
            raise ValueError
        timestamp = datetime.fromisoformat(payload["at"])
        identifier = UUID(payload["id"])
        if timestamp.utcoffset() is None:
            raise ValueError
    except (
        UnicodeEncodeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise InvalidRequestError("invalid_page_cursor", "Page cursor is invalid") from exc
    return timestamp, identifier


def encode_ordinal_cursor(ordinal: int, identifier: UUID) -> str:
    payload = json.dumps(
        {"v": 1, "ordinal": ordinal, "id": str(identifier)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_ordinal_cursor(cursor: str | None) -> tuple[int | None, UUID | None]:
    if cursor is None:
        return None, None
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
        payload = json.loads(raw)
        if payload.get("v") != 1:
            raise ValueError
        ordinal = int(payload["ordinal"])
        identifier = UUID(payload["id"])
        if ordinal < 1:
            raise ValueError
    except (
        UnicodeEncodeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise InvalidRequestError("invalid_page_cursor", "Page cursor is invalid") from exc
    return ordinal, identifier
