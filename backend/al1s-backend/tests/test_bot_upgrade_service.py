from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest

from al1s.components.contracts import ComponentTarget, ComponentRelease, ComponentInventoryEntry
from al1s.components.linux_adapter import CompositeUpgradeHost
from al1s.components.operations import ComponentUpgradeService
from al1s.components.status_capability import StatusCapability
from al1s.execution.errors import ConflictError
from tests.test_bot_upgrade_driver import upgrade_payload


def test_bot_targets_use_existing_local_maintenance_host():
    host=CompositeUpgradeHost(MagicMock(),SimpleNamespace(connection=object()),SimpleNamespace(connections={}))
    assert host.available(ComponentTarget(kind='bot',bot_alias='qq'))
    assert host.available(ComponentTarget(kind='bot',bot_alias='discord'))


def test_preflight_rejects_bot_alias_mismatch_before_inventory():
    _,payload=upgrade_payload()
    release=ComponentRelease.model_validate({**payload['release'],'component':'discord-bot',
        'native_version':'0.1.0','compatible_from':'0'})
    store,inventory=MagicMock(),MagicMock()
    store.get_release.return_value=release
    inventory.get.return_value=ComponentInventoryEntry(component='discord-bot',upgrade_unit='discord-bot',
        target=ComponentTarget(kind='bot',bot_alias='discord'),display_name='Discord',version='0.1.0',
        architecture='amd64',status='healthy',source_identity='a'*64)
    service=ComponentUpgradeService(store,inventory,MagicMock(),StatusCapability('x'*32))
    with pytest.raises(ConflictError,match='target'):
        service.preflight(ComponentTarget(kind='bot',bot_alias='qq'),release.release_id)
    inventory.get.assert_not_called()
