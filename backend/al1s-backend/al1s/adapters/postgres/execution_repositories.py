from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import case, or_, select, true, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from al1s.adapters.postgres.delivery_models import OfflineStartPermitRow
from al1s.adapters.postgres.editor_models import EditorSessionRow
from al1s.adapters.postgres.execution_models import (
    ExecutionLeaseRow,
    TargetDeviceIdentifierRow,
    TargetDeviceRow,
    TerminalCapabilityProfileRow,
    TerminalCredentialRow,
    TerminalRegistrationGrantRow,
    TerminalRow,
)
from al1s.adapters.postgres.maa_models import MaaQuickTestSessionRow
from al1s.adapters.postgres.scheduling_models import ExecutionRow
from al1s.execution.storage import StorageObservation
from al1s.execution.types import (
    CapabilityManifest,
    CapabilityProfileRecord,
    ExecutionLeaseRecord,
    LeaseKind,
    LeaseOwnerKind,
    RegistrationGrant,
    TargetDeviceMode,
    TargetDeviceRecord,
    TargetIdentifierRecord,
    TargetIdentifierSource,
    TerminalAcceptanceStatus,
    TerminalManagementFacts,
    TerminalRecord,
    TerminalServiceStatus,
    TerminalType,
)

MAX_PAGE_SIZE = 100


def _bounded_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    return limit


def _grant_record(row: TerminalRegistrationGrantRow) -> RegistrationGrant:
    return RegistrationGrant(
        grant_id=row.id,
        secret_digest=row.secret_digest,
        allowed_terminal_type=(
            TerminalType(row.allowed_terminal_type) if row.allowed_terminal_type else None
        ),
        target_device_id=row.target_device_id,
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
        consumed_by_terminal_id=row.consumed_by_terminal_id,
        row_version=row.row_version,
    )


def _terminal_record(row: TerminalRow) -> TerminalRecord:
    return TerminalRecord(
        terminal_id=row.id,
        installation_id=row.installation_id,
        terminal_type=TerminalType(row.terminal_type),
        display_name=row.display_name,
        name_is_custom=row.name_is_custom,
        name_version=row.name_version,
        service_status=TerminalServiceStatus(row.service_status),
        acceptance_status=TerminalAcceptanceStatus(row.acceptance_status),
        agent_version=row.agent_version,
        current_capability_profile_id=row.current_capability_profile_id,
        row_version=row.row_version,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        deleted_at=row.deleted_at,
        storage=(
            StorageObservation(
                row.storage_directory,
                row.storage_total_bytes,
                row.storage_used_bytes,
                row.storage_available_bytes,
            )
            if row.storage_directory is not None
            and row.storage_total_bytes is not None
            and row.storage_used_bytes is not None
            and row.storage_available_bytes is not None
            else None
        ),
        storage_observed_at=row.storage_observed_at,
        storage_probe_ok=row.storage_probe_ok,
        storage_alert_active=row.storage_alert_active,
    )


def _capability_record(row: TerminalCapabilityProfileRow) -> CapabilityProfileRecord:
    return CapabilityProfileRecord(
        profile_id=row.id,
        terminal_id=row.terminal_id,
        revision=row.revision,
        manifest_hash=row.manifest_hash,
        manifest=CapabilityManifest(
            schema_version=row.schema_version,
            protocol_version=row.protocol_version,
            agent_version=row.agent_version,
            os_name=row.os_name,
            os_version=row.os_version,
            architecture=row.architecture,
            cpu_cores=row.cpu_cores,
            memory_bytes=row.memory_bytes,
            storage_available_bytes=row.storage_available_bytes,
            accelerator_type=row.accelerator_type,
            low_resource=row.low_resource,
            provider_keys=tuple(row.provider_keys),
            details=dict(row.details),
        ),
        created_at=row.created_at,
    )


