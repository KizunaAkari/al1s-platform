from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.components.contracts import ComponentOperation
from al1s.components.linux_adapter import CompositeUpgradeHost
from al1s.components.types import TerminalProfile
from al1s.components.inventory import terminal_entries
from al1s.execution.errors import ConflictError


def bridge():
    identity, terminal = uuid4(), uuid4()
    operation = ComponentOperation(operation_id=identity,component="linux-terminal",target={"kind":"terminal","terminal_id":terminal},release_id=uuid4(),expected_source="a"*64,state="unknown",stage="unconfirmed",created_at=datetime.now(UTC),updated_at=datetime.now(UTC),row_version=1)
    store, linux = MagicMock(), MagicMock()
    linux.store.get.return_value=None
    linux.connections={terminal: object()}
    health={"boot_id":str(uuid4()),"container_id":"a"*64,"container_started_at":"now","unresolved_upgrade_id":None,"healthy":True}
    linux.client.return_value.request.return_value=health
    profile=TerminalProfile(terminal,"Linux","online","agent","aarch64",datetime.now(UTC),{})
    store.get_linux_profile.return_value=profile
    service=CompositeUpgradeHost(store,MagicMock(),linux)
    operation=operation.model_copy(update={"expected_source":service.fingerprint(terminal_entries(profile)[0],health)})
    store.get_operation.return_value=operation
    return service,operation


def test_linux_bridge_uses_one_original_command_identity_and_fixed_deadline():
    service, operation = bridge()
    result=service.submit(operation.operation_id)
    request=service.linux.submit.call_args.args[1]
    assert request.command_id==operation.operation_id
    assert request.release_id==operation.release_id
    assert request.expires_at==operation.created_at.timestamp()+600
    assert result["operation_id"]==str(operation.operation_id)


def test_linux_recovery_cannot_reconcile_another_physical_upgrade():
    service, operation = bridge()
    service.linux.client.return_value.request.return_value={"unresolved_upgrade_id":str(uuid4())}
    with pytest.raises(ConflictError,match="different terminal upgrade"):
        service.reconcile(operation.operation_id)
    assert service.linux.client.return_value.request.call_count==1


def test_linux_safe_recovery_uses_existing_deployer_evidence():
    service, operation = bridge()
    service.linux.store.get.return_value=SimpleNamespace(state="failed")
    service.linux.client.return_value.request.side_effect=[{"unresolved_upgrade_id":str(operation.operation_id)},{"deployment_id":str(operation.operation_id),"resolved":True,"status":"rolled_back"}]
    receipt=service.reconcile(operation.operation_id)
    assert receipt["state"]=="failed_safe" and receipt["proof"] is True
    assert service.linux.client.return_value.request.call_args.args == ("/v1/upgrades/recover", {"deployment_id":str(operation.operation_id)})


def test_linux_recreated_between_preflight_and_submit_never_posts_command():
    service, operation=bridge()
    service.linux.client.return_value.request.return_value['container_id']='b'*64
    with pytest.raises(ConflictError,match='changed after preflight'):
        service.submit(operation.operation_id)
    service.linux.submit.assert_not_called()


def test_same_sdk_upgrade_waits_for_new_heartbeat_without_requiring_new_profile():
    service, operation=bridge()
    completed=datetime.now(UTC)
    service.store.get_release.return_value=SimpleNamespace(maa_version='v5.12.1')
    service.linux.get.return_value=SimpleNamespace(state='succeeded',late_state=None,error_code=None,completed_at=completed)
    profile=TerminalProfile(operation.target.terminal_id,'Linux','online','agent','aarch64',operation.created_at-timedelta(hours=1),{'maa':{'version':'v5.12.1','execution_ready':True}},completed-timedelta(seconds=1))
    service.store.get_linux_profile.return_value=profile
    assert service.status(operation.operation_id)['state']=='verifying'
    from dataclasses import replace
    service.store.get_linux_profile.return_value=replace(profile,last_seen_at=completed+timedelta(seconds=1))
    assert service.status(operation.operation_id)['state']=='succeeded'
