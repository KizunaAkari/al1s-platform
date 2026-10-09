"""Runs only against the separately created Android tmpfs fixture."""
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.device_path_models import DeviceAuthorityRow
from al1s.adapters.postgres.device_path_repository import DevicePathTransaction
from al1s.adapters.postgres.execution_models import (
    TargetDeviceIdentifierRow, TargetDeviceRow, TerminalRow, TerminalCapabilityProfileRow,
)
from al1s.execution.device_path_service import DevicePathService, PathObservation
from al1s.execution.errors import ConflictError
from al1s.adapters.postgres.execution_repositories import PostgresTargetDeviceRepository


@pytest.fixture
def database():
    path = Path(__file__).resolve().parents[3] / "artifacts/android-direct/path-test-db.json"
    if not path.exists():
        pytest.skip("Dedicated Android fixture has not been created")
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["container"] == "al1s-android-direct-test-20261006"
    assert config["tmpfs"] and not config["production_data_used"]
    engine = create_engine(config["dsn"])
    try:
        yield sessionmaker(engine, expire_on_commit=False), engine
    finally:
        engine.dispose()


def terminal(kind, now):
    return TerminalRow(id=uuid4(), installation_id=uuid4(), terminal_type=kind,
                       display_name="path fixture", service_status="online",
                       acceptance_status="accepting", agent_version="fixture", last_seen_at=now)


def identifier(device, endpoint, kind, now):
    return TargetDeviceIdentifierRow(id=uuid4(), source_terminal_id=endpoint,
        target_device_id=device, source_type=kind, display_hint="fixture",
        identifier_digest=hashlib.sha256(str(uuid4()).encode()).hexdigest(),
        observed_at=now, row_version=1)


def test_takeover_waits_for_exact_old_holder_release(database):
    sessions, _ = database
    now = datetime.now(UTC)
    android, linux = terminal("android", now), terminal("linux", now)
    device = TargetDeviceRow(id=uuid4(), display_name="same phone", mode="standalone",
                             managing_terminal_id=android.id, row_version=1, updated_at=now)
    with sessions.begin() as tx:
        tx.add_all([android, linux]); tx.flush(); tx.add(device); tx.flush()
        tx.add_all([identifier(device.id, android.id, "apk_installation", now),
                    identifier(device.id, linux.id, "adb_serial", now)])
    service = DevicePathService(lambda: DevicePathTransaction(sessions))
    apk_epoch, linux_epoch = uuid4(), uuid4()
    apk = PathObservation(device.id, apk_epoch, True)
    mounted = PathObservation(device.id, linux_epoch, True)
    service.observe(android.id, [apk]); service.observe(android.id, [apk])
    service.observe(linux.id, [mounted]); service.observe(linux.id, [mounted])
    releasing = service.observe(android.id, [apk])[0]
    assert releasing["release_requested"] and not releasing["input_granted"]
    wrong = PathObservation(device.id, apk_epoch, False, int(releasing["generation"]) + 1)
    assert service.observe(android.id, [wrong])[0]["state"] == "releasing"
    release = PathObservation(device.id, apk_epoch, False, int(releasing["generation"]))
    assert service.observe(android.id, [release])[0]["holder_terminal_id"] == str(linux.id)
    with sessions() as tx:
        current = tx.get(TargetDeviceRow, device.id)
        assert current.mode == "mounted" and current.managing_terminal_id == linux.id
        authority = tx.get(DeviceAuthorityRow, device.id)
        assert authority.generation == 2 and authority.instance_id == linux_epoch


def test_hundred_observations_use_bounded_reads_and_reject_unassociated_device(database):
    sessions, engine = database
    now = datetime.now(UTC)
    endpoint = terminal("linux", now)
    devices = [TargetDeviceRow(id=uuid4(), display_name="fixture", mode="unassigned",
                              row_version=1, updated_at=now) for _ in range(100)]
    with sessions.begin() as tx:
        tx.add(endpoint); tx.flush(); tx.add_all(devices); tx.flush()
        tx.add_all([identifier(d.id, endpoint.id, "adb_serial", now) for d in devices])
    reads = []
    def count(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            reads.append(statement)
    event.listen(engine, "before_cursor_execute", count)
    service = DevicePathService(lambda: DevicePathTransaction(sessions))
    items = [PathObservation(d.id, uuid4(), True) for d in devices]
    try:
        assert len(service.observe(endpoint.id, items)) == 100
        assert len(reads) <= 8
    finally:
        event.remove(engine, "before_cursor_execute", count)
    with pytest.raises(ConflictError):
        service.observe(endpoint.id, [PathObservation(uuid4(), uuid4(), True)])


def test_frozen_ocr_requirement_can_select_android_over_linux_without_ocr(database):
    sessions,_=database
    now=datetime.now(UTC);linux,android=terminal("linux",now),terminal("android",now)
    device=TargetDeviceRow(id=uuid4(),display_name="capability fixture",mode="mounted",
                          managing_terminal_id=linux.id,row_version=1,updated_at=now)
    with sessions.begin() as tx:
        tx.add_all([linux,android]);tx.flush();tx.add(device);tx.flush()
        tx.add_all([identifier(device.id,linux.id,"adb_serial",now),identifier(device.id,android.id,"apk_installation",now)])
        for endpoint,providers in ((linux,["maa"]),(android,["maa","ocr"])):
            profile=TerminalCapabilityProfileRow(id=uuid4(),terminal_id=endpoint.id,revision=1,
                schema_version=1,protocol_version=1,agent_version="fixture",os_name=endpoint.terminal_type,
                os_version="fixture",architecture="arm64",cpu_cores=4,memory_bytes=8*1024**3,
                storage_available_bytes=8*1024**3,accelerator_type=None,low_resource=False,
                provider_keys=providers,details={},manifest_hash="a"*64,created_at=now)
            tx.add(profile);tx.flush();endpoint.current_capability_profile_id=profile.id
    service=DevicePathService(lambda:DevicePathTransaction(sessions))
    linux_epoch,android_epoch=uuid4(),uuid4()
    for endpoint,epoch in ((linux,linux_epoch),(android,android_epoch)):
        observation=PathObservation(device.id,epoch,True)
        service.observe(endpoint.id,[observation]);service.observe(endpoint.id,[observation])
    service.qualify({device.id:{"maa","ocr"}})
    release=service.observe(linux.id,[PathObservation(device.id,linux_epoch,True)])[0]
    assert release["release_requested"]
    service.observe(linux.id,[PathObservation(device.id,linux_epoch,False,int(release["generation"]))])
    with sessions() as tx:
        current=tx.get(TargetDeviceRow,device.id)
        assert current.managing_terminal_id==android.id and current.mode=="standalone"


def test_legacy_mode_writer_cannot_bypass_live_path_authority(database):
    sessions,_=database
    now=datetime.now(UTC);endpoint=terminal("android",now)
    device=TargetDeviceRow(id=uuid4(),display_name="mode guard",mode="standalone",
        managing_terminal_id=endpoint.id,row_version=1,updated_at=now)
    with sessions.begin() as tx:
        tx.add(endpoint);tx.flush();tx.add(device);tx.flush()
        tx.add(identifier(device.id,endpoint.id,"apk_installation",now))
    service=DevicePathService(lambda:DevicePathTransaction(sessions))
    service.observe(endpoint.id,[PathObservation(device.id,uuid4(),True)])
    with sessions() as tx:
        assert PostgresTargetDeviceRepository(tx).has_pending_control(device.id,endpoint.id)
