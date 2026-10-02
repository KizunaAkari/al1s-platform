import pytest

from al1s.execution.storage import StorageObservation


@pytest.mark.parametrize(
    "free, previous, expected",
    [(20, False, False), (19, False, True), (24, True, True), (25, True, False), (19, True, True)],
)
def test_storage_threshold_hysteresis(free, previous, expected):
    assert (
        StorageObservation("/data/runtime", 100, 100 - free, free).alert_state(previous) is expected
    )


@pytest.mark.parametrize("values", [(0, 0, 0), (10, 11, 0), (10, 2, 9), (10, -1, 2), (True, 0, 0)])
def test_invalid_observation(values):
    with pytest.raises(ValueError):
        StorageObservation("/data/runtime", *values)
