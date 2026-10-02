from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from al1s.execution.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
)
from al1s.execution.resource_base import ResourceServiceBase
from al1s.execution.types import (
    ExecutionLeaseRecord,
    LeaseKind,
    LeaseOwnerKind,
    TargetDeviceRecord,
)


class LeaseOperations(ResourceServiceBase):
    def acquire_lease(
        self,
        *,
        terminal_id: UUID | None,
        target_device_id: UUID | None,
        lease_kind: LeaseKind,
        owner_kind: LeaseOwnerKind,
        owner_id: UUID,
        ttl: timedelta,
    ) -> ExecutionLeaseRecord:
        if terminal_id is None and target_device_id is None:
            raise InvalidRequestError("lease_resource_required", "Lease requires a resource")
        if not timedelta(seconds=5) <= ttl <= timedelta(hours=4):
            raise InvalidRequestError(
                "invalid_lease_ttl", "Lease TTL must be between 5 seconds and 4 hours"
            )
        now = self._now()
        try:
            with self._uow_factory() as uow:
                target: TargetDeviceRecord | None = None
                if target_device_id is not None:
                    target = uow.target_devices.get_active(target_device_id, for_update=True)
                    if target is None:
                        raise NotFoundError("target_device")
                if terminal_id is not None:
                    terminal = uow.terminals.get_active(terminal_id, for_update=True)
                    if terminal is None:
                        raise NotFoundError("terminal")
                    if target is not None and target.managing_terminal_id != terminal_id:
                        raise ConflictError(
                            "target_terminal_mismatch",
                            "Target device is not managed by the leased terminal",
                        )
                uow.leases.release_expired(
                    terminal_id=terminal_id, target_device_id=target_device_id, now=now
                )
                if uow.leases.has_active(
                    terminal_id=terminal_id,
                    target_device_id=target_device_id,
                    now=now,
                ):
                    raise ConflictError("resource_leased", "Execution resource is already leased")
                lease = uow.leases.add(
                    uuid4(),
                    terminal_id,
                    target_device_id,
                    lease_kind,
                    owner_kind,
                    owner_id,
                    now,
                    now + ttl,
                )
                uow.commit()
        except IntegrityError as exc:
            raise ConflictError("resource_leased", "Execution resource is already leased") from exc
        return lease

    def renew_lease(
        self,
        *,
        lease_id: UUID,
        expected_version: int,
        owner_kind: LeaseOwnerKind,
        owner_id: UUID,
        ttl: timedelta,
    ) -> ExecutionLeaseRecord:
        now = self._now()
        if not timedelta(seconds=5) <= ttl <= timedelta(hours=4):
            raise InvalidRequestError(
                "invalid_lease_ttl", "Lease TTL must be between 5 seconds and 4 hours"
            )
        with self._uow_factory() as uow:
            lease = uow.leases.get_for_update(lease_id)
            if lease is None:
                raise NotFoundError("execution_lease")
            if lease.released_at is not None or lease.expires_at <= now:
                raise ConflictError("lease_expired", "Execution lease has expired")
            renewed = uow.leases.renew(lease_id, expected_version, owner_kind, owner_id, now + ttl)
            if renewed is None:
                raise ConflictError("stale_lease_owner", "Lease owner or version is stale")
            uow.commit()
        return renewed

    def release_lease(
        self,
        *,
        lease_id: UUID,
        expected_version: int,
        owner_kind: LeaseOwnerKind,
        owner_id: UUID,
    ) -> None:
        now = self._now()
        with self._uow_factory() as uow:
            lease = uow.leases.get_for_update(lease_id)
            if lease is None:
                raise NotFoundError("execution_lease")
            if lease.released_at is not None:
                return
            if not uow.leases.release(lease_id, expected_version, owner_kind, owner_id, now):
                raise ConflictError("stale_lease_owner", "Lease owner or version is stale")
            uow.commit()
