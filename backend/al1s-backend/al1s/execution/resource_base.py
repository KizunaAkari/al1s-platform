from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID, uuid4

from al1s.execution.android_target_enrollment import enroll_android_target
from al1s.execution.errors import (
    AuthenticationError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
)
from al1s.execution.ports import ExecutionUnitOfWork
from al1s.execution.security import (
    SecretDigester,
)
from al1s.execution.types import (
    CapabilityManifest,
    RegistrationGrant,
    TargetDeviceMode,
    TargetDeviceRecord,
    TerminalRecord,
    TerminalType,
)

UowFactory = Callable[[], ExecutionUnitOfWork]


class ResourceServiceBase:
    def __init__(
        self,
        uow_factory: UowFactory,
        *,
        digester: SecretDigester | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._digester = digester or SecretDigester()
        self._now = now or (lambda: datetime.now(UTC))

    def _authenticate(
        self,
        uow: ExecutionUnitOfWork,
        terminal_id: UUID,
        secret: str,
        *,
        for_update: bool = False,
    ) -> TerminalRecord:
        digest = uow.credentials.get_active_digest(terminal_id)
        if digest is None or not self._digester.verify(secret, digest):
            raise AuthenticationError()
        terminal = uow.terminals.get_active(terminal_id, for_update=for_update)
        if terminal is None:
            raise AuthenticationError()
        return terminal

    def _validate_registration_grant(
        self,
        grant: RegistrationGrant | None,
        secret: str,
        terminal_type: TerminalType,
        now: datetime,
    ) -> None:
        if grant is None or not self._digester.verify(secret, grant.secret_digest):
            raise AuthenticationError("invalid_registration_code", "Registration code is invalid")
        if grant.consumed_at is not None:
            raise ConflictError(
                "registration_code_consumed", "Registration code has already been used"
            )
        if grant.expires_at <= now:
            raise AuthenticationError("expired_registration_code", "Registration code has expired")
        if (
            grant.allowed_terminal_type is not None
            and grant.allowed_terminal_type is not terminal_type
        ):
            raise ConflictError(
                "registration_terminal_type_mismatch",
                "Registration code does not allow this terminal type",
            )

    @staticmethod
    def _resolve_registration_terminal(
        uow: ExecutionUnitOfWork,
        *,
        installation_id: UUID,
        terminal_type: TerminalType,
        display_name: str,
        agent_version: str,
        now: datetime,
    ) -> TerminalRecord:
        terminal = uow.terminals.find_active_by_installation_id_for_update(installation_id)
        if terminal is None:
            return uow.terminals.add(
                uuid4(),
                installation_id,
                terminal_type,
                display_name,
                agent_version,
                now,
            )
        if terminal.terminal_type is not terminal_type:
            raise ConflictError(
                "installation_terminal_type_mismatch",
                "Installation is already registered as another terminal type",
            )
        refreshed = uow.terminals.refresh_registration(
            terminal.terminal_id,
            terminal.row_version,
            terminal.display_name if terminal.name_is_custom else display_name,
            agent_version,
            now,
        )
        if refreshed is None:
            raise ConflictError("stale_terminal", "Terminal changed concurrently")
        return refreshed

    def _replace_terminal_credential(
        self, uow: ExecutionUnitOfWork, terminal_id: UUID, now: datetime
    ) -> str:
        credential, credential_digest = self._digester.issue(terminal_id)
        uow.credentials.revoke_active(terminal_id, now)
        uow.credentials.add(uuid4(), terminal_id, credential_digest)
        return credential

    def _enroll_android_target(
        self,
        uow: ExecutionUnitOfWork,
        *,
        grant: RegistrationGrant,
        terminal: TerminalRecord,
        installation_id: UUID,
        display_name: str,
        now: datetime,
        correlation_id: UUID,
    ) -> TargetDeviceRecord:
        return enroll_android_target(
            uow,
            grant=grant,
            terminal=terminal,
            installation_id=installation_id,
            display_name=display_name,
            now=now,
            correlation_id=correlation_id,
        )

    @staticmethod
    def _lock_and_validate_target_managers(
        uow: ExecutionUnitOfWork,
        *,
        target: TargetDeviceRecord,
        mode: TargetDeviceMode,
        managing_terminal_id: UUID | None,
    ) -> tuple[UUID, ...]:
        if mode is TargetDeviceMode.UNASSIGNED:
            if managing_terminal_id is not None:
                raise InvalidRequestError(
                    "unassigned_target_has_manager",
                    "Unassigned target device cannot have a managing terminal",
                )
        elif managing_terminal_id is None:
            raise InvalidRequestError(
                "target_manager_required", "Target mode requires a managing terminal"
            )

        manager_ids = {
            manager_id
            for manager_id in (target.managing_terminal_id, managing_terminal_id)
            if manager_id is not None
        }
        managers = {
            manager_id: uow.terminals.get_active(manager_id, for_update=True)
            for manager_id in sorted(manager_ids)
        }
        if any(manager is None for manager in managers.values()):
            raise NotFoundError("terminal")
        if managing_terminal_id is not None:
            manager = managers[managing_terminal_id]
            assert manager is not None
            expected_type = (
                TerminalType.ANDROID if mode is TargetDeviceMode.STANDALONE else TerminalType.LINUX
            )
            if manager.terminal_type is not expected_type:
                raise ConflictError(
                    "target_mode_terminal_type_mismatch",
                    f"{mode.value} mode requires a {expected_type.value} terminal",
                )
        return tuple(sorted(manager_ids))

    @staticmethod
    def _manifest_hash(manifest: CapabilityManifest) -> str:
        payload = asdict(manifest)
        payload["provider_keys"] = sorted(manifest.provider_keys)
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        return hashlib.sha256(encoded).hexdigest()
