"""Batched path observations; fixed read count, target locks serialize all writers."""

from datetime import datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.delivery_models import OfflineStartPermitRow
from al1s.adapters.postgres.device_path_models import DeviceAuthorityRow, DevicePathRow
from al1s.adapters.postgres.editor_models import EditorSessionRow
from al1s.adapters.postgres.execution_models import (
    TargetDeviceIdentifierRow,
    TargetDeviceRow,
    TerminalCapabilityProfileRow,
    TerminalRow,
)
from al1s.adapters.postgres.maa_models import MaaQuickTestSessionRow
from al1s.adapters.postgres.repositories import PostgresAuditRepository, PostgresOutboxRepository
from al1s.adapters.postgres.scheduling_models import ExecutionRow
from al1s.execution.device_authority import DevicePath
from al1s.execution.device_path_service import PathObservation, PathState
from al1s.execution.errors import ConflictError, NotFoundError
from al1s.kernel.types import NewAuditEntry, NewOutboxEvent


class DevicePathTransaction:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions
        self.session: Session
        self.terminal_id: UUID | None = None

    def __enter__(self) -> Self:
        self.session = self._sessions()
        self.session.begin()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.session.commit() if kind is None else self.session.rollback()
        finally:
            self.session.close()

    def observe(
        self, terminal_id: UUID, items: list[PathObservation], now: datetime
    ) -> list[PathState]:
        self.terminal_id = terminal_id
        ids = [i.device_id for i in items]
        self.targets = {
            t.id: t
            for t in self.session.scalars(
                select(TargetDeviceRow)
                .where(
                    TargetDeviceRow.id.in_(ids),
                    TargetDeviceRow.deleted_at.is_(None),
                )
                .order_by(TargetDeviceRow.id)
                .with_for_update()
            )
        }
        terminal = self.session.get(TerminalRow, terminal_id)
        if terminal is None or terminal.deleted_at is not None:
            raise ConflictError("terminal_unavailable", "Terminal is unavailable")
        bindings = set(
            self.session.scalars(
                select(TargetDeviceIdentifierRow.target_device_id).where(
                    TargetDeviceIdentifierRow.target_device_id.in_(ids),
                    TargetDeviceIdentifierRow.source_terminal_id == terminal_id,
                    TargetDeviceIdentifierRow.deleted_at.is_(None),
                    TargetDeviceIdentifierRow.source_type
                    == (
                        "apk_installation" if terminal.terminal_type == "android" else "adb_serial"
                    ),
                )
            )
        )
        if len(self.targets) != len(ids) or set(ids) - bindings:
            raise ConflictError("path_not_associated", "Explicit device association required")
        existing = {
            (p.device_id, p.kind): p
            for p in self.session.scalars(
                select(DevicePathRow).where(DevicePathRow.device_id.in_(ids))
            )
        }
        for item in items:
            key = item.device_id, terminal.terminal_type
            row = existing.get(key)
            if row is None:
                row = DevicePathRow(
                    device_id=item.device_id,
                    kind=terminal.terminal_type,
                    terminal_id=terminal_id,
                    ready_count=0,
                    row_version=1,
                )
                self.session.add(row)
            if row.terminal_id != terminal_id:
                raise ConflictError("path_terminal_conflict", "Connection is already associated")
            previous_fresh = (
                row.instance_id == item.instance_id
                and row.control_ready
                and row.observed_at
                and row.observed_at >= now - timedelta(seconds=30)
            )
            row.ready_count = (
                min(2, row.ready_count + 1)
                if previous_fresh and item.control_ready
                else int(item.control_ready)
            )
            row.instance_id, row.observed_at = item.instance_id, now
            row.control_ready, row.authority_protocol = item.control_ready, 1
            row.row_version += 1
        self.session.flush()
        return self._states(ids, now)

    def _states(self, ids: list[UUID], now: datetime) -> list[PathState]:
        self.authorities = {
            a.device_id: a
            for a in self.session.scalars(
                select(DeviceAuthorityRow)
                .where(DeviceAuthorityRow.device_id.in_(ids))
                .with_for_update()
            )
        }
        paths: dict[UUID, list[DevicePath]] = {device: [] for device in ids}
        rows = self.session.execute(
            select(DevicePathRow, TerminalRow, TerminalCapabilityProfileRow)
            .join(TerminalRow, TerminalRow.id == DevicePathRow.terminal_id)
            .outerjoin(
                TerminalCapabilityProfileRow,
                TerminalCapabilityProfileRow.id == TerminalRow.current_capability_profile_id,
            )
            .where(DevicePathRow.device_id.in_(ids), TerminalRow.deleted_at.is_(None))
        )
        for path, terminal, capability in rows:
            if path.instance_id and path.observed_at and terminal.last_seen_at:
                paths[path.device_id].append(
                    DevicePath(
                        path.terminal_id,
                        path.kind,
                        path.instance_id,
                        path.observed_at,
                        terminal.last_seen_at,
                        path.ready_count,
                        path.control_ready and terminal.acceptance_status == "accepting",
                        path.authority_protocol,
                        frozenset(capability.provider_keys if capability else []),
                    )
                )
        pending = self._pending(ids)
        result = []
        for device in ids:
            authority = self.authorities.get(device)
            if authority is None:
                target = self.targets[device]
                authority = DeviceAuthorityRow(
                    device_id=device,
                    holder_terminal_id=target.managing_terminal_id,
                    generation=1,
                    state="active" if target.managing_terminal_id else "released",
                    require_ocr=False,
                    row_version=1,
                    updated_at=now,
                )
                self.session.add(authority)
                self.authorities[device] = authority
            result.append(
                PathState(
                    device,
                    authority.holder_terminal_id,
                    authority.instance_id,
                    authority.requested_terminal_id,
                    authority.generation,
                    authority.state,
                    paths[device],
                    pending[device][0],
                    pending[device][1],
                    authority.require_ocr,
                )
            )
        return result

    def _pending(self, ids: list[UUID]) -> dict[UUID, tuple[bool, bool]]:
        predicates = [
            select(model.id).where(column == TargetDeviceRow.id, condition).exists()
            for model, column, condition in (
                (
                    ExecutionRow,
                    ExecutionRow.target_device_id,
                    ExecutionRow.status.in_(("waiting", "queued", "running")),
                ),
                (
                    OfflineStartPermitRow,
                    OfflineStartPermitRow.target_device_id,
                    OfflineStartPermitRow.status == "issued",
                ),
                (
                    MaaQuickTestSessionRow,
                    MaaQuickTestSessionRow.target_device_id,
                    MaaQuickTestSessionRow.status.in_(("issued", "claimed")),
                ),
            )
        ]
        editor = (
            select(EditorSessionRow.id)
            .where(
                EditorSessionRow.device_id == TargetDeviceRow.id,
                EditorSessionRow.status.in_(("pending", "active", "closing")),
            )
            .exists()
        )
        return {
            device: (bool(work), bool(editing))
            for device, work, editing in self.session.execute(
                select(TargetDeviceRow.id, or_(*predicates), editor).where(
                    TargetDeviceRow.id.in_(ids)
                )
            )
        }

    def save(self, states: list[PathState], now: datetime) -> None:
        for value in states:
            row = self.authorities[value.device_id]
            before = (
                row.holder_terminal_id,
                row.instance_id,
                row.requested_terminal_id,
                row.generation,
                row.state,
                row.require_ocr,
            )
            after = (
                value.holder,
                value.holder_instance,
                value.requested,
                value.generation,
                value.state,
                value.require_ocr,
            )
            if before == after:
                continue
            row.holder_terminal_id, row.instance_id, row.requested_terminal_id = after[:3]
            row.generation, row.state, row.require_ocr = after[3:]
            row.row_version += 1
            row.updated_at = now
            self._record_change(value, now)
            target = self.targets[value.device_id]
            if target.managing_terminal_id != value.holder and value.state == "active":
                target.mode, target.managing_terminal_id = "unassigned", None
                target.row_version += 1
        self.session.flush()

        for value in states:
            target = self.targets[value.device_id]
            if (
                value.state == "active"
                and value.holder
                and target.managing_terminal_id != value.holder
            ):
                chosen = next(p for p in value.paths if p.terminal_id == value.holder)
                target.mode = "mounted" if chosen.kind == "linux" else "standalone"
                target.managing_terminal_id, target.updated_at = value.holder, now
                target.row_version += 1
        self.session.flush()

    def _record_change(self, value: PathState, now: datetime) -> None:
        correlation = uuid4()
        payload = {
            "device_id": str(value.device_id),
            "generation": value.generation,
            "holder_terminal_id": str(value.holder) if value.holder else None,
            "state": value.state,
        }
        PostgresOutboxRepository(self.session, owner_module="maa").add(
            NewOutboxEvent(
                event_id=uuid4(),
                event_type="target_device.authority_changed.v1",
                schema_version=1,
                aggregate_type="target_device",
                aggregate_id=value.device_id,
                correlation_id=correlation,
                occurred_at=now,
                payload=payload,
            )
        )
        PostgresAuditRepository(self.session, owner_module="maa").add(
            NewAuditEntry(
                audit_id=uuid4(),
                actor_type="terminal" if self.terminal_id else "system",
                actor_id=self.terminal_id,
                action="target_device.authority.change",
                target_type="target_device",
                target_id=value.device_id,
                correlation_id=correlation,
                details=payload,
            )
        )

    def view(self, device: UUID, now: datetime) -> dict[str, object]:
        target = self.session.get(TargetDeviceRow, device)
        if target is None or target.deleted_at is not None:
            raise NotFoundError("target_device")
        authority = self.session.get(DeviceAuthorityRow, device)
        paths = []
        for path, terminal in self.session.execute(
            select(DevicePathRow, TerminalRow)
            .join(TerminalRow, TerminalRow.id == DevicePathRow.terminal_id)
            .where(DevicePathRow.device_id == device, TerminalRow.deleted_at.is_(None))
        ):
            fresh = bool(path.observed_at and path.observed_at >= now - timedelta(seconds=30))
            online = bool(
                terminal.last_seen_at
                and terminal.last_seen_at >= now - timedelta(seconds=90)
                and terminal.service_status == "online"
            )
            paths.append(
                {
                    "kind": path.kind,
                    "terminal_id": str(path.terminal_id),
                    "display_name": terminal.display_name,
                    "online": online,
                    "control_ready": fresh and online and path.control_ready,
                    "observed_at": path.observed_at,
                    "ready_count": path.ready_count,
                    "authority_protocol": path.authority_protocol,
                }
            )
        return {
            "device_id": str(device),
            "holder_terminal_id": str(target.managing_terminal_id)
            if target.managing_terminal_id
            else None,
            "state": authority.state if authority else "active",
            "generation": authority.generation if authority else 0,
            "policy": "linux_first",
            "paths": paths,
        }

    def load(self, devices: list[UUID], now: datetime) -> list[PathState]:
        self.targets = {
            t.id: t
            for t in self.session.scalars(
                select(TargetDeviceRow)
                .where(TargetDeviceRow.id.in_(devices), TargetDeviceRow.deleted_at.is_(None))
                .order_by(TargetDeviceRow.id)
                .with_for_update()
            )
        }
        return self._states(list(self.targets), now)
