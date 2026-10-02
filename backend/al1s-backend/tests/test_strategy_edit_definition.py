from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from al1s.maa.catalog_service import MaaCatalogQueryService
from al1s.maa.errors import MaaDomainError
from al1s.maa.types import StrategyModuleRole


def fixture():
    identity, version_id = uuid4(), uuid4()
    strategy = SimpleNamespace(strategy_id=identity, current_version_id=version_id)
    modules = [SimpleNamespace(position=i, script_version_id=uuid4(), module_role=role,
                               wait_after_ms=i * 100)
               for i, role in enumerate((StrategyModuleRole.START, StrategyModuleRole.PROCESS,
                                         StrategyModuleRole.END))]
    version = SimpleNamespace(strategy_id=identity, strategy_version_id=version_id,
                              manifest={"default_parameters": {"nested": {"keep": True}}})
    uow = SimpleNamespace(strategies=Mock(), strategy_versions=Mock(), script_versions=Mock())
    uow.strategies.get_active.return_value = strategy
    uow.strategy_versions.get.return_value = version
    uow.strategy_versions.list_modules.return_value = list(reversed(modules))
    owners = {m.script_version_id: uuid4() for m in modules}
    uow.script_versions.script_ids_for_versions.return_value = owners
    return MaaCatalogQueryService(lambda: nullcontext(uow)), uow, strategy, version, modules, owners


def test_order_waits_parameters_and_single_id_batch():
    service, uow, strategy, version, modules, owners = fixture()
    current, definition = service.get_strategy_edit_definition(strategy.strategy_id)
    assert current is strategy
    assert definition["start_script_id"] == owners[modules[0].script_version_id]
    assert definition["end_script_id"] == owners[modules[2].script_version_id]
    assert definition["process_modules"] == [
        {"script_id": owners[modules[1].script_version_id], "wait_after_ms": 100}
    ]
    definition["default_parameters"]["nested"]["keep"] = False
    assert version.manifest["default_parameters"]["nested"]["keep"] is True
    uow.script_versions.script_ids_for_versions.assert_called_once()
    uow.script_versions.get.assert_not_called()
    uow.script_versions.find_by_ids.assert_not_called()


@pytest.mark.parametrize("failure", ["missing", "version", "roles", "dangling"])
def test_invalid_data_is_not_guessed(failure):
    service, uow, strategy, version, modules, owners = fixture()
    if failure == "missing":
        uow.strategies.get_active.return_value = None
    elif failure == "version":
        version.strategy_id = uuid4()
    elif failure == "roles":
        modules[0].module_role = StrategyModuleRole.END
    else:
        owners.clear()
    with pytest.raises(MaaDomainError):
        service.get_strategy_edit_definition(strategy.strategy_id)
