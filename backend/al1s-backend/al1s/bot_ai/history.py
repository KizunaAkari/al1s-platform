import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VisionJob(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt_id: UUID
    created_at: float = Field(ge=0)
    boot_id: str | None = Field(default=None, min_length=1, max_length=64)
    execution_hash: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    registration_state: Literal['pending', 'confirmed', 'unknown', 'rejected'] | None = None
    transport_started: bool = False

    @model_validator(mode='after')
    def validate_resource_state(self):
        managed = (self.boot_id, self.execution_hash, self.registration_state)
        if any(value is not None for value in managed):
            if (any(value is None for value in managed) or (self.transport_started and self.registration_state == 'pending')
                    or (self.registration_state == 'rejected' and not self.transport_started)):
                raise ValueError('ai_vision_job_invalid')
        elif self.transport_started:
            raise ValueError('ai_vision_job_invalid')
        return self


@dataclass(frozen=True)
class HistoryContext:
    id: UUID
    revision: int
    service_id: UUID
    scope: str
    target: str
    messages: list[dict]


def digest(messages: list[dict]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def parse_origin(value: str) -> tuple[UUID, str, str]:
    matched = re.fullmatch(r'al1s_qq_([0-9a-f]{32}):(GroupMessage|FriendMessage):([1-9][0-9]{0,19})', value)
    if not matched:
        raise ValueError('ai_history_origin_invalid')
    service, scope, target = matched.groups()
    return UUID(service), 'qq_group' if scope == 'GroupMessage' else 'qq_private', target


def split_turns(messages: list[dict]) -> list[list[dict]]:
    turns, pending = [], set()
    for message in messages:
        role = message.get('role')
        if role not in {'user', 'assistant', 'tool', 'system'}:
            raise ValueError('ai_history_role_invalid')
        if role == 'user':
            if pending:
                raise ValueError('ai_history_tool_incomplete')
            turns.append([])
        if not turns:
            raise ValueError('ai_history_round_unknown')
        if role == 'tool':
            identity = message.get('tool_call_id')
            if identity not in pending:
                raise ValueError('ai_history_tool_orphan')
            pending.remove(identity)
        elif pending:
            raise ValueError('ai_history_tool_incomplete')
        if role == 'assistant':
            for call in message.get('tool_calls') or []:
                identity = call.get('id')
                if not isinstance(identity, str) or not identity or identity in pending:
                    raise ValueError('ai_history_tool_invalid')
                pending.add(identity)
        turns[-1].append(message)
    if pending:
        raise ValueError('ai_history_tool_incomplete')
    return turns
