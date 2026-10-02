from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.execution.errors import ConflictError, NotFoundError
from al1s.execution.services import ExecutionResourceService


def setup_service():
    uow = MagicMock()
    uow.__enter__.return_value = uow
    return ExecutionResourceService(lambda: uow), uow


def test_missing_terminal_has_no_capability():
    service, uow = setup_service()
    uow.terminals.get_active.return_value = None
    with pytest.raises(NotFoundError):
        service.current_capability(uuid4())
    uow.capabilities.get.assert_not_called()


def test_not_yet_reported_is_empty_without_extra_query():
    service, uow = setup_service()
    uow.terminals.get_active.return_value = SimpleNamespace(current_capability_profile_id=None)
    assert service.current_capability(uuid4()) is None
    uow.capabilities.get.assert_not_called()


def test_only_terminal_owned_profile_is_returned():
    service, uow = setup_service()
    terminal_id, profile_id = uuid4(), uuid4()
    uow.terminals.get_active.return_value = SimpleNamespace(
        current_capability_profile_id=profile_id)
    profile = SimpleNamespace(terminal_id=terminal_id)
    uow.capabilities.get.return_value = profile
    assert service.current_capability(terminal_id) is profile
    uow.capabilities.get.assert_called_once_with(profile_id)
    profile.terminal_id = uuid4()
    with pytest.raises(ConflictError):
        service.current_capability(terminal_id)
