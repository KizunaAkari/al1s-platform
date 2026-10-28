from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from al1s.execution.resource_targets import TargetDeviceOperations
from al1s.execution.types import TargetDeviceMode, TargetDeviceRecord


@pytest.mark.parametrize("mode", [TargetDeviceMode.MOUNTED, TargetDeviceMode.STANDALONE])
def test_old_control_observation_is_unknown_and_does_not_assert_disconnection(mode):
    now = datetime.now(UTC)
    device = TargetDeviceRecord(uuid4(), "phone", "android", mode, uuid4(), 1, now, now, None)
    stale = TargetDeviceOperations._with_availability(
        device, ("online", "device", now - timedelta(seconds=31)), now
    )
    assert stale.availability == "unknown"
    assert stale.availability_reason == "adb_observation_stale"
    assert (
        TargetDeviceOperations._with_availability(
            device, ("online", "device", now), now
        ).availability
        == "connected"
    )