def _target_record(row: TargetDeviceRow) -> TargetDeviceRecord:
    return TargetDeviceRecord(
        device_id=row.id,
        display_name=row.display_name,
        platform=row.platform,
        mode=TargetDeviceMode(row.mode),
        managing_terminal_id=row.managing_terminal_id,
        row_version=row.row_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _identifier_record(row: TargetDeviceIdentifierRow) -> TargetIdentifierRecord:
    return TargetIdentifierRecord(
        identifier_id=row.id,
        target_device_id=row.target_device_id,
        source_terminal_id=row.source_terminal_id,
        source_type=TargetIdentifierSource(row.source_type),
        identifier_digest=row.identifier_digest,
        display_hint=row.display_hint,
        row_version=row.row_version,
        created_at=row.created_at,
        bound_at=row.bound_at,
        deleted_at=row.deleted_at,
        adb_state=row.adb_state,
        observed_at=row.observed_at,
    )


def _lease_record(row: ExecutionLeaseRow) -> ExecutionLeaseRecord:
    return ExecutionLeaseRecord(
        lease_id=row.id,
        terminal_id=row.terminal_id,
        target_device_id=row.target_device_id,
        lease_kind=LeaseKind(row.lease_kind),
        owner_kind=LeaseOwnerKind(row.owner_kind),
        owner_id=row.owner_id,
        acquired_at=row.acquired_at,
        expires_at=row.expires_at,
        released_at=row.released_at,
        row_version=row.row_version,
    )


class PostgresRegistrationGrantRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        grant_id: UUID,
        secret_digest: str,
        allowed_terminal_type: TerminalType | None,
        target_device_id: UUID | None,
        expires_at: datetime,
    ) -> None:
        self._session.add(
            TerminalRegistrationGrantRow(
                id=grant_id,
                secret_digest=secret_digest,
                allowed_terminal_type=(
                    allowed_terminal_type.value if allowed_terminal_type else None
                ),
                target_device_id=target_device_id,
                expires_at=expires_at,
                row_version=1,
            )
        )

    def get_for_update(self, grant_id: UUID) -> RegistrationGrant | None:
        row = self._session.scalar(
            select(TerminalRegistrationGrantRow)
            .where(TerminalRegistrationGrantRow.id == grant_id)
            .with_for_update()
        )
        return _grant_record(row) if row else None

    def consume(
        self,
        grant_id: UUID,
        expected_version: int,
        terminal_id: UUID,
        consumed_at: datetime,
    ) -> bool:
        statement = (
            update(TerminalRegistrationGrantRow)
            .where(
                TerminalRegistrationGrantRow.id == grant_id,
                TerminalRegistrationGrantRow.row_version == expected_version,
                TerminalRegistrationGrantRow.consumed_at.is_(None),
            )
            .values(
                consumed_at=consumed_at,
                consumed_by_terminal_id=terminal_id,
                row_version=TerminalRegistrationGrantRow.row_version + 1,
            )
            .returning(TerminalRegistrationGrantRow.id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None


class PostgresTerminalRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def expire_heartbeats(self, cutoff: datetime, *, limit: int) -> list[TerminalRecord]:
        if not 1 <= limit <= 100:
            raise ValueError("heartbeat batch limit must be 1..100")
        candidates = (
            select(TerminalRow.id)
            .where(
                TerminalRow.deleted_at.is_(None),
                TerminalRow.service_status == "online",
                TerminalRow.last_seen_at <= cutoff,
            )
            .order_by(TerminalRow.last_seen_at, TerminalRow.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("expired_terminals")
        )
        rows = self._session.scalars(
            update(TerminalRow)
            .where(TerminalRow.id.in_(select(candidates.c.id)))
            .values(service_status="offline", row_version=TerminalRow.row_version + 1)
            .returning(TerminalRow)
        ).all()
        return [_terminal_record(row) for row in rows]

    def add(
        self,
        terminal_id: UUID,
        installation_id: UUID,
        terminal_type: TerminalType,
        display_name: str,
        agent_version: str,
        now: datetime,
    ) -> TerminalRecord:
        row = TerminalRow(
            id=terminal_id,
            installation_id=installation_id,
            terminal_type=terminal_type.value,
            display_name=display_name,
            service_status=TerminalServiceStatus.ONLINE.value,
            acceptance_status=TerminalAcceptanceStatus.ACCEPTING.value,
            agent_version=agent_version,
            row_version=1,
            last_seen_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return _terminal_record(row)

    def find_active_by_installation_id_for_update(
        self, installation_id: UUID
    ) -> TerminalRecord | None:
        row = self._session.scalar(
            select(TerminalRow)
            .where(
                TerminalRow.installation_id == installation_id,
                TerminalRow.deleted_at.is_(None),
            )
            .with_for_update()
        )
        return _terminal_record(row) if row else None

    def get_active(self, terminal_id: UUID, *, for_update: bool = False) -> TerminalRecord | None:
        statement = select(TerminalRow).where(
            TerminalRow.id == terminal_id, TerminalRow.deleted_at.is_(None)
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _terminal_record(row) if row else None

    def refresh_registration(
        self,
        terminal_id: UUID,
        expected_version: int,
        display_name: str,
        agent_version: str,
        now: datetime,
    ) -> TerminalRecord | None:
        statement = (
            update(TerminalRow)
            .where(
                TerminalRow.id == terminal_id,
                TerminalRow.row_version == expected_version,
                TerminalRow.deleted_at.is_(None),
            )
            .values(
                display_name=display_name,
                agent_version=agent_version,
                service_status=TerminalServiceStatus.ONLINE.value,
                last_seen_at=now,
                row_version=TerminalRow.row_version + 1,
            )
            .returning(TerminalRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return _terminal_record(row) if row else None

    def rename(
        self, terminal_id: UUID, expected_name_version: int, display_name: str
    ) -> TerminalRecord | None:
        row = self._session.execute(
            update(TerminalRow)
            .where(
                TerminalRow.id == terminal_id,
                TerminalRow.name_version == expected_name_version,
                TerminalRow.deleted_at.is_(None),
            )
            .values(
                display_name=display_name,
                name_is_custom=True,
                name_version=TerminalRow.name_version + 1,
                row_version=TerminalRow.row_version + 1,
            )
            .returning(TerminalRow)
        ).scalar_one_or_none()
        return _terminal_record(row) if row else None

    def heartbeat(
        self,
        terminal_id: UUID,
        expected_version: int,
        service_status: TerminalServiceStatus,
        acceptance_status: TerminalAcceptanceStatus,
        agent_version: str,
        now: datetime,
        storage: StorageObservation | None = None,
        storage_alert_active: bool = False,
    ) -> TerminalRecord | None:
        statement = (
            update(TerminalRow)
            .where(
                TerminalRow.id == terminal_id,
                TerminalRow.row_version == expected_version,
                TerminalRow.deleted_at.is_(None),
            )
            .values(
                service_status=service_status.value,
                acceptance_status=acceptance_status.value,
                agent_version=agent_version,
                last_seen_at=now,
                storage_probe_ok=storage is not None,
                row_version=TerminalRow.row_version + 1,
            )
            .returning(TerminalRow)
        )
        if storage is not None:
            statement = statement.values(
                storage_directory=storage.directory,
                storage_total_bytes=storage.total_bytes,
                storage_used_bytes=storage.used_bytes,
                storage_available_bytes=storage.available_bytes,
                storage_observed_at=now,
                storage_alert_active=storage_alert_active,
            )
        row = self._session.execute(statement).scalar_one_or_none()
        return _terminal_record(row) if row else None

    def set_current_capability(
        self,
        terminal_id: UUID,
        expected_version: int,
        profile_id: UUID,
        agent_version: str,
    ) -> TerminalRecord | None:
        statement = (
            update(TerminalRow)
            .where(
                TerminalRow.id == terminal_id,
                TerminalRow.row_version == expected_version,
                TerminalRow.deleted_at.is_(None),
            )
            .values(
                current_capability_profile_id=profile_id,
                agent_version=agent_version,
                row_version=TerminalRow.row_version + 1,
            )
            .returning(TerminalRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return _terminal_record(row) if row else None

    def soft_delete(self, terminal_id: UUID, expected_version: int, deleted_at: datetime) -> bool:
        statement = (
            update(TerminalRow)
            .where(
                TerminalRow.id == terminal_id,
                TerminalRow.row_version == expected_version,
                TerminalRow.deleted_at.is_(None),
            )
            .values(
                service_status=TerminalServiceStatus.OFFLINE.value,
                acceptance_status=TerminalAcceptanceStatus.DISABLED.value,
                deleted_at=deleted_at,
                row_version=TerminalRow.row_version + 1,
            )
            .returning(TerminalRow.id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def list_active(self, *, after_id: UUID | None, limit: int) -> list[TerminalRecord]:
        statement = select(TerminalRow).where(TerminalRow.deleted_at.is_(None))
        if after_id is not None:
            statement = statement.where(TerminalRow.id > after_id)
        rows = self._session.scalars(
            statement.order_by(TerminalRow.id).limit(_bounded_limit(limit))
        ).all()
        return [_terminal_record(row) for row in rows]

    def list_management_facts(
        self, *, after_id: UUID | None, limit: int, now: datetime
    ) -> list[TerminalManagementFacts]:
        has_active_lease = (
            select(ExecutionLeaseRow.id)
            .where(
                ExecutionLeaseRow.terminal_id == TerminalRow.id,
                ExecutionLeaseRow.released_at.is_(None),
                ExecutionLeaseRow.expires_at > now,
            )
            .exists()
        )
        manages_target_devices = (
            select(TargetDeviceRow.id)
            .where(
                TargetDeviceRow.managing_terminal_id == TerminalRow.id,
                TargetDeviceRow.deleted_at.is_(None),
            )
            .exists()
        )
        statement = select(TerminalRow, has_active_lease, manages_target_devices).where(
            TerminalRow.deleted_at.is_(None)
        )
        if after_id is not None:
            statement = statement.where(TerminalRow.id > after_id)
        rows = self._session.execute(
            statement.order_by(TerminalRow.id).limit(_bounded_limit(limit))
        ).all()
        return [
            TerminalManagementFacts(
                terminal=_terminal_record(row),
                has_active_lease=bool(active_lease),
                manages_target_devices=bool(manages_targets),
            )
            for row, active_lease, manages_targets in rows
        ]


class PostgresTerminalCredentialRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_active_digest(self, terminal_id: UUID) -> str | None:
        return self._session.scalar(
            select(TerminalCredentialRow.secret_digest).where(
                TerminalCredentialRow.terminal_id == terminal_id,
                TerminalCredentialRow.revoked_at.is_(None),
            )
        )

    def revoke_active(self, terminal_id: UUID, revoked_at: datetime) -> int:
        statement = (
            update(TerminalCredentialRow)
            .where(
                TerminalCredentialRow.terminal_id == terminal_id,
                TerminalCredentialRow.revoked_at.is_(None),
            )
            .values(
                revoked_at=revoked_at,
                row_version=TerminalCredentialRow.row_version + 1,
            )
            .returning(TerminalCredentialRow.id)
        )
        return len(self._session.execute(statement).scalars().all())

    def add(self, credential_id: UUID, terminal_id: UUID, secret_digest: str) -> None:
        self._session.add(
            TerminalCredentialRow(
                id=credential_id,
                terminal_id=terminal_id,
                secret_digest=secret_digest,
                row_version=1,
            )
        )


class PostgresCapabilityProfileRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, profile_id: UUID) -> CapabilityProfileRecord | None:
        row = self._session.get(TerminalCapabilityProfileRow, profile_id)
        return _capability_record(row) if row else None

    def find_revision(self, terminal_id: UUID, revision: int) -> CapabilityProfileRecord | None:
        row = self._session.scalar(
            select(TerminalCapabilityProfileRow).where(
                TerminalCapabilityProfileRow.terminal_id == terminal_id,
                TerminalCapabilityProfileRow.revision == revision,
            )
        )
        return _capability_record(row) if row else None

    def add(
        self,
        profile_id: UUID,
        terminal_id: UUID,
        revision: int,
        manifest_hash: str,
        manifest: CapabilityManifest,
    ) -> CapabilityProfileRecord:
        row = TerminalCapabilityProfileRow(
            id=profile_id,
            terminal_id=terminal_id,
            revision=revision,
            schema_version=manifest.schema_version,
            protocol_version=manifest.protocol_version,
            agent_version=manifest.agent_version,
            os_name=manifest.os_name,
            os_version=manifest.os_version,
            architecture=manifest.architecture,
            cpu_cores=manifest.cpu_cores,
            memory_bytes=manifest.memory_bytes,
            storage_available_bytes=manifest.storage_available_bytes,
            accelerator_type=manifest.accelerator_type,
            low_resource=manifest.low_resource,
            provider_keys=list(manifest.provider_keys),
            details=dict(manifest.details),
            manifest_hash=manifest_hash,
        )
        self._session.add(row)
        self._session.flush()
        return _capability_record(row)


class PostgresTargetDeviceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def has_pending_control(self, device_id: UUID, manager_id: UUID | None) -> bool:
        # The caller holds the target row lock; all new control work takes the same lock.
        predicates = (
            select(ExecutionRow.id).where(
                ExecutionRow.target_device_id == device_id,
                ExecutionRow.status.in_(("waiting", "queued", "running")),
            ).exists(),
            select(OfflineStartPermitRow.id).where(
                OfflineStartPermitRow.target_device_id == device_id,
                OfflineStartPermitRow.status == "issued",
                *([OfflineStartPermitRow.terminal_id == manager_id] if manager_id else []),
            ).exists(),
            select(EditorSessionRow.id).where(
                EditorSessionRow.device_id == device_id,
                EditorSessionRow.status.in_(("pending", "active", "closing")),
            ).exists(),
            select(MaaQuickTestSessionRow.id).where(
                MaaQuickTestSessionRow.target_device_id == device_id,
                MaaQuickTestSessionRow.status.in_(("issued", "claimed")),
                *([MaaQuickTestSessionRow.terminal_id == manager_id] if manager_id else []),
            ).exists(),
        )
        return bool(self._session.scalar(select(or_(*predicates))))

    def add(self, device_id: UUID, display_name: str, now: datetime) -> TargetDeviceRecord:
        row = TargetDeviceRow(
            id=device_id,
            display_name=display_name,
            platform="android",
            mode=TargetDeviceMode.UNASSIGNED.value,
            row_version=1,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return _target_record(row)

    def get_active(self, device_id: UUID, *, for_update: bool = False) -> TargetDeviceRecord | None:
        statement = select(TargetDeviceRow).where(
            TargetDeviceRow.id == device_id, TargetDeviceRow.deleted_at.is_(None)
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _target_record(row) if row else None

    def rename(
        self, device_id: UUID, expected_version: int, display_name: str, now: datetime
    ) -> TargetDeviceRecord | None:
        row = self._session.execute(
            update(TargetDeviceRow)
            .where(
                TargetDeviceRow.id == device_id,
                TargetDeviceRow.row_version == expected_version,
                TargetDeviceRow.deleted_at.is_(None),
            )
            .values(
                display_name=display_name,
                updated_at=now,
                row_version=TargetDeviceRow.row_version + 1,
            )
            .returning(TargetDeviceRow)
        ).scalar_one_or_none()
        return _target_record(row) if row else None

    def update_mode(
        self,
        device_id: UUID,
        expected_version: int,
        mode: TargetDeviceMode,
        managing_terminal_id: UUID | None,
        now: datetime,
    ) -> TargetDeviceRecord | None:
        statement = (
            update(TargetDeviceRow)
            .where(
                TargetDeviceRow.id == device_id,
                TargetDeviceRow.row_version == expected_version,
                TargetDeviceRow.deleted_at.is_(None),
                ~select(EditorSessionRow.id)
                .where(
                    EditorSessionRow.device_id == device_id,
                    EditorSessionRow.status.in_(("pending", "active", "closing")),
                )
                .exists(),
            )
            .values(
                mode=mode.value,
                managing_terminal_id=managing_terminal_id,
                updated_at=now,
                row_version=TargetDeviceRow.row_version + 1,
            )
            .returning(TargetDeviceRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return _target_record(row) if row else None

    def list_active(self, *, after_id: UUID | None, limit: int) -> list[TargetDeviceRecord]:
        statement = select(TargetDeviceRow).where(TargetDeviceRow.deleted_at.is_(None))
        if after_id is not None:
            statement = statement.where(TargetDeviceRow.id > after_id)
        rows = self._session.scalars(
            statement.order_by(TargetDeviceRow.id).limit(_bounded_limit(limit))
        ).all()
        return [_target_record(row) for row in rows]

    def availability_rows(
        self, device_ids: tuple[UUID, ...]
    ) -> dict[UUID, tuple[str | None, str | None, datetime | None]]:
        if not device_ids:
            return {}
        if len(device_ids) > MAX_PAGE_SIZE:
            raise ValueError("availability page exceeds maximum")
        observation = (
            select(
                TargetDeviceIdentifierRow.adb_state.label("adb_state"),
                TargetDeviceIdentifierRow.observed_at.label("observed_at"),
            )
            .where(
                TargetDeviceIdentifierRow.target_device_id == TargetDeviceRow.id,
                TargetDeviceIdentifierRow.source_terminal_id
                == TargetDeviceRow.managing_terminal_id,
                TargetDeviceIdentifierRow.source_type == "adb_serial",
                TargetDeviceIdentifierRow.deleted_at.is_(None),
                TargetDeviceIdentifierRow.observed_at.is_not(None),
            )
            .order_by(TargetDeviceIdentifierRow.observed_at.desc())
            .limit(1)
            .lateral()
        )
        statement = (
            select(
                TargetDeviceRow.id,
                TerminalRow.service_status,
                observation.c.adb_state,
                observation.c.observed_at,
            )
            .select_from(TargetDeviceRow)
            .outerjoin(TerminalRow, TerminalRow.id == TargetDeviceRow.managing_terminal_id)
            .outerjoin(observation, true())
            .where(TargetDeviceRow.id.in_(device_ids))
        )
        return {
            device_id: (terminal_status, adb_state, observed_at)
            for device_id, terminal_status, adb_state, observed_at
            in self._session.execute(statement)
        }

    def has_managed_by(self, terminal_id: UUID) -> bool:
        return (
            self._session.scalar(
                select(TargetDeviceRow.id)
                .where(
                    TargetDeviceRow.managing_terminal_id == terminal_id,
                    TargetDeviceRow.deleted_at.is_(None),
                )
                .limit(1)
            )
            is not None
        )


class PostgresTargetIdentifierRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_adb(
        self, *, terminal_id: UUID, after_id: UUID | None, limit: int
    ) -> list[TargetIdentifierRecord]:
        statement = select(TargetDeviceIdentifierRow).where(
            TargetDeviceIdentifierRow.source_terminal_id == terminal_id,
            TargetDeviceIdentifierRow.source_type == TargetIdentifierSource.ADB_SERIAL.value,
            TargetDeviceIdentifierRow.deleted_at.is_(None),
        )
        if after_id is not None:
            statement = statement.where(TargetDeviceIdentifierRow.id > after_id)
        return [
            _identifier_record(row)
            for row in self._session.scalars(
                statement.order_by(TargetDeviceIdentifierRow.id).limit(limit)
            )
        ]

    def find_active(
        self,
        source_type: TargetIdentifierSource,
        identifier_digest: str,
        *,
        for_update: bool = False,
    ) -> TargetIdentifierRecord | None:
        statement = select(TargetDeviceIdentifierRow).where(
            TargetDeviceIdentifierRow.source_type == source_type.value,
            TargetDeviceIdentifierRow.identifier_digest == identifier_digest,
            TargetDeviceIdentifierRow.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _identifier_record(row) if row else None

    def find_bound_to_target(
        self,
        source_type: TargetIdentifierSource,
        target_device_id: UUID,
        *,
        for_update: bool = False,
    ) -> TargetIdentifierRecord | None:
        statement = select(TargetDeviceIdentifierRow).where(
            TargetDeviceIdentifierRow.source_type == source_type.value,
            TargetDeviceIdentifierRow.target_device_id == target_device_id,
            TargetDeviceIdentifierRow.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _identifier_record(row) if row else None

    def get_active(
        self, identifier_id: UUID, *, for_update: bool = False
    ) -> TargetIdentifierRecord | None:
        statement = select(TargetDeviceIdentifierRow).where(
            TargetDeviceIdentifierRow.id == identifier_id,
            TargetDeviceIdentifierRow.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _identifier_record(row) if row else None

    def add(
        self,
        identifier_id: UUID,
        source_terminal_id: UUID,
        source_type: TargetIdentifierSource,
        identifier_digest: str,
        display_hint: str,
    ) -> TargetIdentifierRecord:
        row = TargetDeviceIdentifierRow(
            id=identifier_id,
            source_terminal_id=source_terminal_id,
            source_type=source_type.value,
            identifier_digest=identifier_digest,
            display_hint=display_hint,
            row_version=1,
        )
        self._session.add(row)
        self._session.flush()
        return _identifier_record(row)

    def observe(
        self, identifier_id: UUID, adb_state: str, observed_at: datetime
    ) -> TargetIdentifierRecord:
        statement = (
            update(TargetDeviceIdentifierRow)
            .where(
                TargetDeviceIdentifierRow.id == identifier_id,
                TargetDeviceIdentifierRow.deleted_at.is_(None),
                (TargetDeviceIdentifierRow.observed_at.is_(None)
                 | (TargetDeviceIdentifierRow.observed_at <= observed_at)),
            )
            .values(adb_state=adb_state, observed_at=observed_at)
            .returning(TargetDeviceIdentifierRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        if row is None:
            row = self._session.get(TargetDeviceIdentifierRow, identifier_id)
        assert row is not None
        return _identifier_record(row)

    def observe_batch(
        self, terminal_id: UUID, items: tuple[tuple[UUID, str], ...], observed_at: datetime
    ) -> int:
        ids = tuple(identifier_id for identifier_id, _ in items)
        owned = self._session.scalars(
            select(TargetDeviceIdentifierRow.id).where(
                TargetDeviceIdentifierRow.id.in_(ids),
                TargetDeviceIdentifierRow.source_terminal_id == terminal_id,
                TargetDeviceIdentifierRow.source_type == "adb_serial",
                TargetDeviceIdentifierRow.deleted_at.is_(None),
            )
        ).all()
        if len(owned) != len(ids):
            raise ValueError("adb_observation_identifier_not_owned")
        statement = (
            update(TargetDeviceIdentifierRow)
            .where(
                TargetDeviceIdentifierRow.id.in_(ids),
                (TargetDeviceIdentifierRow.observed_at.is_(None)
                 | (TargetDeviceIdentifierRow.observed_at <= observed_at)),
            )
            .values(
                adb_state=case(dict(items), value=TargetDeviceIdentifierRow.id),
                observed_at=observed_at,
            )
        )
        return cast(CursorResult[Any], self._session.execute(statement)).rowcount or 0

    def bind(
        self,
        identifier_id: UUID,
        expected_version: int,
        target_device_id: UUID,
        bound_at: datetime,
    ) -> TargetIdentifierRecord | None:
        statement = (
            update(TargetDeviceIdentifierRow)
            .where(
                TargetDeviceIdentifierRow.id == identifier_id,
                TargetDeviceIdentifierRow.row_version == expected_version,
                TargetDeviceIdentifierRow.deleted_at.is_(None),
            )
            .values(
                target_device_id=target_device_id,
                bound_at=bound_at,
                row_version=TargetDeviceIdentifierRow.row_version + 1,
            )
            .returning(TargetDeviceIdentifierRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return _identifier_record(row) if row else None


class PostgresExecutionLeaseRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _resource_condition(
        terminal_id: UUID | None, target_device_id: UUID | None
    ) -> ColumnElement[bool]:
        conditions = []
        if terminal_id is not None:
            conditions.append(ExecutionLeaseRow.terminal_id == terminal_id)
        if target_device_id is not None:
            conditions.append(ExecutionLeaseRow.target_device_id == target_device_id)
        if not conditions:
            raise ValueError("at least one leased resource is required")
        return or_(*conditions)

    def release_expired(
        self,
        *,
        terminal_id: UUID | None,
        target_device_id: UUID | None,
        now: datetime,
    ) -> int:
        statement = (
            update(ExecutionLeaseRow)
            .where(
                self._resource_condition(terminal_id, target_device_id),
                ExecutionLeaseRow.released_at.is_(None),
                ExecutionLeaseRow.expires_at <= now,
            )
            .values(
                released_at=now,
                row_version=ExecutionLeaseRow.row_version + 1,
            )
            .returning(ExecutionLeaseRow.id)
        )
        return len(self._session.execute(statement).scalars().all())

    def has_active(
        self,
        *,
        terminal_id: UUID | None = None,
        target_device_id: UUID | None = None,
        now: datetime,
    ) -> bool:
        statement = select(ExecutionLeaseRow.id).where(
            self._resource_condition(terminal_id, target_device_id),
            ExecutionLeaseRow.released_at.is_(None),
            ExecutionLeaseRow.expires_at > now,
        )
        return self._session.scalar(statement.limit(1)) is not None

    def add(
        self,
        lease_id: UUID,
        terminal_id: UUID | None,
        target_device_id: UUID | None,
        lease_kind: LeaseKind,
        owner_kind: LeaseOwnerKind,
        owner_id: UUID,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> ExecutionLeaseRecord:
        row = ExecutionLeaseRow(
            id=lease_id,
            terminal_id=terminal_id,
            target_device_id=target_device_id,
            lease_kind=lease_kind.value,
            owner_kind=owner_kind.value,
            owner_id=owner_id,
            acquired_at=acquired_at,
            expires_at=expires_at,
            row_version=1,
        )
        self._session.add(row)
        self._session.flush()
        return _lease_record(row)

    def get_for_update(self, lease_id: UUID) -> ExecutionLeaseRecord | None:
        row = self._session.scalar(
            select(ExecutionLeaseRow).where(ExecutionLeaseRow.id == lease_id).with_for_update()
        )
        return _lease_record(row) if row else None

    def renew(
        self,
        lease_id: UUID,
        expected_version: int,
        owner_kind: LeaseOwnerKind,
        owner_id: UUID,
        expires_at: datetime,
    ) -> ExecutionLeaseRecord | None:
        statement = (
            update(ExecutionLeaseRow)
            .where(
                ExecutionLeaseRow.id == lease_id,
                ExecutionLeaseRow.row_version == expected_version,
                ExecutionLeaseRow.owner_kind == owner_kind.value,
                ExecutionLeaseRow.owner_id == owner_id,
                ExecutionLeaseRow.released_at.is_(None),
            )
            .values(
                expires_at=expires_at,
                row_version=ExecutionLeaseRow.row_version + 1,
            )
            .returning(ExecutionLeaseRow)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        return _lease_record(row) if row else None

    def release(
        self,
        lease_id: UUID,
        expected_version: int,
        owner_kind: LeaseOwnerKind,
        owner_id: UUID,
        released_at: datetime,
    ) -> bool:
        statement = (
            update(ExecutionLeaseRow)
            .where(
                ExecutionLeaseRow.id == lease_id,
                ExecutionLeaseRow.row_version == expected_version,
                ExecutionLeaseRow.owner_kind == owner_kind.value,
                ExecutionLeaseRow.owner_id == owner_id,
                ExecutionLeaseRow.released_at.is_(None),
            )
            .values(
                released_at=released_at,
                row_version=ExecutionLeaseRow.row_version + 1,
            )
            .returning(ExecutionLeaseRow.id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None
