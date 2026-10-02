"""Explicit administrator grants of a script category to registered phones."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from al1s.maa.errors import MaaDomainError
from al1s.maa.mutation_service import MaaMutationService, MutationExecution, MutationOutcome
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.types import ApplicationDeviceRecord


class MaaApplicabilityService:
    def __init__(
        self, uow_factory: Callable[[], MaaUnitOfWork], *, now: Callable[[], datetime] | None = None
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now or (lambda: datetime.now(UTC))
        self._mutations = MaaMutationService(uow_factory, now=self._now)

    def list_devices(self, application_id: UUID) -> list[ApplicationDeviceRecord]:
        with self._uow_factory() as uow:
            if uow.applications.get_active(application_id) is None:
                raise MaaDomainError("application_not_found", "Application was not found", 404)
            return uow.application_devices.for_application(application_id)

    def list_applications(self, device_id: UUID) -> list[dict[str, str]]:
        with self._uow_factory() as uow:
            if not uow.application_devices.lock_active_device(device_id):
                raise MaaDomainError("target_device_not_found", "Phone is not registered", 404)
            bindings = uow.application_devices.for_device(device_id)
            if len(bindings) > 200:
                raise MaaDomainError("too_many_applications", "Too many phone categories", 409)
            result = []
            for binding in bindings:
                application = uow.applications.get_active(binding.application_id)
                if application is not None:
                    result.append(
                        {
                            "application_id": str(application.application_id),
                            "package_name": application.package_name,
                            "display_name": application.display_name,
                        }
                    )
            return result

    def bind(
        self, *, application_id: UUID, device_id: UUID, idempotency_key: str
    ) -> MutationExecution:
        return self._mutations.execute(
            operation=f"maa.applications.bind:{application_id}:{device_id}",
            idempotency_key=idempotency_key,
            request_payload={"application_id": str(application_id), "device_id": str(device_id)},
            mutate=lambda uow: self._bind(uow, application_id, device_id),
            integrity_error_mapper=lambda _error: MaaDomainError(
                "application_binding_conflict",
                "Phone is already bound to this application",
                409,
            ),
        )

    def _bind(self, uow: MaaUnitOfWork, application_id: UUID, device_id: UUID) -> MutationOutcome:
        if not uow.application_devices.lock_active_device(device_id):
            raise MaaDomainError("target_device_not_found", "Phone is not registered", 404)
        application = uow.applications.get_active(application_id, for_update=True)
        if application is None:
            raise MaaDomainError("application_not_found", "Application was not found", 404)
        existing = uow.application_devices.for_phone_package(device_id, application.package_name)
        if existing is not None and existing.application_id != application_id:
            raise MaaDomainError(
                "application_binding_conflict",
                "Phone already uses another category for this application",
                409,
            )
        if existing is None:
            uow.application_devices.add(
                ApplicationDeviceRecord(
                    binding_id=uuid4(),
                    application_id=application_id,
                    device_id=device_id,
                    package_name=application.package_name,
                    created_at=self._now(),
                )
            )
        return MutationOutcome(
            aggregate_type="maa_application",
            aggregate_id=application_id,
            response_status=200,
            response_body={"application_id": str(application_id), "device_id": str(device_id)},
        )

    def unbind(
        self, *, application_id: UUID, device_id: UUID, idempotency_key: str
    ) -> MutationExecution:
        return self._mutations.execute(
            operation=f"maa.applications.unbind:{application_id}:{device_id}",
            idempotency_key=idempotency_key,
            request_payload={"application_id": str(application_id), "device_id": str(device_id)},
            mutate=lambda uow: self._unbind(uow, application_id, device_id),
        )

    def _unbind(self, uow: MaaUnitOfWork, application_id: UUID, device_id: UUID) -> MutationOutcome:
        if not uow.application_devices.lock_active_device(device_id):
            raise MaaDomainError("target_device_not_found", "Phone is not registered", 404)
        application = uow.applications.get_active(application_id, for_update=True)
        if application is None:
            raise MaaDomainError("application_not_found", "Application was not found", 404)
        existing = uow.application_devices.for_phone_package(device_id, application.package_name)
        if existing is None or existing.application_id != application_id:
            raise MaaDomainError("application_binding_not_found", "Phone is not bound here", 404)
        blockers = uow.application_devices.active_maa_tasks(device_id)
        if blockers:
            raise MaaDomainError(
                "application_binding_in_use",
                "Phone has dispatched Maa work; stop it before unbinding",
                409,
                context={
                    "task_ids": [str(task) for task in blockers[:50]],
                    "has_more": len(blockers) > 50,
                },
            )
        uow.application_devices.remove(application_id, device_id, self._now())
        return MutationOutcome(
            aggregate_type="maa_application",
            aggregate_id=application_id,
            response_status=200,
            response_body={"application_id": str(application_id), "device_id": str(device_id)},
        )
