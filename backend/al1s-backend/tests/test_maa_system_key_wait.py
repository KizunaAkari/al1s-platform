from datetime import UTC, datetime
from uuid import uuid4

import pytest

from al1s.execution.definitions import canonical_manifest_hash
from al1s.maa.compiler import MaaRegisteredActionCompiler
from al1s.maa.types import ScriptVersionRecord
from al1s.maa.validation import validate_script_document


def document(action, **values):
    return {
        "version": 2,
        "script_type": "module_process",
        "target": {"application_package": "test.app"},
        "steps": [{"action": action, **values}],
    }


@pytest.mark.parametrize("action", ["back", "home", "task_view"])
def test_system_key_accepts_numeric_before_and_after_wait_and_rejects_invalid_values(action):
    for value in [None, 0, 1.25, 14400]:
        assert validate_script_document(
            document(
                action, wait_before_execution_seconds=value, wait_after_execution_seconds=value
            )
        ).valid
    for field in ["wait_before_execution_seconds", "wait_after_execution_seconds"]:
        for value in [-1, 14401, "1", True, float("nan")]:
            assert any(
                issue.pointer.endswith(field)
                for issue in validate_script_document(document(action, **{field: value})).issues
            )


@pytest.mark.parametrize("before,after", [(None, None), (0, 0), (1.25, 2.5), (0, 2)])
def test_only_nonzero_system_wait_requires_new_terminal_capability(before, after):
    manifest = document(
        "back", wait_before_execution_seconds=before, wait_after_execution_seconds=after
    )
    version = ScriptVersionRecord(
        uuid4(), uuid4(), 1, 2, canonical_manifest_hash(manifest), manifest, datetime.now(UTC)
    )
    step = MaaRegisteredActionCompiler().compile(version, []).definition["steps"][0]
    assert "wait_before_execution_seconds" not in step["parameters"]
    assert "wait_after_execution_seconds" not in step["parameters"]
    assert step["wrappers"] == (
        [
            {
                "kind": "system_key_wait",
                "handler_id": "maa.wrapper.system_key_wait",
                "parameters": {"before_seconds": before or 0, "after_seconds": after or 0},
            }
        ]
        if before or after
        else []
    )


def test_pre_wait_is_not_a_generic_action_parameter():
    assert not validate_script_document(
        document("wait", seconds=1, wait_before_execution_seconds=1)
    ).valid
