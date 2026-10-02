from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel, Field

from al1s.app.service_state import service_state
from al1s.notifications.discord_forward_rules import DiscordForwardRuleService
from al1s.notifications.discord_forward_types import ActionInput, RuleInput

router = APIRouter()


class ActionBody(BaseModel):
    channel_id: UUID
    kind: Literal["qq_private", "qq_group", "smtp"]
    target: str = Field(min_length=1, max_length=320)
    review_policy: Literal["none", "lexicon"]


class RuleBody(BaseModel):
    bot_service_id: UUID
    name: str = Field(min_length=1, max_length=100)
    guild_id: str = Field(min_length=17, max_length=20)
    channel_id: str = Field(min_length=17, max_length=20)
    trigger_kind: Literal["contains", "acrostic", "frequency"]
    trigger_text: str | None = Field(default=None, max_length=256)
    frequency_count: int | None = Field(default=None, ge=2, le=64)
    frequency_window_seconds: int | None = Field(default=None, ge=1, le=3600)
    cooldown_seconds: int | None = Field(default=None, ge=0, le=3600)
    enabled: bool = True
    actions: list[ActionBody] = Field(min_length=1, max_length=20)


def _service(request: Request) -> DiscordForwardRuleService:
    return service_state(request).discord_forward_rules


def _data(body: RuleBody) -> RuleInput:
    return RuleInput(
        bot_service_id=body.bot_service_id,
        name=body.name,
        guild_id=body.guild_id,
        channel_id=body.channel_id,
        trigger_kind=body.trigger_kind,
        trigger_text=body.trigger_text,
        frequency_count=body.frequency_count,
        frequency_window_seconds=body.frequency_window_seconds,
        cooldown_seconds=body.cooldown_seconds,
        enabled=body.enabled,
        actions=tuple(ActionInput(**item.model_dump()) for item in body.actions),
    )


@router.get("/notifications/discord-forward-rules")
def list_rules(
    request: Request,
    bot_service_id: UUID,
    after_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=50),
) -> dict[str, object]:
    rows = _service(request).list_page(bot_service_id, after_id=after_id, limit=limit)
    return {"items": rows, "next_cursor": rows[-1]["id"] if len(rows) == limit else None}


@router.post("/notifications/discord-forward-rules", status_code=201)
def create(body: RuleBody, request: Request) -> dict[str, object]:
    return _service(request).save(_data(body))


@router.put("/notifications/discord-forward-rules/{rule_id}")
def update(
    rule_id: UUID,
    body: RuleBody,
    request: Request,
    if_match: Annotated[int, Header(alias="If-Match", ge=1)],
) -> dict[str, object]:
    return _service(request).save(_data(body), rule_id=rule_id, expected_version=if_match)


@router.delete("/notifications/discord-forward-rules/{rule_id}", status_code=204)
def delete(
    rule_id: UUID,
    request: Request,
    if_match: Annotated[int, Header(alias="If-Match", ge=1)],
) -> Response:
    _service(request).delete(rule_id, if_match)
    return Response(status_code=204)


@router.get("/bots/worker/forward-rules")
def worker_rules(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    credential = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    service_id = service_state(request).bot_service.worker_service_id(credential)
    return {"items": _service(request).enabled_for_worker(service_id)}
