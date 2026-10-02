from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, Field

from al1s.api.cursors import decode_timestamp_cursor, encode_timestamp_cursor
from al1s.app.service_state import service_state
from al1s.notifications.review_service import ReviewService

router = APIRouter()


def service(request: Request) -> ReviewService:
    return service_state(request).reviews


class ApproveBody(BaseModel):
    row_version: int = Field(ge=1)
    text: str | None = Field(default=None, min_length=1, max_length=16_000)


class BatchBody(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=50)
    action: Literal["reject", "delete"]


class RuleMatchBody(BaseModel):
    rule_id: UUID
    rule_version: int = Field(ge=1)
    guild_id: str = Field(pattern=r"^[1-9][0-9]{16,19}$")
    channel_id: str = Field(pattern=r"^[1-9][0-9]{16,19}$")
    message_ids: list[str] = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1, max_length=512_000)


@router.get("/notifications/reviews")
def page(
    request: Request,
    state: Literal["hold", "pending", "approved", "rejected", "expired", "completed"] | None = None,
    cursor: str | None = Query(None, max_length=512),
    limit: int = Query(50, ge=1, le=50),
) -> dict[str, object]:
    before, identity = decode_timestamp_cursor(cursor)
    rows = service(request).page(state, before, identity, limit)
    next_cursor = None
    if len(rows) == limit:
        next_cursor = encode_timestamp_cursor(
            datetime.fromisoformat(str(rows[-1]["received_at"])),
            UUID(str(rows[-1]["id"])),
        )
    return {"items": rows, "next_cursor": next_cursor}


@router.get("/notifications/reviews/{review_id}")
def detail(review_id: UUID, request: Request) -> dict[str, object]:
    return service(request).detail(review_id)


@router.post("/notifications/reviews/{review_id}/approve")
def approve(review_id: UUID, body: ApproveBody, request: Request) -> dict[str, object]:
    return service(request).approve(review_id, body.row_version, body.text)


@router.post("/notifications/reviews/batch")
def batch(body: BatchBody, request: Request) -> list[dict[str, object]]:
    return service(request).bulk(body.ids, body.action)


@router.post("/bots/worker/rule-matches", status_code=202)
def receive_rule_match(
    body: RuleMatchBody,
    request: Request,
    authorization: str | None = Header(None),
) -> dict[str, object]:
    credential = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    bot = service_state(request).bot_service
    service_id = bot.worker_service_id(credential)
    return service(request).receive_rule_match(
        service_id,
        body.rule_id,
        body.rule_version,
        body.guild_id,
        body.channel_id,
        body.message_ids,
        body.body,
    )
