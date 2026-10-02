from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from al1s.execution.definitions import canonical_manifest_hash
from al1s.execution.errors import ConflictError
from al1s.maa.types import ScriptBlobReference, ScriptVersionRecord
from al1s.maa.validation import RECOGNITION_ACTIONS

COMPILER_VERSION = "maa-registered-actions-v2"


@dataclass(frozen=True, slots=True)
class MaaActionDefinition:
    action_id: str
    handler_id: str
    provider_keys: tuple[str, ...] = ()


class MaaActionCompilerRegistry:
    """Map safe DSL action identifiers to deployed runtime handlers."""

    def __init__(self, definitions: tuple[MaaActionDefinition, ...] | None = None) -> None:
        values = definitions or _default_action_definitions()
        self._definitions = {item.action_id: item for item in values}
        if len(self._definitions) != len(values):
            raise ValueError("Maa action identifiers must be unique")

    def require(self, action_id: str) -> MaaActionDefinition:
        definition = self._definitions.get(action_id)
        if definition is None:
            raise ConflictError(
                "maa_action_handler_unavailable",
                "Published Maa action has no deployed runtime handler",
            )
        return definition


@dataclass(frozen=True, slots=True)
class CompiledMaaScript:
    definition: dict[str, Any]
    provider_keys: tuple[str, ...]


