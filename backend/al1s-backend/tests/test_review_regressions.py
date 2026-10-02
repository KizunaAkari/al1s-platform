from copy import deepcopy
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.execution.errors import ConflictError
from al1s.execution.scheduling_types import RetryOriginalSnapshotCommand
from al1s.execution.snapshot_retry_service import SnapshotRetryService
from al1s.lineup.catalog import SUPPORTED_MODEL_VERSIONS, catalog
from al1s.lineup.results import validated_result
from tests.test_lineup_partial import partial_result


@pytest.mark.parametrize("model", sorted(SUPPORTED_MODEL_VERSIONS))
@pytest.mark.parametrize("key", ["image_id", "ocr_id"])
def test_missing_nullable_slot_keys_are_invalid_instead_of_raising(model, key):
    raw = deepcopy(partial_result())
    raw["model_version"] = model
    raw.update(
        teams=["attack", "defense"],
        team_sizes={"attack": 6, "defense": 6},
        layout_hint="auto",
        attack_side="left",
    )
    for slot in raw["slots"]:
        slot.update(present=True, box=[0, 0, 8, 8], image_id=None, ocr_id=None)
    record = {"catalog_version": catalog()["version"], "width": 150, "height": 100}
    assert validated_result(raw, record) is not None
    del raw["slots"][0][key]
    assert (
        validated_result(
            raw, {"catalog_version": catalog()["version"], "width": 150, "height": 100}
        )
        is None
    )


@pytest.mark.parametrize("source", ["lineup", "lineup_batch"])
def test_generic_retry_rejects_lineup_before_idempotent_lookup_or_writes(source):
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.tasks.get.return_value.source_module = source
    service = SnapshotRetryService(lambda: uow, MagicMock())
    with pytest.raises(ConflictError) as error:
        service.retry(
            uuid4(),
            RetryOriginalSnapshotCommand(idempotency_key="same-key"),
            correlation_id=uuid4(),
        )
    assert error.value.code == "lineup_retry_requires_workspace"
    uow.tasks.find_by_idempotency_key.assert_not_called()
    uow.tasks.add.assert_not_called()
