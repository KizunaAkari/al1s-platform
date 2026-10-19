from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from al1s.bots.qq_identity.contracts import QqIdentitySummary
from al1s.components.contracts import ComponentRelease


@dataclass(frozen=True, slots=True)
class NativeComponent:
    component: str
    version: str | None
    architecture: str | None
    status: str
    observed_at: datetime | None
    image: str | None = None
    image_id: str | None = None
    source_identity: str | None = None
    storage_layout: Literal["legacy", "bundle"] | None = None
    runtime_version: str | None = None
    qq_identity: QqIdentitySummary | None = None
    qq_protocol_mode: Literal["direct", "pmhq"] = "direct"
    qq_client_version: str | None = None
    pmhq_version: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalProfile:
    terminal_id: UUID
    display_name: str
    status: str
    agent_version: str
    architecture: str | None
    observed_at: datetime | None
    details: dict[str, Any]
    last_seen_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ComponentVerificationJob:
    release: ComponentRelease
    candidate_blob_id: UUID | None
    destination_key: str | None
