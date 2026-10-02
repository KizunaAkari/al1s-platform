from copy import deepcopy

import pytest

from al1s.execution.scheduling_types import CapabilityRequirements, ResolvedExecutionDefinition
from al1s.maa.errors import MaaDomainError
from al1s.maa.single_step import select_step


def definition():
    manifest = {
        "definition_type": "quick_test",
        "entry_definition_key": "root",
        "definitions": {
            "root": {
                "steps": [
                    {"action_id": "home"},
                    {
                        "action_id": "wait",
                        "step_index": 2,
                        "wrappers": [
                            {
                                "kind": "conditional_skip",
                                "parameters": {"skip_to_step_index": 3, "value": 5},
                            },
                            {
                                "kind": "failure_retry",
                                "parameters": {"recovery_definition_key": "recovery"},
                            },
                        ],
                    },
                    {"action_id": "back"},
                ],
                "cleanup_on_finish": True,
                "independent_rules": [
                    {"name": "yes", "from_step_index": 2, "through_step_index": 3},
                    {"name": "no", "step_indexes": [1, 3]},
                ],
            },
            "recovery": {"steps": [{"action_id": "home"}]},
        },
    }
    return ResolvedExecutionDefinition("test", 1, "a" * 64, manifest, CapabilityRequirements())


def test_single_step_preserves_wrappers_and_only_applicable_rules():
    original = definition()
    before = deepcopy(original.manifest)
    selected = select_step(original, 2)
    root = selected.manifest["definitions"]["root"]
    assert len(root["steps"]) == 1
    assert root["steps"][0]["step_index"] == 1
    assert root["steps"][0]["wrappers"][0]["parameters"] == {"value": 5}
    assert root["steps"][0]["wrappers"][1]["kind"] == "failure_retry"
    assert root["independent_rules"] == [{"name": "yes", "step_indexes": [1]}]
    assert root["cleanup_on_finish"] is False
    assert selected.manifest["debug_step_number"] == 2
    assert selected.manifest_hash != original.manifest_hash
    assert original.manifest == before


@pytest.mark.parametrize("number", [0, 4, True, 1.5])
def test_invalid_step_rejected(number):
    with pytest.raises(MaaDomainError):
        select_step(definition(), number)
