"""New editor/category flow against an explicitly disposable PostgreSQL database."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.execution_models import TargetDeviceRow, TerminalRow
from al1s.adapters.postgres.maa_models import (
    MaaApplicationDeviceRow,
    MaaApplicationRow,
    MaaScriptRow,
)
from al1s.adapters.postgres.maa_unit_of_work import MaaSqlAlchemyUnitOfWork
from al1s.maa.applicability_service import MaaApplicabilityService
from al1s.maa.catalog_command_service import MaaCatalogCommandService
from al1s.maa.editor_creation import MaaEditorCreationService
from al1s.maa.errors import MaaDomainError
from al1s.maa.script_command_service import MaaScriptCommandService
from tests.integration.maa_support import clean_maa_tables as clean_maa_tables
from tests.integration.maa_support import engine as engine

pytestmark = pytest.mark.integration


def test_editor_creates_bound_category_once_and_saves_process(engine):
    terminal_id, first_phone, second_phone = uuid4(), uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            TerminalRow(
                id=terminal_id,
                installation_id=uuid4(),
                terminal_type="linux",
                display_name="Test terminal",
                service_status="online",
                acceptance_status="accepting",
                agent_version="test",
                row_version=1,
            )
        )
        session.flush()
        for phone_id in (first_phone, second_phone):
            session.add(
                TargetDeviceRow(
                    id=phone_id,
                    display_name=str(phone_id),
                    platform="android",
                    mode="mounted",
                    managing_terminal_id=terminal_id,
                    row_version=1,
                )
            )

    def factory():
        return MaaSqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))
    creator = MaaEditorCreationService(factory)
    package = "com.example.integration"

    def create(phone: UUID, key: str):
        return creator.create(
            device_id=phone,
            package_name=package,
            name=None,
            idempotency_key=key,
            correlation_id=uuid4(),
        )

    first = create(first_phone, "new-category")
    assert first.receipt.response_body["created_category"] is True
    assert first.receipt.response_body["script_type"] == "module_start"
    assert create(first_phone, "new-category").replayed
    process = create(first_phone, "new-process")
    assert process.receipt.response_body["created_category"] is False
    assert process.receipt.response_body["script_type"] == "module_process"
    other = create(second_phone, "other-category")
    assert other.receipt.response_body["created_category"] is True
    assert (
        other.receipt.response_body["application_id"]
        != first.receipt.response_body["application_id"]
    )

    app_id = UUID(first.receipt.response_body["application_id"])
    process_id = UUID(process.receipt.response_body["script_id"])
    commands = MaaScriptCommandService(factory)
    with pytest.raises(MaaDomainError) as empty:
        commands.save_document(
            script_id=process_id,
            expected_row_version=1,
            device_id=first_phone,
            manifest={
                "version": 2,
                "script_type": "module_process",
                "target": {"application_package": package},
                "steps": [],
            },
            idempotency_key="empty",
            correlation_id=uuid4(),
        )
    assert empty.value.code == "empty_script"
    saved = commands.save_document(
        script_id=process_id,
        expected_row_version=1,
        device_id=first_phone,
        manifest={
            "version": 2,
            "script_type": "module_process",
            "target": {"application_package": package},
            "steps": [{"action": "wait", "seconds": 1}],
        },
        idempotency_key="save-process",
        correlation_id=uuid4(),
    )
    assert saved.receipt.response_body["script"]["current_version_id"]
    with pytest.raises(MaaDomainError) as unauthorized:
        commands.save_document(
            script_id=process_id,
            expected_row_version=3,
            device_id=second_phone,
            manifest={
                "version": 2,
                "script_type": "module_process",
                "target": {"application_package": package},
                "steps": [{"action": "wait", "seconds": 1}],
            },
            idempotency_key="wrong-phone",
            correlation_id=uuid4(),
        )
    assert unauthorized.value.code == "script_device_not_applicable"

    binding = MaaApplicabilityService(factory)
    with pytest.raises(MaaDomainError) as conflict:
        binding.bind(application_id=app_id, device_id=second_phone, idempotency_key="bind-conflict")
    assert conflict.value.code == "application_binding_conflict"

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(MaaApplicationRow)) == 2
        assert session.scalar(select(func.count()).select_from(MaaApplicationDeviceRow)) == 2
        assert session.scalar(select(func.count()).select_from(MaaScriptRow)) == 5
        ends = session.scalars(
            select(MaaScriptRow).where(
                MaaScriptRow.application_id == app_id,
                MaaScriptRow.script_type == "module_end",
            )
        ).all()
        assert len(ends) == 1 and ends[0].current_version_id is not None

    MaaCatalogCommandService(factory).delete_application(
        application_id=app_id,
        expected_row_version=1,
        idempotency_key="delete-category",
        correlation_id=uuid4(),
    )
    with Session(engine) as session:
        assert session.get(MaaApplicationRow, app_id).deleted_at is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(MaaScriptRow)
                .where(
                    MaaScriptRow.application_id == app_id,
                    MaaScriptRow.deleted_at.is_(None),
                )
            )
            == 0
        )
