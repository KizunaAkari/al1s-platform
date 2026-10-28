import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from al1s.components.contracts import (
    ComponentInventoryEntry,
    ComponentOperation,
    ComponentPreflight,
    ComponentRelease,
    ComponentReleaseInput,
    ComponentTarget,
    ComponentUpgradeRequest,
    OperationPage,
)
from al1s.components.inventory import ComponentInventoryService
from al1s.components.status_capability import StatusCapability
from al1s.components.types import TerminalProfile
from al1s.execution.errors import ConflictError

SERVICES = {
    "platform": ["backend"],
    "postgresql": ["backend", "postgres"],
    "s3": ["backend", "seaweed-master", "seaweed-volume", "seaweed-filer", "seaweed-s3"],
    "mqtt": ["backend", "mosquitto"],
    "linux-terminal": ["terminal-agent"],
    "qq-bot": ["llbot"],
    "discord-bot": ["discord-worker"],
}
FINAL = {"succeeded", "failed_safe"}


def identity_runtime_version_supported(value: str | None) -> bool:
    if value is None or not re.fullmatch(r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}", value):
        return False
    return tuple(int(part) for part in value.split(".")) >= (1, 1, 0)


def compatible_release(
    current: ComponentInventoryEntry, release: ComponentReleaseInput
) -> list[str]:
    reasons = []
    summary = current.qq_identity
    headed = getattr(current, "qq_protocol_mode", "direct") == "pmhq"
    if release.component == "qq-bot" and headed and release.runtime_version not in {
        "2.0.0", "2.0.1", "2.1.0"
    }:
        reasons.append("qq_headed_runtime_required")
    if (release.component == "qq-bot" and getattr(current, "qq_quant_v1", False)
            and release.runtime_version != "2.1.0"):
        reasons.append("qq_quant_runtime_required")
    if release.component == "qq-bot" and not headed and summary is not None:
        if summary.pending:
            reasons.append("qq_identity_pending_restart")
        if summary.failed:
            reasons.append("qq_identity_apply_unresolved")
        if summary.configured and not identity_runtime_version_supported(release.runtime_version):
            reasons.append("qq_identity_runtime_unsupported")
        if summary.custom_name and release.native_version != "8.3.2":
            reasons.append("qq_device_name_override_unsupported")
    if release.component == "s3":
        bundled = (release.candidate_image or "").split(":", 1)[0] == "al1s-platform-storage"
        if bundled != (current.storage_layout == "bundle"):
            reasons.append("component_storage_layout_mismatch")
    if current.upgrade_unit != release.component:
        reasons.append("component_mismatch")
    if (
        current.status != "healthy" and current.target.kind != "bot"
    ) or not current.source_identity:
        reasons.append("component_not_ready")
    if current.architecture != release.architecture:
        reasons.append("architecture_mismatch")
    sources = release.compatible_from.split(",")
    version = current.version or ""
    if not any(version == item or version.startswith(item + ".") for item in sources):
        reasons.append("source_version_incompatible")
    if (
        release.component == "postgresql"
        and version.split(".")[0] != release.native_version.split(".")[0]
    ):
        reasons.append("postgres_major_migration_required")
    if (
        release.component in {"s3", "mqtt"}
        and version.split(".")[0] != release.native_version.split(".")[0]
    ):
        reasons.append("component_data_migration_required")
    return reasons


def receipt_transition(current: str, received: str, proof: bool) -> str:
    sequence = {"accepted": 0, "preparing": 1, "switching": 2, "verifying": 3}
    if current in FINAL and received != current:
        raise ConflictError("component_result_final", "Completed upgrade result cannot regress")
    if received in FINAL and not proof:
        raise ConflictError("component_result_unproven", "Upgrade result needs deployment evidence")
    if current == "unknown" and received not in FINAL | {"unknown"}:
        raise ConflictError(
            "component_result_unknown", "Reconcile actual deployment before continuing"
        )
    if current in sequence and received in sequence and sequence[received] < sequence[current]:
        raise ConflictError(
            "component_receipt_stale", "Old upgrade receipt cannot replace current stage"
        )
    if received not in sequence and received not in FINAL | {"unknown"}:
        raise ConflictError("component_receipt_invalid", "Unknown upgrade stage")
    return received


class OperationStore(Protocol):
    def get_linux_profile(self, identity: UUID) -> TerminalProfile | None: ...
    def active_operations(self) -> list[ComponentOperation]: ...
    def list_operations(
        self, limit: int = 25, cursor: UUID | None = None, scope: str | None = None
    ) -> OperationPage: ...
    def find_operation(self, scope: str, key: str) -> tuple[ComponentOperation, str] | None: ...
    def get_release(self, identity: UUID) -> ComponentRelease: ...
    def reserve_operation(
        self, request: ComponentUpgradeRequest, request_hash: str, now: datetime
    ) -> ComponentOperation: ...
    def get_operation(self, identity: UUID) -> ComponentOperation: ...
    def update_operation(
        self, identity: UUID, receipt: dict[str, object], now: datetime
    ) -> ComponentOperation: ...
    def blockers(self, target: ComponentTarget) -> bool: ...


