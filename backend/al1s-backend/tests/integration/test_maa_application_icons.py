"""Persist, remove, and reacquire icons in a disposable Maa database."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.execution_models import TargetDeviceRow, TerminalRow
from al1s.adapters.postgres.maa_models import MaaApplicationRow
from al1s.adapters.postgres.maa_unit_of_work import MaaSqlAlchemyUnitOfWork
from al1s.maa.catalog_command_service import MaaCatalogCommandService
from al1s.maa.editor_creation import MaaEditorCreationService
from al1s.maa.errors import MaaDomainError
from tests.integration.maa_support import clean_maa_tables as clean_maa_tables
from tests.integration.maa_support import engine as engine

pytestmark = pytest.mark.integration


def test_icon_capture_delete_reacquire_and_upload(engine) -> None:
    terminal_id, phone_id = uuid4(), uuid4()
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
        session.add(
            TargetDeviceRow(
                id=phone_id,
                display_name="Phone",
                platform="android",
                mode="mounted",
                managing_terminal_id=terminal_id,
                row_version=1,
            )
        )
    def factory():
        return MaaSqlAlchemyUnitOfWork(sessionmaker(engine, expire_on_commit=False))
    creator = MaaEditorCreationService(factory)
    commands = MaaCatalogCommandService(factory)
    package = "com.example.icon"
    first_icon, next_icon, uploaded_icon = b"first", b"next", b"uploaded"

    assert creator.needs_icon(phone_id, package)
    first = creator.create(
        device_id=phone_id,
        package_name=package,
        name=None,
        idempotency_key="icon-first",
        correlation_id=uuid4(),
        icon_png=first_icon,
    )
    app_id = UUID(first.receipt.response_body["application_id"])
    assert not creator.needs_icon(phone_id, package)
    creator.create(
        device_id=phone_id,
        package_name=package,
        name="Process A",
        idempotency_key="icon-second",
        correlation_id=uuid4(),
        icon_png=next_icon,
    )
    with Session(engine) as session:
        assert (
            session.scalar(select(MaaApplicationRow.icon_png).where(MaaApplicationRow.id == app_id))
            == first_icon
        )

    removed = commands.set_application_icon(
        application_id=app_id,
        icon_png=None,
        expected_row_version=1,
        idempotency_key="icon-remove",
        correlation_id=uuid4(),
    )
    assert removed.receipt.response_body["row_version"] == 2
    assert creator.needs_icon(phone_id, package)
    creator.create(
        device_id=phone_id,
        package_name=package,
        name="Process B",
        idempotency_key="icon-third",
        correlation_id=uuid4(),
        icon_png=next_icon,
    )
    with Session(engine) as session:
        row = session.get(MaaApplicationRow, app_id)
        assert row.icon_png == next_icon and row.row_version == 3

    with pytest.raises(MaaDomainError) as conflict:
        commands.set_application_icon(
            application_id=app_id,
            icon_png=uploaded_icon,
            expected_row_version=3,
            idempotency_key="upload-without-delete",
            correlation_id=uuid4(),
        )
    assert conflict.value.code == "application_icon_exists"
    commands.set_application_icon(
        application_id=app_id,
        icon_png=None,
        expected_row_version=3,
        idempotency_key="icon-remove-again",
        correlation_id=uuid4(),
    )
    uploaded = commands.set_application_icon(
        application_id=app_id,
        icon_png=uploaded_icon,
        expected_row_version=4,
        idempotency_key="icon-upload",
        correlation_id=uuid4(),
    )
    assert uploaded.receipt.response_body["row_version"] == 5
    assert not creator.needs_icon(phone_id, package)
    with Session(engine) as session:
        row = session.get(MaaApplicationRow, app_id)
        assert row.icon_png == uploaded_icon and row.updated_at <= datetime.now(UTC)
