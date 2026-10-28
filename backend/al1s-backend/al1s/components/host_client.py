import json
import ssl
import time
from datetime import datetime
from typing import Any, Literal
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from pydantic import Field, ValidationError

from al1s.bots.qq_identity.contracts import QqIdentitySummary
from al1s.components.bot_contracts import BuiltQqCandidate
from al1s.components.contracts import Contract, UpgradeUnit
from al1s.components.types import NativeComponent
from al1s.execution.host_management import HostManagerConnection
from al1s.infrastructure.direct_http import NoRedirect


class HostInventoryItem(Contract):
    component: UpgradeUnit
    version: str | None = Field(default=None, max_length=128)
    architecture: str | None = Field(default=None, max_length=32)
    status: str = Field(max_length=16)
    observed_at: datetime | None = None
    image: str | None = Field(default=None, max_length=256)
    image_id: str | None = Field(default=None, max_length=71)
    source_identity: str | None = Field(default=None, max_length=64)
    storage_layout: Literal["legacy", "bundle"] | None = None
    runtime_version: str | None = None
    qq_identity: QqIdentitySummary | None = None
    qq_protocol_mode: Literal["direct", "pmhq"] = "direct"
    qq_quant_v1: bool = False
    qq_client_version: str | None = Field(default=None, max_length=64)
    pmhq_version: str | None = Field(default=None, max_length=64)


class ComponentHostClient:
    def __init__(self, connection: HostManagerConnection | None):
        self.connection = connection

    def request(self, path: str, body: dict[str, object] | None = None) -> Any:
        if self.connection is None:
            raise OSError("component_host_unconfigured")
        context = ssl.create_default_context(cafile=self.connection.ca_file)
        opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=context))
        request = Request(
            self.connection.url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": "Bearer " + self.connection.token.get_secret_value(),
                "Content-Type": "application/json",
            },
        )
        try:
            with opener.open(request, timeout=15) as response:
                raw = response.read(128 * 1024 + 1)
            if len(raw) > 128 * 1024:
                raise ValueError("component_host_response_limit")
            return json.loads(raw)
        except (OSError, ValueError):
            raise OSError("component_host_unavailable") from None

    def inventory(self) -> list[NativeComponent]:
        result = self.request("/inventory")
        values = result.get("items") if isinstance(result, dict) else None
        if not isinstance(values, list) or len(values) > 8:
            raise ValueError("component_host_inventory_invalid")
        rows = []
        for value in values:
            try:
                item = HostInventoryItem.model_validate(value)
                rows.append(NativeComponent(**item.model_dump()))
            except ValidationError:
                continue
        return rows

    def verify_release(self, identity: UUID) -> str:
        result = self.request("/verify-release", {"release_id": str(identity)})
        generation = result.get("generation") if isinstance(result, dict) else None
        if type(generation) is not int or not 1 <= generation <= 2147483647:
            raise ValueError("component_probe_generation_invalid")
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            native = result.get("native_version") if isinstance(result, dict) else None
            if isinstance(native, str) and len(native) <= 64:
                return native
            if not isinstance(result, dict) or result.get("state") == "failed":
                raise ValueError("component_native_version_unavailable")
            time.sleep(1)
            result = self.request(f"/verifications/{identity}?generation={generation}")
        raise ValueError("component_native_probe_timeout")

    def prepare_qq_release(self, identity: UUID) -> BuiltQqCandidate:

        result = self.request("/qq-candidates/prepare", {"release_id": str(identity)})
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            if result.get("state") == "completed":
                return BuiltQqCandidate.model_validate(result["result"])
            if result.get("state") == "failed":
                raise ValueError("qq_candidate_preparation_failed")
            time.sleep(2)
            result = self.request(f"/qq-candidates/{identity}")
        raise ValueError("qq_candidate_preparation_timeout")

    def submit(self, operation_id: UUID) -> dict[str, object]:
        result = self.request("/operations", {"operation_id": str(operation_id)})
        if not isinstance(result, dict):
            raise OSError("component_host_receipt_invalid")
        return result

    def status(self, operation_id: UUID) -> dict[str, object]:
        result = self.request(f"/operations/{operation_id}")
        if not isinstance(result, dict):
            raise OSError("component_host_receipt_invalid")
        return result

    def reconcile(self, operation_id: UUID) -> dict[str, object]:
        result = self.request(f"/operations/{operation_id}/reconcile", {})
        if not isinstance(result, dict):
            raise OSError("component_host_receipt_invalid")
        return result
