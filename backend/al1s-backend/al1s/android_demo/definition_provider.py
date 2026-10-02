from __future__ import annotations

from typing import Any

from al1s.execution.definitions import canonical_manifest_hash
from al1s.execution.errors import InvalidRequestError
from al1s.execution.scheduling_types import (
    CapabilityRequirements,
    ResolvedExecutionDefinition,
)


class AndroidDemoExecutionDefinitionProvider:
    """Resolve only diagnostics compiled into the Xiaomi root demo APK."""

    def resolve(
        self,
        logical_content_id: str,
        parameters: dict[str, Any],
    ) -> ResolvedExecutionDefinition:
        if parameters:
            raise InvalidRequestError(
                "android_demo_parameters_forbidden",
                "Android demo diagnostics do not accept parameters",
            )
        if logical_content_id != "root_probe":
            raise InvalidRequestError(
                "android_demo_action_unsupported",
                "Android demo diagnostic action is not supported",
            )
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "executor": "xiaomi_root_demo",
            "action": "root_probe",
        }
        return ResolvedExecutionDefinition(
            revision_id="xiaomi-root-demo-v1",
            schema_version=1,
            manifest_hash=canonical_manifest_hash(manifest),
            manifest=manifest,
            capability_requirements=CapabilityRequirements(
                schema_version=1,
                min_protocol_version=1,
                provider_keys=("android.xiaomi.root.demo",),
                requires_target_device=True,
            ),
        )
