from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from al1s.maa.errors import MaaDomainError
from al1s.maa.script_metadata import rename_script


def invoke(uow, *, expected=4, name="new"):
    return rename_script(uow, script_id=uuid4(), expected_version=expected,
                         name=name, normalized_name=name, now=datetime.now(UTC),
                         correlation_id=uuid4())


def fixture_uow():
    uow = SimpleNamespace(scripts=Mock(), audit=Mock(), outbox=Mock())
    uow.scripts.get_active.return_value = SimpleNamespace(name="old", row_version=4)
    uow.scripts.update_name.return_value = SimpleNamespace(name="new", row_version=5)
    return uow


def test_rename_only_updates_metadata_and_records_event():
    uow = fixture_uow()
    assert invoke(uow).name == "new"
    uow.scripts.update_name.assert_called_once()
    uow.audit.add.assert_called_once()
    uow.outbox.add.assert_called_once()
    assert [call[0] for call in uow.scripts.method_calls] == ["get_active", "update_name"]


def test_same_name_does_not_create_version_or_event():
    uow = fixture_uow()
    invoke(uow, name="old")
    uow.scripts.update_name.assert_not_called()
    uow.outbox.add.assert_not_called()


def test_stale_version_is_not_silently_overwritten():
    uow = fixture_uow()
    with pytest.raises(MaaDomainError):
        invoke(uow, expected=3)
    uow.scripts.update_name.assert_not_called()


def test_missing_script_rejected():
    uow = fixture_uow()
    uow.scripts.get_active.return_value = None
    with pytest.raises(MaaDomainError):
        invoke(uow)
