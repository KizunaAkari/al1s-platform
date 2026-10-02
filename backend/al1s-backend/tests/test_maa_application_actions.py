from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from al1s.maa.catalog_service import MaaCatalogQueryService
from al1s.maa.errors import MaaDomainError


def test_application_list_counts_saved_scripts_for_page_in_one_batch():
    first = SimpleNamespace(application_id=uuid4())
    second = SimpleNamespace(application_id=uuid4())
    uow = SimpleNamespace(applications=Mock(), scripts=Mock())
    uow.applications.list_active.return_value = [first, second]
    uow.scripts.count_saved_by_applications.return_value = {first.application_id: 2}

    result = MaaCatalogQueryService(lambda: nullcontext(uow)).list_applications(
        after_id=None, limit=50
    )

    assert result == [(first, 2), (second, 0)]
    uow.scripts.count_saved_by_applications.assert_called_once_with(
        [first.application_id, second.application_id]
    )


def test_application_actions_allow_cascade_delete_after_active_task_check():
    application_id = uuid4()
    uow = SimpleNamespace(
        applications=Mock(), scripts=Mock(), strategies=Mock(),
    )
    uow.applications.get_active.return_value = object()
    service = MaaCatalogQueryService(lambda: nullcontext(uow))
    assert service.application_actions(application_id) == {"rename": None, "delete": None}
    uow.scripts.list_active.assert_not_called()
    uow.strategies.list_active.assert_not_called()


def test_missing_application_rejects_actions():
    uow = SimpleNamespace(applications=Mock(), scripts=Mock())
    uow.applications.get_active.return_value = None
    with pytest.raises(MaaDomainError):
        MaaCatalogQueryService(lambda: nullcontext(uow)).application_actions(uuid4())
    uow.scripts.list_active.assert_not_called()
