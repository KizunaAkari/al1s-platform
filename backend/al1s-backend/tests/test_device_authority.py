from datetime import UTC, datetime, timedelta
from uuid import uuid4

from al1s.execution.device_authority import DevicePath, choose_path, handoff_allowed
from al1s.execution.device_path_service import DevicePathService,PathState
from al1s.execution.errors import ConflictError
import pytest


def path(kind, now, **changes):
    return DevicePath(terminal_id=uuid4(), kind=kind, instance_id=uuid4(),
                      observed_at=now, heartbeat_at=now, ready_count=2,
                      control_ready=True, authority_protocol=1,
                      capabilities=frozenset({"maa", "editor"}), **changes)


def test_linux_has_priority_only_when_fresh_and_qualified():
    now = datetime.now(UTC)
    linux, android = path("linux", now), path("android", now)
    assert choose_path([android, linux], {"maa"}, now) == linux
    assert choose_path([path("linux", now - timedelta(seconds=31)), android],
                       {"maa"}, now) == android
    assert choose_path([linux, android], {"missing"}, now) is None


def test_handoff_requires_physical_release_and_settled_work():
    assert not handoff_allowed(physical_release=False, unsettled_work=False, editor_open=False)
    assert not handoff_allowed(physical_release=True, unsettled_work=True, editor_open=False)
    assert not handoff_allowed(physical_release=True, unsettled_work=False, editor_open=True)
    assert handoff_allowed(physical_release=True, unsettled_work=False, editor_open=False)


class MemoryTransaction:
    def __init__(self,state):self.state=state
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def load(self,devices,now):return [self.state]
    def save(self,states,now):self.state=states[0]
    def view(self,device,now):return {'state':self.state.state,'generation':self.state.generation}


def test_requested_handoff_checks_generation_policy_and_unsettled_work():
    now=datetime.now(UTC);apk,linux=path('android',now),path('linux',now)
    state=PathState(uuid4(),apk.terminal_id,apk.instance_id,None,1,'active',[apk,linux],False)
    transaction=MemoryTransaction(state)
    service=DevicePathService(lambda:transaction)
    with pytest.raises(ConflictError):service.request_handoff(state.device_id,linux.terminal_id,2)
    with pytest.raises(ConflictError):service.request_handoff(state.device_id,apk.terminal_id,1)
    state.pending_control=True
    with pytest.raises(ConflictError):service.request_handoff(state.device_id,linux.terminal_id,1)
    state.pending_control=False
    assert service.request_handoff(state.device_id,linux.terminal_id,1)['state']=='releasing'
    assert state.holder==apk.terminal_id and state.requested==linux.terminal_id
    assert service.request_handoff(state.device_id,linux.terminal_id,1)['state']=='releasing'
