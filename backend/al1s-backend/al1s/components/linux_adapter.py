import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from al1s.components.contracts import ComponentInventoryEntry, ComponentTarget
from al1s.components.host_client import ComponentHostClient
from al1s.components.inventory import terminal_entries
from al1s.components.operations import OperationStore
from al1s.execution.errors import ConflictError
from al1s.execution.host_management import UpgradeRequest
from al1s.execution.maintenance_service import MaintenanceService


class CompositeUpgradeHost:
    def __init__(
        self, store: OperationStore, platform: ComponentHostClient, linux: MaintenanceService
    ):
        self.store, self.platform, self.linux = store, platform, linux

    def available(self, target: ComponentTarget) -> bool:
        return (
            self.platform.connection is not None
            if target.kind in {"platform", "bot"}
            else target.terminal_id in self.linux.connections
        )

    @staticmethod
    def fingerprint(current: ComponentInventoryEntry, health: dict[str, object]) -> str:
        fields = {key: health[key] for key in ("boot_id", "container_id", "container_started_at")}
        if not all(isinstance(value, str) and value for value in fields.values()):
            raise ValueError("component_terminal_identity_unavailable")
        return hashlib.sha256(
            json.dumps({"profile": current.source_identity, **fields}, sort_keys=True).encode()
        ).hexdigest()

    def resolve_source(self, current: ComponentInventoryEntry) -> ComponentInventoryEntry:
        terminal = current.target.terminal_id
        if terminal is None:
            return current
        try:
            health = self.linux.client(terminal).request("/v1/health")
            if health.get("healthy") is not True or health.get("unresolved_upgrade_id"):
                raise ValueError("component_terminal_not_ready")
            return current.model_copy(update={"source_identity": self.fingerprint(current, health)})
        except Exception:
            return current.model_copy(update={"status": "unknown", "source_identity": None})

    def submit(self, identity: UUID) -> dict[str, object]:
        operation = self.store.get_operation(identity)
        terminal = operation.target.terminal_id
        if terminal is None:
            return self.platform.submit(identity)
        if self.linux.store.get(terminal, identity) is not None:
            return self.status(identity)
        health = self.linux.client(terminal).request("/v1/health")
        profile = self.store.get_linux_profile(terminal)
        if (
            profile is None
            or self.fingerprint(terminal_entries(profile)[0], health) != operation.expected_source
        ):
            raise ConflictError(
                "component_source_changed", "Terminal instance changed after preflight"
            )
        request = UpgradeRequest(
            command_id=identity,
            action="upgrade_container",
            release_id=operation.release_id,
            expires_at=(operation.created_at + timedelta(minutes=10)).timestamp(),
            confirm_interrupt=True,
            expected_boot_id=health["boot_id"],
            expected_container_id=health["container_id"],
            expected_container_started_at=health["container_started_at"],
            confirmed_upgrade_id=health.get("unresolved_upgrade_id"),
        )
        self.linux.submit(terminal, request)
        return {
            "operation_id": str(identity),
            "state": "accepted",
            "stage": "terminal_command_submitted",
            "proof": False,
        }

    def status(self, identity: UUID) -> dict[str, object]:
        operation = self.store.get_operation(identity)
        if operation.target.terminal_id is None:
            return self.platform.status(identity)
        record = self.linux.get(operation.target.terminal_id, identity)
        reported = record.late_state if record.late_state == "succeeded" else record.state
        state = {
            "pending": "accepted",
            "accepted": "preparing",
            "executing": "switching",
            "recovering": "verifying",
            "succeeded": "succeeded",
            "refused": "failed_safe",
            "expired": "failed_safe",
            "cancelled": "failed_safe",
        }.get(reported, "unknown")
        if state == "succeeded":
            release = self.store.get_release(operation.release_id)
            terminal = operation.target.terminal_id
            assert terminal is not None
            profile = self.store.get_linux_profile(terminal)
            maa = profile.details.get("maa") if profile else None
            if (
                profile is None
                or profile.status != "online"
                or profile.last_seen_at is None
                or record.completed_at is None
                or profile.last_seen_at < record.completed_at
                or (
                    release.maa_version
                    and (
                        not isinstance(maa, dict)
                        or maa.get("version") != release.maa_version
                        or maa.get("execution_ready") is not True
                    )
                )
            ):
                state = "verifying"
        return {
            "operation_id": str(identity),
            "state": state,
            "stage": record.state,
            "proof": state in {"succeeded", "failed_safe"},
            "error_code": record.error_code,
        }

    def reconcile(self, identity: UUID) -> dict[str, object]:
        operation = self.store.get_operation(identity)
        terminal = operation.target.terminal_id
        if terminal is None:
            return self.platform.reconcile(identity)
        health = self.linux.client(terminal).request("/v1/health")
        unresolved = health.get("unresolved_upgrade_id")
        if unresolved and unresolved != str(identity):
            raise ConflictError(
                "component_recovery_identity", "A different terminal upgrade is unresolved"
            )
        if (
            self.linux.store.get(terminal, identity) is None
            and (datetime.now(UTC) - operation.created_at).total_seconds() > 615
        ):
            return {
                "operation_id": str(identity),
                "state": "failed_safe",
                "stage": "command_not_submitted_expired",
                "proof": True,
            }
        result = self.linux.client(terminal).request(
            "/v1/upgrades/recover", {"deployment_id": str(identity)}
        )
        if (
            result.get("deployment_id") == str(identity)
            and result.get("resolved") is True
            and result.get("status") in {"failed", "rolled_back"}
        ):
            return {
                "operation_id": str(identity),
                "state": "failed_safe",
                "stage": "terminal_deployment_reconciled",
                "proof": True,
            }
        self.linux.poll_once()
        return self.status(identity)