class MaaRegisteredActionCompiler:
    """Compile validated script DSL into a data-only registered-action plan."""

    def __init__(self, registry: MaaActionCompilerRegistry | None = None) -> None:
        self._registry = registry or MaaActionCompilerRegistry()

    def compile(
        self,
        version: ScriptVersionRecord,
        references: list[ScriptBlobReference],
        *,
        script_name: str | None = None,
        recovery_keys: dict[UUID, str] | None = None,
    ) -> CompiledMaaScript:
        if canonical_manifest_hash(version.manifest) != version.manifest_hash:
            raise ConflictError(
                "maa_script_manifest_hash_mismatch",
                "Published Maa script manifest hash is invalid",
            )
        pointer_keys = {item.json_pointer: resource_key_for(item) for item in references}
        providers = {"maa"}
        compiled_steps = [
            self._compile_step(
                index,
                step,
                pointer_keys,
                providers,
                recovery_keys or {},
            )
            for index, step in enumerate(version.manifest.get("steps", []))
        ]
        popups = self._compile_value(
            version.manifest.get("global_popups", []),
            "/global_popups",
            pointer_keys,
        )
        definition = {
            "schema_version": 1,
            "compiler_version": COMPILER_VERSION,
            "script_version_id": str(version.script_version_id),
            "script_name": (script_name or str(version.script_id)).strip(),
            "source_manifest_hash": version.manifest_hash,
            "script_type": version.manifest.get("script_type", "standard"),
            "target": copy.deepcopy(version.manifest.get("target", {})),
            "steps": compiled_steps,
            "independent_rules": popups,
            "cleanup_on_finish": version.manifest.get("cleanup_on_finish") is True,
        }
        return CompiledMaaScript(definition, tuple(sorted(providers)))

    def _compile_step(
        self,
        index: int,
        raw_step: Any,
        pointer_keys: dict[str, str],
        providers: set[str],
        recovery_keys: dict[UUID, str],
    ) -> dict[str, Any]:
        if not isinstance(raw_step, dict):
            raise ConflictError("maa_step_invalid", "Published Maa step is invalid")
        action_id = raw_step.get("action")
        if not isinstance(action_id, str):
            raise ConflictError("maa_action_invalid", "Published Maa action is invalid")
        action = self._registry.require(action_id)
        providers.update(action.provider_keys)
        if action_id == "recognize_execute" and raw_step.get("recognition_mode") == "text":
            providers.add("ocr")
        pointer = f"/steps/{index}"
        parameters = {
            key: self._compile_value(value, f"{pointer}/{_escape_pointer(key)}", pointer_keys)
            for key, value in raw_step.items()
            if key
            not in {
                "action",
                "failure_retry",
                "post_assertion",
                "skip_condition",
                "region_previews",
            }
            and not (key == "wait_after_execution_seconds" and action_id in RECOGNITION_ACTIONS)
            and not (
                key in {"wait_before_execution_seconds", "wait_after_execution_seconds"}
                and action_id in {"back", "home", "task_view"}
            )
            and not (key == "consecutive_match_count" and action_id == "wait_image")
        }
        wrappers = self._compile_wrappers(
            raw_step,
            pointer,
            pointer_keys,
            providers,
            recovery_keys,
        )
        return {
            "step_index": index + 1,
            "action_id": action.action_id,
            "handler_id": action.handler_id,
            "parameters": parameters,
            "wrappers": wrappers,
        }

    def _compile_wrappers(
        self,
        step: dict[str, Any],
        pointer: str,
        pointer_keys: dict[str, str],
        providers: set[str],
        recovery_keys: dict[UUID, str],
    ) -> list[dict[str, Any]]:
        wrappers = self._image_stability_wrappers(step)
        if (
            step.get("action") == "recognize_execute"
            and step.get("execution_mode") == "match_center"
        ):
            wrappers.append(
                {
                    "kind": "recognize_match_center",
                    "handler_id": "maa.wrapper.recognize_match_center",
                    "parameters": {},
                }
            )
        skip = step.get("skip_condition")
        if isinstance(skip, dict) and skip.get("enabled") is True:
            if skip.get("mode", "numeric") == "numeric":
                providers.add("ocr")
            wrapper = self._wrapper(
                "conditional_skip", skip, f"{pointer}/skip_condition", pointer_keys
            )
            if skip.get("mode") in {"recognition_failure", "execution_failure"}:
                wrapper["handler_id"] = "maa.wrapper.failure_skip"
            wrappers.append(wrapper)
        assertion = step.get("post_assertion")
        if isinstance(assertion, dict) and assertion.get("enabled") is True:
            wrapper = self._wrapper(
                "post_assertion",
                assertion,
                f"{pointer}/post_assertion",
                pointer_keys,
            )
            if assertion.get("recognition_mode", "image") == "text":
                providers.add("ocr")
                wrapper["handler_id"] = "maa.wrapper.post_assertion_text"
            wrappers.append(wrapper)
        retry = step.get("failure_retry")
        if isinstance(retry, dict) and retry.get("enabled") is True:
            wrappers.append(self._compile_failure_retry(retry, recovery_keys))
        seconds = step.get("wait_after_execution_seconds")
        if (
            step.get("action") in RECOGNITION_ACTIONS
            and isinstance(seconds, (int, float))
            and not isinstance(seconds, bool)
            and seconds > 0
        ):
            wrappers.append(
                {
                    "kind": "wait_after_execution",
                    "handler_id": "maa.wrapper.wait_after_execution",
                    "parameters": {"seconds": seconds},
                }
            )
        if step.get("action") in {"back", "home", "task_view"}:
            before = step.get("wait_before_execution_seconds") or 0
            after = step.get("wait_after_execution_seconds") or 0
            if before > 0 or after > 0:
                wrappers.append(
                    {
                        "kind": "system_key_wait",
                        "handler_id": "maa.wrapper.system_key_wait",
                        "parameters": {"before_seconds": before, "after_seconds": after},
                    }
                )
        return wrappers

    @staticmethod
    def _image_stability_wrappers(step: dict[str, Any]) -> list[dict[str, Any]]:
        count = step.get("consecutive_match_count")
        if step.get("action") != "wait_image" or type(count) is not int or count <= 1:
            return []
        return [
            {
                "kind": "wait_image_stability",
                "handler_id": "maa.wrapper.wait_image_stability",
                "parameters": {"consecutive_match_count": count},
            }
        ]

    def _wrapper(
        self,
        kind: str,
        value: dict[str, Any],
        pointer: str,
        pointer_keys: dict[str, str],
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "handler_id": f"maa.wrapper.{kind}",
            "parameters": self._compile_value(value, pointer, pointer_keys),
        }

    @staticmethod
    def _compile_failure_retry(
        value: dict[str, Any], recovery_keys: dict[UUID, str]
    ) -> dict[str, Any]:
        process_script_id = UUID(str(value["process_script_id"]))
        recovery_key = recovery_keys.get(process_script_id)
        if recovery_key is None:
            raise ConflictError(
                "maa_recovery_definition_missing",
                "Failure retry process script is absent from the execution closure",
            )
        return {
            "kind": "failure_retry",
            "handler_id": "maa.wrapper.failure_retry",
            "parameters": {
                "max_retries": int(value["max_retries"]),
                "recovery_definition_key": recovery_key,
            },
        }

    def _compile_value(self, value: Any, pointer: str, pointer_keys: dict[str, str]) -> Any:
        if isinstance(value, dict):
            if "$blob" in value:
                resource_key = pointer_keys.get(pointer)
                if resource_key is None:
                    raise ConflictError(
                        "maa_resource_closure_missing",
                        "Published Maa resource is absent from its immutable closure",
                    )
                return {
                    "$resource": resource_key,
                    "blob_id": str(value["$blob"]),
                    "media_type": value["media_type"],
                    "sha256": value["sha256"],
                    "size_bytes": value["size_bytes"],
                }
            return {
                key: self._compile_value(
                    child,
                    f"{pointer}/{_escape_pointer(str(key))}",
                    pointer_keys,
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [
                self._compile_value(child, f"{pointer}/{index}", pointer_keys)
                for index, child in enumerate(value)
            ]
        return copy.deepcopy(value)


def resource_key_for(reference: ScriptBlobReference) -> str:
    return f"maa/{reference.blob_id}/{reference.resource_role}"


def _default_action_definitions() -> tuple[MaaActionDefinition, ...]:
    native = {
        "back",
        "home",
        "task_view",
        "launch_app",
        "recognize_execute",
        "smart_swipe",
        "wait",
        "wait_click",
        "wait_image",
        "tap",
        "swipe",
    }
    custom = {"cleanup", "course_schedule", "feedback", "start", "screenshot", "wait_random"}
    return (
        tuple(MaaActionDefinition(action, f"maa.pipeline.{action}") for action in sorted(native))
        + tuple(
            MaaActionDefinition(action, f"maa.registered.{action}") for action in sorted(custom)
        )
        + (
            MaaActionDefinition("wait_text", "maa.pipeline.wait_text", ("ocr",)),
            MaaActionDefinition("click_text", "maa.pipeline.click_text", ("ocr",)),
        )
    )


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")
