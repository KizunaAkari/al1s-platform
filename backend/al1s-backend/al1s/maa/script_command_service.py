from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from al1s.maa.errors import MaaDomainError
from al1s.maa.mutation_service import (
    MaaMutationService,
    MutationExecution,
    MutationOutcome,
)
from al1s.maa.naming import normalize_asset_name
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.publication_service import MaaScriptPublicationService
from al1s.maa.script_deletion import delete_script
from al1s.maa.script_metadata import rename_script
from al1s.maa.types import (
    ApplicationRecord,
    ScriptRecord,
    ScriptType,
)

MaaUowFactory = Callable[[], MaaUnitOfWork]


class MaaScriptCommandService:
    """Rename, delete and save scripts through bounded, atomic commands."""

    def __init__(
        self,
        uow_factory: MaaUowFactory,
        *,
        publication_service: MaaScriptPublicationService | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._mutations = MaaMutationService(uow_factory, now=self._now)
        self._publication = publication_service or MaaScriptPublicationService(
            uow_factory, now=self._now
        )

    def rename_script(
        self,
        *,
        script_id: UUID,
        name: str,
        expected_row_version: int,
        idempotency_key: str,
        correlation_id: UUID,
    ) -> MutationExecution:
        normalized = normalize_asset_name(name, asset_kind="script")
        return self._mutations.execute(
            operation=f"maa.scripts.rename:{script_id}",
            idempotency_key=idempotency_key,
            request_payload={"name": name.strip(), "expected_row_version": expected_row_version},
            mutate=lambda uow: MutationOutcome(
                aggregate_type="maa_script",
                aggregate_id=script_id,
                response_status=200,
                response_body=self._script_body(
                    rename_script(
                        uow,
                        script_id=script_id,
                        name=name.strip(),
                        normalized_name=normalized,
                        expected_version=expected_row_version,
                        now=self._now(),
                        correlation_id=correlation_id,
                    )
                ),
            ),
            integrity_error_mapper=self._script_integrity_error,
        )

    def delete_script(
        self,
        *,
        script_id: UUID,
        expected_row_version: int,
        idempotency_key: str,
        correlation_id: UUID,
    ) -> MutationExecution:
        return self._mutations.execute(
            operation=f"maa.scripts.delete:{script_id}",
            idempotency_key=idempotency_key,
            request_payload={"expected_row_version": expected_row_version},
            mutate=lambda uow: delete_script(
                uow,
                script_id=script_id,
                expected_version=expected_row_version,
                now=self._now(),
                correlation_id=correlation_id,
            ),
        )

    def save_document(
        self,
        *,
        script_id: UUID,
        expected_row_version: int,
        manifest: dict[str, Any],
        device_id: UUID,
        idempotency_key: str,
        correlation_id: UUID,
    ) -> MutationExecution:
        return self._mutations.execute(
            operation=f"maa.scripts.document.save:{script_id}",
            idempotency_key=idempotency_key,
            request_payload={
                "expected_row_version": expected_row_version,
                "manifest": manifest,
                "device_id": str(device_id),
            },
            mutate=lambda uow: self._save_document(
                uow,
                script_id=script_id,
                expected_row_version=expected_row_version,
                manifest=manifest,
                device_id=device_id,
                correlation_id=correlation_id,
            ),
        )

    def _save_document(
        self,
        uow: MaaUnitOfWork,
        *,
        script_id: UUID,
        expected_row_version: int,
        manifest: dict[str, Any],
        device_id: UUID,
        correlation_id: UUID,
    ) -> MutationOutcome:
        if not isinstance(manifest.get("steps"), list) or not manifest["steps"]:
            raise MaaDomainError("empty_script", "保存前至少添加一个完整步骤", 422)
        script = self._require_script(uow, script_id)
        application = self._require_application(uow, script.application_id)
        if script.script_type is ScriptType.STANDARD:
            raise MaaDomainError("legacy_script_type", "普通脚本不能在新编辑器保存", 409)
        if script.candidate_version_id is not None:
            raise MaaDomainError(
                "legacy_candidate_requires_migration",
                "旧候选版本尚未迁移。不能用新编辑器直接覆盖",
                409,
            )
        binding = uow.application_devices.for_phone_package(device_id, application.package_name)
        if binding is None or binding.application_id != application.application_id:
            raise MaaDomainError("script_device_not_applicable", "该脚本分类未授权给当前手机", 403)
        now = self._now()
        saved = self._publication.save_current_document_in_uow(
            uow,
            script=script,
            application=application,
            expected_script_version=expected_row_version,
            manifest=manifest,
            correlation_id=correlation_id,
            now=now,
            source="document_save",
        )
        return self._script_outcome(saved, status=200)

    @staticmethod
    def _require_application(uow: MaaUnitOfWork, application_id: UUID) -> ApplicationRecord:
        application = uow.applications.get_active(application_id, for_update=True)
        if application is None:
            raise MaaDomainError("application_not_found", "Application was not found", 404)
        return application

    @staticmethod
    def _require_script(uow: MaaUnitOfWork, script_id: UUID) -> ScriptRecord:
        script = uow.scripts.get_active(script_id, for_update=True)
        if script is None:
            raise MaaDomainError("script_not_found", "Script was not found", 404)
        return script

    @staticmethod
    def _script_integrity_error(_error: IntegrityError) -> MaaDomainError:
        return MaaDomainError(
            "script_conflict",
            "Script name conflicts with another active script",
            409,
        )

    @classmethod
    def _script_outcome(cls, script: ScriptRecord, *, status: int) -> MutationOutcome:
        return MutationOutcome(
            aggregate_type="maa_script",
            aggregate_id=script.script_id,
            response_status=status,
            response_body={
                "script": cls._script_body(script),
            },
        )

    @staticmethod
    def _script_body(script: ScriptRecord) -> dict[str, Any]:
        return {
            "script_id": str(script.script_id),
            "application_id": str(script.application_id),
            "name": script.name,
            "script_type": script.script_type.value,
            "status": script.status.value,
            "current_version_id": (
                str(script.current_version_id) if script.current_version_id else None
            ),
            "candidate_version_id": (
                str(script.candidate_version_id) if script.candidate_version_id else None
            ),
            "row_version": script.row_version,
            "created_at": script.created_at.isoformat(),
            "updated_at": script.updated_at.isoformat(),
        }
