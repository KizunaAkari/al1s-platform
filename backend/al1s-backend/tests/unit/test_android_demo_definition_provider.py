from __future__ import annotations

import pytest

from al1s.android_demo.definition_provider import AndroidDemoExecutionDefinitionProvider
from al1s.execution.definitions import canonical_manifest_hash
from al1s.execution.errors import InvalidRequestError


def test_root_probe_requires_the_android_demo_provider() -> None:
    definition = AndroidDemoExecutionDefinitionProvider().resolve("root_probe", {})

    assert definition.revision_id == "xiaomi-root-demo-v1"
    assert definition.manifest == {
        "schema_version": 1,
        "executor": "xiaomi_root_demo",
        "action": "root_probe",
    }
    assert definition.manifest_hash == canonical_manifest_hash(definition.manifest)
    assert definition.capability_requirements.provider_keys == (
        "android.xiaomi.root.demo",
    )
    assert definition.capability_requirements.requires_target_device is True


@pytest.mark.parametrize("content_id", ["shell", "root_tap", ""])
def test_unknown_android_demo_actions_are_rejected(content_id: str) -> None:
    with pytest.raises(InvalidRequestError, match="not supported"):
        AndroidDemoExecutionDefinitionProvider().resolve(content_id, {})


def test_android_demo_parameters_are_rejected() -> None:
    with pytest.raises(InvalidRequestError, match="do not accept parameters"):
        AndroidDemoExecutionDefinitionProvider().resolve("root_probe", {"command": "id"})
