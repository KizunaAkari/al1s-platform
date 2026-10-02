import hashlib
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from al1s.execution.errors import ConflictError, ExecutionDomainError, NotFoundError
from al1s.execution.host_management import (
    CancelMaintenanceRequest,
    HostCommand,
    HostManagerClient,
    HostManagerConnection,
    RestartRequest,
    UpgradeRequest,
)
from al1s.execution.maintenance_types import MaintenanceRecord


class MaintenanceStore(Protocol):
    def latest(self, terminal: UUID) -> MaintenanceRecord | None: ...
    def get(self, terminal: UUID, identity: UUID) -> MaintenanceRecord | None: ...
    def reserve(self, value: MaintenanceRecord) -> tuple[MaintenanceRecord, bool]: ...
    def due(self, now: datetime) -> list[MaintenanceRecord]: ...
    def observe(
        self,
        terminal: UUID,
        identity: UUID,
        now: datetime,
        remote: HostCommand | None = None,
        *,
        refused: bool = False,
    ) -> MaintenanceRecord: ...


class MaintenanceService:
    def __init__(
        self,
        store: MaintenanceStore,
        connections: Mapping[UUID, HostManagerConnection],
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.store, self.connections = store, connections
        self.now = now or (lambda: datetime.now(UTC))

    def client(self, terminal: UUID) -> HostManagerClient:
        connection = self.connections.get(terminal)
        if connection is None:
            raise ExecutionDomainError(
                "host_management_not_configured",
                "Independent host management is not configured",
                503,
            )
        return HostManagerClient(connection)

    def submit(self, terminal: UUID, body: RestartRequest | UpgradeRequest) -> MaintenanceRecord:
        digest = hashlib.sha256(body.model_dump_json(exclude_none=True).encode()).hexdigest()
        existing = self.store.get(terminal, body.command_id)
        if existing:
            if existing.request_hash != digest:
                raise ConflictError("command_identity_conflict", "Command identity changed")
            return existing  # Never POST an unknown previous command again.
        now = self.now()
        expires = datetime.fromtimestamp(body.expires_at, UTC)
        if not now < expires <= now + timedelta(seconds=600):
            raise ConflictError("command_deadline_invalid", "Command deadline is invalid")
        client = self.client(terminal)
        health = client.request("/v1/health")
        if health.get("upgrade_in_progress"):
            raise ConflictError("upgrade_in_progress", "Upgrade is still executing")
        if isinstance(body, UpgradeRequest) and health.get("unresolved_upgrade_id"):
            raise ConflictError("previous_upgrade_unresolved", "Resolve the previous upgrade first")
        if health.get("unresolved_upgrade_id") != body.confirmed_upgrade_id:
            raise ConflictError(
                "upgrade_risk_confirmation_required",
                "Confirm the current unresolved upgrade risk",
            )
        if (
            health["boot_id"] != body.expected_boot_id
            or health["container_id"] != body.expected_container_id
            or health["container_started_at"] != body.expected_container_started_at
        ):
            raise ConflictError("host_identity_changed", "Refresh host identity before restarting")
        value, created = self.store.reserve(
            MaintenanceRecord(
                body.command_id,
                terminal,
                body.action,
                digest,
                "pending",
                now,
                expires,
                None,
                None,
                None,
                0,
                None,
                body.release_id if isinstance(body, UpgradeRequest) else None,
            )
        )
        if not created:
            return value
        try:
            response = client.request("/v1/commands", body.model_dump(mode="json"))
            remote = HostCommand.model_validate(response)
        except ExecutionDomainError as exc:
            return self.store.observe(
                terminal, body.command_id, self.now(), refused=exc.status_code == 409
            )
        return self.store.observe(terminal, body.command_id, self.now(), remote)

    def get(self, terminal: UUID, identity: UUID) -> MaintenanceRecord:
        value = self.store.get(terminal, identity)
        if value is None:
            raise NotFoundError("maintenance_command")
        return self.store.observe(terminal, identity, self.now())

    def cancel(self, terminal: UUID, body: CancelMaintenanceRequest) -> MaintenanceRecord:
        current = self.get(terminal, body.command_id)
        if current.state == "cancelled":
            return current
        cancellable = current.state == "accepted" or (
            current.state == "executing" and current.action == "upgrade_container"
            and current.error_code in {"upgrade_downloading", "upgrade_cancel_requested"}
        )
        if not cancellable or current.remote_version != body.version:
            raise ConflictError("maintenance_not_cancellable", "Refresh command state")
        response = self.client(terminal).request(
            "/v1/commands/cancel", body.model_dump(mode="json"),
        )
        return self.store.observe(
            terminal, body.command_id, self.now(), HostCommand.model_validate(response),
        )

    def latest(self, terminal: UUID) -> MaintenanceRecord | None:
        value = self.store.latest(terminal)
        return self.get(terminal, value.command_id) if value else None

    def poll_once(self) -> int:
        commands = self.store.due(self.now())
        if not commands:
            return 0
        # Reads only; four slow/offline hosts cannot serialize four network timeouts.
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self._poll, commands))
        return len(commands)

    def _poll(self, command: MaintenanceRecord) -> None:
        remote = None
        try:
            response = self.client(command.terminal_id).request(
                f"/v1/commands/{command.command_id}"
            )
            remote = HostCommand.model_validate(response)
        except ExecutionDomainError:
            pass  # Unknown/404 never triggers another POST or resets a deadline.
        self.store.observe(command.terminal_id, command.command_id, self.now(), remote)