class UpgradeHost(Protocol):
    def submit(self, operation_id: UUID) -> dict[str, object]: ...
    def status(self, operation_id: UUID) -> dict[str, object]: ...
    def reconcile(self, operation_id: UUID) -> dict[str, object]: ...


class ComponentUpgradeService:
    def __init__(
        self,
        store: OperationStore,
        inventory: ComponentInventoryService,
        host: UpgradeHost,
        signer: StatusCapability,
        now: Callable[[], datetime] | None = None,
        available: Callable[[ComponentTarget], bool] | None = None,
        resolve_source: Callable[[ComponentInventoryEntry], ComponentInventoryEntry] | None = None,
    ):
        self.store, self.inventory, self.host, self.signer = store, inventory, host, signer
        self.now = now or (lambda: datetime.now(UTC))
        self.available = available or (lambda _target: True)
        self.resolve_source = resolve_source or (lambda row: row)

    def preflight(self, target: ComponentTarget, release_id: UUID) -> ComponentPreflight:
        release = self.store.get_release(release_id)
        bot = release.component in {"qq-bot", "discord-bot"}
        if bot != (target.kind == "bot") or (
            bot and release.component != f"{target.bot_alias}-bot"
        ):
            raise ConflictError("component_target_mismatch", "Version target does not match Bot")
        if (release.component == "linux-terminal") != (target.kind == "terminal"):
            raise ConflictError(
                "component_target_mismatch", "Version target does not match component"
            )
        current = self._current(target, release.component)
        reasons = compatible_release(current, release)
        if release.state != "published":
            reasons.append("component_version_unpublished")
        if not self.available(target):
            reasons.append("component_host_unconfigured")
        if self.store.blockers(target):
            reasons.append("component_active_work")
        return ComponentPreflight(
            component=release.component,
            target=target,
            release_id=release_id,
            expected_source=current.source_identity or "",
            current_version=current.version,
            target_version=release.native_version,
            allowed=not reasons,
            reasons=reasons,
            affected_services=(
                ["backend", "storage"]
                if release.component == "s3" and current.storage_layout == "bundle"
                else SERVICES[release.component]
            ),
            backup_required=release.component != "linux-terminal",
        )

    def _current(self, target: ComponentTarget, component: str) -> ComponentInventoryEntry:
        row = self.inventory.get(target, component)
        if row is not None:
            return self.resolve_source(row) if self.available(target) else row
        raise ConflictError("component_target_unavailable", "Refresh the component inventory")

    def start(self, request: ComponentUpgradeRequest) -> ComponentOperation:
        request_hash = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        previous = self.store.find_operation(request.target.scope, request.idempotency_key)
        if previous is not None:
            if previous[1] != request_hash:
                raise ConflictError(
                    "component_operation_identity_conflict", "Upgrade identity changed"
                )
            return self._with_status(previous[0])
        preflight = self.preflight(request.target, request.release_id)
        if (
            request.component != preflight.component
            or request.expected_source != preflight.expected_source
        ):
            raise ConflictError(
                "component_source_changed", "Source component changed; refresh preflight"
            )
        if not preflight.allowed:
            raise ConflictError(preflight.reasons[0], "Upgrade preflight did not pass")
        operation = self.store.reserve_operation(request, request_hash, self.now())
        if operation.state == "accepted":
            try:
                receipt = self.host.submit(operation.operation_id)
            except Exception:
                receipt = {
                    "state": "unknown",
                    "stage": "submission_unconfirmed",
                    "proof": False,
                    "error_code": "component_submission_unconfirmed",
                }
            operation = self.store.update_operation(operation.operation_id, receipt, self.now())
        return self._with_status(operation)

    def _with_status(self, operation: ComponentOperation) -> ComponentOperation:
        return operation.model_copy(
            update={
                "status_token": self.signer.issue(
                    operation.operation_id, self.now() + timedelta(minutes=45)
                )
            }
        )

    def get(self, identity: UUID) -> ComponentOperation:
        return self._with_status(self.store.get_operation(identity))

    def reconcile(self, identity: UUID) -> ComponentOperation:
        current = self.store.get_operation(identity)
        if current.state in FINAL:
            return self._with_status(current)
        receipt = self.host.reconcile(identity)
        return self._with_status(self.store.update_operation(identity, receipt, self.now()))

    def observe(self, identity: UUID) -> ComponentOperation:
        current = self.store.get_operation(identity)
        if current.state in FINAL:
            return current
        try:
            receipt = self.host.status(identity)
        except OSError:
            if (self.now() - current.created_at).total_seconds() < 1800:
                return current
            receipt = {"state": "unknown", "stage": "observation_timeout", "proof": False}
        return self.store.update_operation(identity, receipt, self.now())
