import os
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header

from al1s.bots.qq_identity.contracts import QqIdentityError, QqIdentitySave, QqIdentityState
from al1s.bots.qq_identity.repository import QqIdentityRepository
from al1s.meme_workshop.runtime_identity import read_runtime_identity

REVISION = r'^(?:[a-f0-9]{64}|"[a-f0-9]{64}")$'
RevisionHeader = Annotated[str, Header(alias="If-Match", pattern=REVISION)]
RequestHeader = Annotated[UUID, Header(alias="Idempotency-Key")]


def native_version() -> str | None:
    return read_runtime_identity().native_version


def identity_router(repository: QqIdentityRepository | None = None) -> APIRouter:
    store = repository or QqIdentityRepository(Path("/app/llbot/data"))
    router = APIRouter(prefix="/api/v1/bots/qq/device-identity")

    @router.get("")
    def read() -> QqIdentityState:
        return store.read()

    @router.put("")
    def save(body: QqIdentitySave, revision: RevisionHeader, request_id: RequestHeader
             ) -> QqIdentityState:
        if os.environ.get("PROTOCOL_MODE") == "pmhq":
            raise QqIdentityError("qq_device_settings_direct_only", 409)
        if body.settings.device_name and native_version() != "8.3.2":
            raise QqIdentityError("qq_device_name_override_unsupported", 422)
        return store.save(body, revision.strip('"'), request_id)

    return router
