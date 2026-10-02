"""Create editor-only script drafts from a trusted foreground-app observation."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from al1s.maa.default_documents import default_end_manifest
from al1s.maa.errors import MaaDomainError
from al1s.maa.mutation_service import MaaMutationService, MutationExecution, MutationOutcome
from al1s.maa.naming import normalize_asset_name
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.publication_service import MaaScriptPublicationService
from al1s.maa.types import (
    ApplicationDeviceRecord,
    ApplicationRecord,
    ScriptRecord,
    ScriptStatus,
    ScriptType,
)


class MaaEditorCreationService:
    def __init__(
        self, uow_factory: Callable[[], MaaUnitOfWork], *, now: Callable[[], datetime] | None = None
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now or (lambda: datetime.now(UTC))
        self._mutations = MaaMutationService(uow_factory, now=self._now)
        self._publication = MaaScriptPublicationService(uow_factory, now=self._now)

    def create(
        self,
        *,
        device_id: UUID,
        package_name: str,
        name: str | None,
        idempotency_key: str,
        correlation_id: UUID,
        icon_png: bytes | None = None,
    ) -> MutationExecution:
        if not package_name or len(package_name) > 255:
            raise MaaDomainError("invalid_foreground_package", "Invalid foreground package", 422)
        if name is not None and not 1 <= len(name.strip()) <= 255:
            raise MaaDomainError(
                "invalid_script_name", "Script name must be 1 to 255 characters", 422
            )
        return self._mutations.execute(
            operation=f"maa.editor.create:{device_id}",
            idempotency_key=idempotency_key,
            request_payload={
                "device_id": str(device_id),
                "package_name": package_name,
                "name": name,
            },
            mutate=lambda uow: self._create(
                uow, device_id, package_name, name, correlation_id, icon_png,
            ),
            integrity_error_mapper=lambda _error: MaaDomainError(
                "editor_create_conflict",
                "Category or script changed concurrently; refresh and retry",
                409,
            ),
        )

    def needs_icon(self, device_id: UUID, package_name: str) -> bool:
        with self._uow_factory() as uow:
            binding = uow.application_devices.for_phone_package(device_id, package_name)
            if binding is None:
                return True
            application = uow.applications.get_active(binding.application_id)
            return application is not None and application.icon_png is None

    def _create(
        self,
        uow: MaaUnitOfWork,
        device_id: UUID,
        package: str,
        name: str | None,
        correlation_id: UUID,
        icon_png: bytes | None,
    ) -> MutationOutcome:
        if not uow.application_devices.lock_active_device(device_id):
            raise MaaDomainError("target_device_not_found", "Phone is not registered", 404)
        existing = uow.application_devices.for_phone_package(device_id, package)
        now = self._now()
        created_category = existing is None
        if existing is None:
            application = ApplicationRecord(
                application_id=uuid4(),
                package_name=package,
                display_name=package,
                created_at=now,
                updated_at=now,
                deleted_at=None,
                row_version=1,
                icon_png=icon_png,
            )
            uow.applications.add_many((application,))
            uow.flush()
            uow.application_devices.add(
                ApplicationDeviceRecord(
                    binding_id=uuid4(),
                    application_id=application.application_id,
                    device_id=device_id,
                    package_name=package,
                    created_at=now,
                )
            )
            start = self._draft(
                application.application_id, "开始脚本", ScriptType.MODULE_START, now
            )
            end = self._draft(application.application_id, "结束脚本", ScriptType.MODULE_END, now)
            uow.scripts.add_many((start, end))
            uow.flush()
            end_manifest = default_end_manifest(package)
            self._publication.save_current_document_in_uow(
                uow,
                script=end,
                application=application,
                expected_script_version=1,
                manifest=end_manifest,
                correlation_id=correlation_id,
                now=now,
                source="editor_category_default_end",
            )
            script = start
        else:
            existing_application = uow.applications.get_active(
                existing.application_id, for_update=True,
            )
            if existing_application is None or existing_application.package_name != package:
                raise MaaDomainError(
                    "application_binding_invalid", "Bound category is unavailable", 409
                )
            application = existing_application
            if application.icon_png is None and icon_png is not None:
                refreshed = uow.applications.update_icon(
                    application.application_id, application.row_version, icon_png, now,
                )
                if refreshed is None:
                    raise MaaDomainError("editor_create_conflict", "分类已变化, 请重试", 409)
                application = refreshed
            boundaries = uow.scripts.find_active_by_types(
                application.application_id, (ScriptType.MODULE_START, ScriptType.MODULE_END)
            )
            if len(boundaries) != 2:
                raise MaaDomainError(
                    "application_incomplete", "Category is missing a start or end script", 409
                )
            chosen = (
                name.strip() if name else self._next_process_name(uow, application.application_id)
            )
            normalized = normalize_asset_name(chosen, asset_kind="script")
            if uow.scripts.find_active_by_keys(((application.application_id, normalized),)):
                raise MaaDomainError(
                    "script_name_conflict", "A script with this name already exists", 409
                )
            script = self._draft(application.application_id, chosen, ScriptType.MODULE_PROCESS, now)
            uow.scripts.add_many((script,))
        uow.flush()
        return MutationOutcome(
            aggregate_type="maa_script",
            aggregate_id=script.script_id,
            response_status=201,
            response_body={
                "script_id": str(script.script_id),
                "application_id": str(application.application_id),
                "package_name": package,
                "script_type": script.script_type.value,
                "created_category": created_category,
            },
        )

    @staticmethod
    def _draft(
        application_id: UUID, name: str, script_type: ScriptType, now: datetime
    ) -> ScriptRecord:
        return ScriptRecord(
            script_id=uuid4(),
            application_id=application_id,
            name=name,
            normalized_name=normalize_asset_name(name, asset_kind="script"),
            script_type=script_type,
            status=ScriptStatus.VALIDATION_PENDING,
            current_version_id=None,
            candidate_version_id=None,
            created_at=now,
            updated_at=now,
            deleted_at=None,
            row_version=1,
        )

    @staticmethod
    def _next_process_name(uow: MaaUnitOfWork, application_id: UUID) -> str:
        for number in range(1, 10001):
            name = "过程脚本" if number == 1 else f"过程脚本 {number}"
            key = normalize_asset_name(name, asset_kind="script")
            if not uow.scripts.find_active_by_keys(((application_id, key),)):
                return name
        raise MaaDomainError("script_limit", "Too many process scripts", 409)
