from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any, TypeGuard
from uuid import UUID

from al1s.maa.native_coordinates import validate_coordinates
from al1s.maa.types import (
    DeclaredBlobResource,
    ScriptType,
    ScriptValidationResult,
    ValidationIssue,
)

VALIDATOR_VERSION = "maa-dsl-v2.2"
RECOGNITION_ACTIONS = frozenset(
    {"recognize_execute", "wait_click", "wait_image", "wait_text", "click_text", "smart_swipe"}
)
BASE_ACTIONS = frozenset(
    {
        "back",
        "cleanup",
        "course_schedule",
        "feedback",
        "screenshot",
        "home",
        "task_view",
        "launch_app",
        "smart_swipe",
        "start",
        "wait",
        "wait_random",
        "recognize_execute",
        "wait_click",
        "wait_image",
        "wait_text",
        "click_text",
        "tap",
        "swipe",
    }
)
FORBIDDEN_CODE_KEYS = frozenset(
    {"code", "command_line", "executable", "javascript", "powershell", "python", "shell"}
)
TOP_LEVEL_FIELDS = frozenset(
    {"cleanup_on_finish", "global_popups", "script_type", "steps", "target", "version"}
)
COMMON_STEP_FIELDS = frozenset(
    {
        "action",
        "failure_retry",
        "post_assertion",
        "skip_condition",
        "timeout_seconds",
        "region_previews",
    }
)
ACTION_FIELDS: dict[str, frozenset[str]] = {
    "wait_text": frozenset({"text", "search_region", "poll_interval_seconds"}),
    "click_text": frozenset({"text", "search_region", "poll_interval_seconds"}),
    "tap": frozenset({"x", "y"}),
    "swipe": frozenset({"x1", "y1", "x2", "y2", "duration_ms"}),
    "start": frozenset(),
    "cleanup": frozenset(),
    "back": frozenset(),
    "home": frozenset(),
    "task_view": frozenset(),
    "feedback": frozenset({"message", "subject"}),
    "screenshot": frozenset(),
    "wait": frozenset({"seconds"}),
    "wait_random": frozenset({"min_seconds", "max_seconds"}),
    "recognize_execute": frozenset(
        {
            "recognition_mode",
            "execution_mode",
            "template_base64",
            "template_rect",
            "threshold",
            "text",
            "search_region",
            "poll_interval_seconds",
            "click",
            "swipe",
            "click_template_base64",
            "click_template_rect",
            "click_threshold",
            "execution_count",
            "execution_interval_ms",
        }
    ),
    "launch_app": frozenset({"activity", "force_stop_before_launch", "package", "wait_seconds"}),
    "wait_image": frozenset(
        {
            "consecutive_match_count",
            "poll_interval_seconds",
            "template_base64",
            "template_rect",
            "threshold",
            "timeout_seconds",
        }
    ),
    "smart_swipe": frozenset(
        {
            "mode",
            "poll_interval_seconds",
            "swipe",
            "swipe_duration_ms",
            "swipe_for_seconds",
            "template_base64",
            "template_rect",
            "threshold",
            "timeout_seconds",
            "wait_after_swipe_seconds",
        }
    ),
    "course_schedule": frozenset(
        {
            "course_action_timeout_seconds",
            "course_region_count",
            "course_target_avatars",
            "course_ticket_limit",
        }
    ),
    "wait_click": frozenset(
        {
            "click",
            "click_count",
            "click_interval_ms",
            "click_mode",
            "click_template_base64",
            "click_template_rect",
            "click_threshold",
            "image_branches",
            "marker_absence_checks",
            "marker_component_max_area",
            "marker_component_min_area",
            "marker_group_distance",
            "marker_hsv_lower",
            "marker_hsv_upper",
            "match_anchor",
            "match_index",
            "match_max_clicks",
            "match_offset_x",
            "match_offset_y",
            "match_order",
            "poll_interval_seconds",
            "search_region",
            "template_base64",
            "template_rect",
            "threshold",
            "timeout_seconds",
            "wait_after_click_seconds",
        }
    ),
}
RESOURCE_FIELD_SUFFIX = "_base64"
MAX_STEPS = 1_000
MAX_RUNTIME_SECONDS = 14_400


class ActionRegistry:
    def __init__(self, actions: Iterable[str] = BASE_ACTIONS) -> None:
        self._actions = frozenset(action.strip() for action in actions if action.strip())

    def contains(self, action: str) -> bool:
        return action in self._actions


def validate_script_document(
    document: dict[str, Any],
    registry: ActionRegistry | None = None,
    *,
    allow_inline_resources: bool = False,
) -> ScriptValidationResult:
    action_registry = registry or ActionRegistry()
    issues: list[ValidationIssue] = []
    _validate_top_level(document, issues)
    script_type = _script_type(document, issues)
    steps = _steps(document, issues)
    _find_forbidden_code(document, "", issues)
    _validate_resources(document, "", issues, allow_inline_resources)

    for index, step in enumerate(steps):
        pointer = f"/steps/{index}"
        if not isinstance(step, dict):
            issues.append(ValidationIssue("invalid_step", "Step must be an object", pointer))
            continue
        action = step.get("action")
        if not isinstance(action, str) or not action_registry.contains(action):
            issues.append(
                ValidationIssue(
                    "unregistered_action", "Step action is not registered", f"{pointer}/action"
                )
            )
        else:
            _validate_action(step, action, pointer, issues)
        if action in {"tap", "swipe", "recognize_execute"} and not (
            isinstance(document.get("target"), dict) and "screen_size" in document["target"]
        ):
            issues.append(
                ValidationIssue(
                    "native_screen_size_required",
                    "Coordinate actions require an original screenshot size",
                    "/target/screen_size",
                )
            )
        _validate_step_rules(step, pointer, index, len(steps), issues)
        _optional_number(step, "timeout_seconds", pointer, issues, 0.1, MAX_RUNTIME_SECONDS)

    _validate_lifecycle(document, steps, script_type, issues)
    _validate_global_popups(document.get("global_popups"), len(steps), issues)
    return ScriptValidationResult(script_type=script_type, issues=tuple(issues))


def declared_blob_resources(document: dict[str, Any]) -> tuple[DeclaredBlobResource, ...]:
    resources: list[DeclaredBlobResource] = []
    _collect_blob_resources(document, "", resources)
    return tuple(
        DeclaredBlobResource(
            blob_id=item.blob_id,
            sha256=item.sha256,
            media_type=item.media_type,
            size_bytes=item.size_bytes,
            json_pointer=item.json_pointer,
            resource_role=item.resource_role,
            ordinal=index,
        )
        for index, item in enumerate(resources)
    )


def normalize_legacy_end_script(document: dict[str, Any]) -> bool:
    """Apply the single migration-only end-script normalization approved by YY."""
    steps = document.get("steps")
    if (
        document.get("script_type") != ScriptType.MODULE_PROCESS.value
        or document.get("cleanup_on_finish") is not True
        or not isinstance(steps, list)
        or not steps
        or not isinstance(steps[-1], dict)
        or steps[-1].get("action") != "cleanup"
    ):
        return False
    document["script_type"] = ScriptType.MODULE_END.value
    steps.pop()
    return True


def _validate_top_level(document: dict[str, Any], issues: list[ValidationIssue]) -> None:
    for field in sorted(set(document) - TOP_LEVEL_FIELDS):
        issues.append(
            ValidationIssue(
                "unknown_script_field",
                "Script field is not supported",
                f"/{_escape_pointer(field)}",
            )
        )
    if document.get("version") != 2:
        issues.append(
            ValidationIssue("unsupported_script_schema", "Script version must be 2", "/version")
        )
    target = document.get("target")
    if not isinstance(target, dict):
        issues.append(
            ValidationIssue("invalid_target", "Script target must be an object", "/target")
        )
    elif not _non_empty_string(target.get("application_package"), 255):
        issues.append(
            ValidationIssue(
                "invalid_application_package",
                "Script target must contain an application package",
                "/target/application_package",
            )
        )
    if isinstance(target, dict) and "screen_size" in target:
        size = target["screen_size"]
        if (
            not isinstance(size, dict)
            or set(size) != {"width", "height"}
            or any(
                not _is_integer(size.get(key)) or not 1 <= size[key] <= 8192
                for key in ("width", "height")
            )
            or size["width"] * size["height"] > 16_777_216
        ):
            issues.append(
                ValidationIssue(
                    "invalid_screen_size",
                    "Screen size must contain bounded integer width and height",
                    "/target/screen_size",
                )
            )
        else:
            validate_coordinates(document, size["width"], size["height"], issues)


def _script_type(document: dict[str, Any], issues: list[ValidationIssue]) -> ScriptType | None:
    raw_type = document.get("script_type", ScriptType.STANDARD.value)
    try:
        return ScriptType(str(raw_type))
    except ValueError:
        issues.append(
            ValidationIssue(
                "unsupported_script_type", "Script type is not supported", "/script_type"
            )
        )
        return None


def _steps(document: dict[str, Any], issues: list[ValidationIssue]) -> list[Any]:
    value = document.get("steps")
    if not isinstance(value, list):
        issues.append(ValidationIssue("invalid_steps", "Steps must be an array", "/steps"))
        return []
    if len(value) > MAX_STEPS:
        issues.append(
            ValidationIssue(
                "too_many_steps", f"A script can contain at most {MAX_STEPS} steps", "/steps"
            )
        )
    return value


def _validate_action(
    step: dict[str, Any], action: str, pointer: str, issues: list[ValidationIssue]
) -> None:
    allowed = ACTION_FIELDS.get(action)
    if action in {"back", "home", "task_view"} and allowed is not None:
        allowed = allowed | {"wait_before_execution_seconds", "wait_after_execution_seconds"}
        _optional_number(
            step, "wait_before_execution_seconds", pointer, issues, 0, MAX_RUNTIME_SECONDS
        )
    if action in RECOGNITION_ACTIONS | {"back", "home", "task_view"} and allowed is not None:
        allowed = allowed | {"wait_after_execution_seconds"}
        _optional_number(
            step, "wait_after_execution_seconds", pointer, issues, 0, MAX_RUNTIME_SECONDS
        )
    if allowed is not None:
        for field in sorted(set(step) - COMMON_STEP_FIELDS - allowed):
            issues.append(
                ValidationIssue(
                    "unknown_action_field",
                    "Action parameter is not supported",
                    f"{pointer}/{_escape_pointer(field)}",
                )
            )
    if action in {"tap", "swipe"}:
        keys = ("x", "y") if action == "tap" else ("x1", "y1", "x2", "y2")
        for key in keys:
            _integer(step, key, pointer, issues, minimum=0, maximum=8191)
        if action == "swipe":
            _integer(step, "duration_ms", pointer, issues, minimum=1, maximum=60_000)
    elif action in {"wait_text", "click_text"}:
        _required_string(step, "text", pointer, issues, maximum=200)
        _optional_rect(step, "search_region", pointer, issues)
        _optional_number(step, "poll_interval_seconds", pointer, issues, 0.05, 10)
    elif action == "wait":
        _number(step, "seconds", pointer, issues, minimum=0, maximum=MAX_RUNTIME_SECONDS)
    elif action == "wait_random":
        _number(step, "min_seconds", pointer, issues, minimum=0, maximum=MAX_RUNTIME_SECONDS)
        _number(step, "max_seconds", pointer, issues, minimum=0, maximum=MAX_RUNTIME_SECONDS)
        lower, upper = step.get("min_seconds"), step.get("max_seconds")
        if _is_number(lower) and _is_number(upper) and lower > upper:
            issues.append(ValidationIssue("invalid_wait_range", "Wait range is reversed", pointer))
        timeout = step.get("timeout_seconds", MAX_RUNTIME_SECONDS)
        if _is_number(upper) and _is_number(timeout) and upper > timeout:
            issues.append(
                ValidationIssue(
                    "wait_exceeds_timeout",
                    "Wait exceeds step timeout",
                    pointer,
                )
            )
    elif action == "recognize_execute":
        _validate_recognize_execute(step, pointer, issues)
    elif action == "launch_app":
        _required_string(step, "package", pointer, issues, maximum=255)
        _optional_string_field(step, "activity", pointer, issues, maximum=255)
        _optional_bool(step, "force_stop_before_launch", pointer, issues)
        _optional_number(step, "wait_seconds", pointer, issues, 0, 300)
    elif action in {"wait_image", "smart_swipe", "wait_click"}:
        _recognition_parameters(step, pointer, issues)
    if action == "wait_image":
        _optional_integer(step, "consecutive_match_count", pointer, issues, 1, None)
    elif action == "smart_swipe":
        _validate_swipe(step, pointer, issues)
    elif action == "wait_click":
        _validate_click(step, pointer, issues)
    elif action == "course_schedule":
        _validate_course_schedule(step, pointer, issues)
    elif action == "feedback":
        _optional_string_field(step, "subject", pointer, issues, maximum=200)
        _optional_string_field(step, "message", pointer, issues, maximum=2_000)


def _recognition_parameters(
    value: dict[str, Any], pointer: str, issues: list[ValidationIssue]
) -> None:
    if "template_base64" not in value:
        issues.append(
            ValidationIssue(
                "recognition_template_missing",
                "Recognition action must contain a template",
                f"{pointer}/template_base64",
            )
        )
    _optional_rect(value, "template_rect", pointer, issues)
    _optional_number(value, "threshold", pointer, issues, 0.000001, 1)
    _optional_number(value, "timeout_seconds", pointer, issues, 0.1, MAX_RUNTIME_SECONDS)
    _optional_number(value, "poll_interval_seconds", pointer, issues, 0.05, 10)


def _validate_click(step: dict[str, Any], pointer: str, issues: list[ValidationIssue]) -> None:
    mode = step.get("click_mode", "fixed")
    allowed_modes = {
        "color_marker",
        "fixed",
        "image",
        "match_center",
        "match_offset",
        "template_center",
    }
    if mode not in allowed_modes:
        issues.append(
            ValidationIssue(
                "invalid_click_mode", "Click mode is not supported", f"{pointer}/click_mode"
            )
        )
    _optional_point(step, "click", pointer, issues)
    if mode == "fixed" and not isinstance(step.get("click"), dict):
        issues.append(
            ValidationIssue(
                "click_point_missing", "Fixed click requires coordinates", f"{pointer}/click"
            )
        )
    if mode == "image" and "click_template_base64" not in step:
        issues.append(
            ValidationIssue(
                "click_template_missing",
                "Image click requires a template",
                f"{pointer}/click_template_base64",
            )
        )
    if mode == "match_offset" and not isinstance(step.get("template_rect"), dict):
        issues.append(
            ValidationIssue(
                "click_template_rect_missing",
                "Offset click requires the template rectangle",
                f"{pointer}/template_rect",
            )
        )
    if mode in {"match_offset", "color_marker"} and step.get("image_branches"):
        issues.append(
            ValidationIssue(
                "incompatible_image_branches",
                "This click mode does not support image branches",
                f"{pointer}/image_branches",
            )
        )
    _optional_rect(step, "click_template_rect", pointer, issues)
    _optional_rect(step, "search_region", pointer, issues)
    _optional_number(step, "click_threshold", pointer, issues, 0.000001, 1)
    _optional_number(step, "wait_after_click_seconds", pointer, issues, 0, 300)
    _optional_integer(step, "click_count", pointer, issues, 1, None)
    _optional_integer(step, "click_interval_ms", pointer, issues, 0, 3_600_000)
    _optional_integer(step, "match_index", pointer, issues, 1, 10_000)
    _optional_integer(step, "match_max_clicks", pointer, issues, 1, 10_000)
    _optional_number(step, "match_offset_x", pointer, issues, -100_000, 100_000)
    _optional_number(step, "match_offset_y", pointer, issues, -100_000, 100_000)
    _validate_image_branches(step.get("image_branches"), pointer, issues)


def _validate_recognize_execute(
    step: dict[str, Any], pointer: str, issues: list[ValidationIssue]
) -> None:
    recognition = step.get("recognition_mode")
    if recognition == "image":
        _recognition_parameters(step, pointer, issues)
    elif recognition == "text":
        _required_string(step, "text", pointer, issues, maximum=200)
        _optional_rect(step, "search_region", pointer, issues)
        _optional_number(step, "poll_interval_seconds", pointer, issues, 0.05, 10)
    else:
        issues.append(
            ValidationIssue(
                "invalid_recognition_mode",
                "Recognition mode is not supported",
                f"{pointer}/recognition_mode",
            )
        )

    execution = step.get("execution_mode")
    if execution == "fixed_tap":
        _optional_point(step, "click", pointer, issues)
        if not isinstance(step.get("click"), dict):
            issues.append(
                ValidationIssue(
                    "click_point_missing",
                    "Fixed click requires coordinates",
                    f"{pointer}/click",
                )
            )
    elif execution == "fixed_swipe":
        _validate_swipe(step, pointer, issues)
    elif execution == "match_center":
        if recognition != "image":
            issues.append(
                ValidationIssue(
                    "match_center_requires_image",
                    "Image-center reuse requires image recognition",
                    f"{pointer}/execution_mode",
                )
            )
    elif execution == "image_center":
        if "click_template_base64" not in step:
            issues.append(
                ValidationIssue(
                    "click_template_missing",
                    "Image click requires a template",
                    f"{pointer}/click_template_base64",
                )
            )
        _optional_rect(step, "click_template_rect", pointer, issues)
        _optional_number(step, "click_threshold", pointer, issues, 0.000001, 1)
    else:
        issues.append(
            ValidationIssue(
                "invalid_execution_mode",
                "Execution mode is not supported",
                f"{pointer}/execution_mode",
            )
        )
    _optional_integer(step, "execution_count", pointer, issues, 1, None)
    _optional_integer(step, "execution_interval_ms", pointer, issues, 0, 3_600_000)


def _validate_swipe(step: dict[str, Any], pointer: str, issues: list[ValidationIssue]) -> None:
    mode = step.get("mode", "until_image")
    if mode not in {"after_image", "until_image"}:
        issues.append(
            ValidationIssue("invalid_swipe_mode", "Swipe mode is not supported", f"{pointer}/mode")
        )
    swipe = step.get("swipe")
    if not isinstance(swipe, dict):
        issues.append(
            ValidationIssue(
                "invalid_swipe", "Swipe coordinates must be an object", f"{pointer}/swipe"
            )
        )
    else:
        for field in ("x1", "y1", "x2", "y2"):
            _number(swipe, field, f"{pointer}/swipe", issues, minimum=0, maximum=100_000)
        _number(
            swipe,
            "duration_ms",
            f"{pointer}/swipe",
            issues,
            minimum=1,
            maximum=60_000,
        )
    _optional_integer(step, "swipe_duration_ms", pointer, issues, 1, 60_000)
    _optional_number(step, "swipe_for_seconds", pointer, issues, 0.1, MAX_RUNTIME_SECONDS)
    _optional_number(step, "wait_after_swipe_seconds", pointer, issues, 0, 300)


def _validate_course_schedule(
    step: dict[str, Any], pointer: str, issues: list[ValidationIssue]
) -> None:
    _integer(step, "course_region_count", pointer, issues, minimum=1, maximum=100)
    _integer(step, "course_ticket_limit", pointer, issues, minimum=1, maximum=1_000)
    _number(
        step,
        "course_action_timeout_seconds",
        pointer,
        issues,
        minimum=0.1,
        maximum=MAX_RUNTIME_SECONDS,
    )
    avatars = step.get("course_target_avatars")
    if not isinstance(avatars, list) or not 1 <= len(avatars) <= 100:
        issues.append(
            ValidationIssue(
                "invalid_course_targets",
                "Course schedule must contain 1 to 100 target avatars",
                f"{pointer}/course_target_avatars",
            )
        )
        return
    for index, avatar in enumerate(avatars):
        avatar_pointer = f"{pointer}/course_target_avatars/{index}"
        if not isinstance(avatar, dict):
            issues.append(
                ValidationIssue(
                    "invalid_course_target", "Course target must be an object", avatar_pointer
                )
            )
            continue
        _required_string(avatar, "name", avatar_pointer, issues, maximum=200)
        if "template_base64" not in avatar:
            issues.append(
                ValidationIssue(
                    "course_target_template_missing",
                    "Course target must contain a template",
                    f"{avatar_pointer}/template_base64",
                )
            )
        _optional_rect(avatar, "template_rect", avatar_pointer, issues)
        _optional_number(avatar, "threshold", avatar_pointer, issues, 0.000001, 1)
        _optional_integer(avatar, "screen_width", avatar_pointer, issues, 1, 100_000)
        _optional_integer(avatar, "screen_height", avatar_pointer, issues, 1, 100_000)


def _validate_step_rules(
    step: dict[str, Any],
    pointer: str,
    index: int,
    step_count: int,
    issues: list[ValidationIssue],
) -> None:
    _validate_skip(step, pointer, index, step_count, issues)
    _validate_assertion(step.get("post_assertion"), pointer, issues)
    _validate_failure_retry(step, pointer, issues)


def _validate_skip(
    step: dict[str, Any],
    pointer: str,
    index: int,
    step_count: int,
    issues: list[ValidationIssue],
) -> None:
    skip = step.get("skip_condition")
    if skip is None:
        return
    if not isinstance(skip, dict):
        issues.append(
            ValidationIssue(
                "invalid_skip_condition",
                "Skip condition must be an object",
                f"{pointer}/skip_condition",
            )
        )
        return
    if not isinstance(skip.get("enabled", False), bool):
        issues.append(
            ValidationIssue(
                "invalid_skip_enabled",
                "Skip enabled must be boolean",
                f"{pointer}/skip_condition/enabled",
            )
        )
        return
    if skip.get("enabled") is not True:
        return
    if step.get("action") == "start":
        issues.append(
            ValidationIssue(
                "start_step_cannot_skip",
                "Start step cannot be skipped",
                f"{pointer}/skip_condition",
            )
        )
    target = skip.get("skip_to_step_index")
    if target is not None and (
        not _is_integer(target) or not index + 2 <= int(target) <= step_count
    ):
        issues.append(
            ValidationIssue(
                "invalid_skip_target",
                "Skip target must be a later existing step using a 1-based index",
                f"{pointer}/skip_condition/skip_to_step_index",
            )
        )
    mode = skip.get("mode", "numeric")
    if mode not in {"image", "numeric", "recognition_failure", "execution_failure"}:
        issues.append(
            ValidationIssue(
                "invalid_skip_mode",
                "Skip mode is not supported",
                f"{pointer}/skip_condition/mode",
            )
        )
    if mode == "numeric":
        if skip.get("operator") not in {"gt", "lt"}:
            issues.append(
                ValidationIssue(
                    "invalid_skip_operator",
                    "Numeric skip operator is invalid",
                    f"{pointer}/skip_condition/operator",
                )
            )
        _number(skip, "value", f"{pointer}/skip_condition", issues)
        _optional_rect(skip, "region", f"{pointer}/skip_condition", issues)
    elif mode == "image":
        if "preview_base64" not in skip:
            issues.append(
                ValidationIssue(
                    "skip_template_missing",
                    "Image skip must contain a template",
                    f"{pointer}/skip_condition/preview_base64",
                )
            )
        _optional_rect(skip, "preview_rect", f"{pointer}/skip_condition", issues)
        _optional_number(skip, "threshold", f"{pointer}/skip_condition", issues, 0.000001, 1)


def _validate_assertion(value: Any, pointer: str, issues: list[ValidationIssue]) -> None:
    if value is None:
        return
    assertion_pointer = f"{pointer}/post_assertion"
    if not isinstance(value, dict):
        issues.append(
            ValidationIssue(
                "invalid_post_assertion", "Post assertion must be an object", assertion_pointer
            )
        )
        return
    if not isinstance(value.get("enabled", False), bool):
        issues.append(
            ValidationIssue(
                "invalid_assertion_enabled",
                "Assertion enabled must be boolean",
                f"{assertion_pointer}/enabled",
            )
        )
        return
    if value.get("enabled") is not True:
        return
    mode = value.get("recognition_mode", "image")
    if mode == "text":
        _required_string(value, "text", assertion_pointer, issues, maximum=200)
        _optional_rect(value, "search_region", assertion_pointer, issues)
    elif mode == "image":
        if "template_base64" not in value:
            issues.append(
                ValidationIssue(
                    "assertion_template_missing",
                    "Assertion must contain a template",
                    f"{assertion_pointer}/template_base64",
                )
            )
        _optional_rect(value, "template_rect", assertion_pointer, issues)
        _number(value, "threshold", assertion_pointer, issues, minimum=0.000001, maximum=1)
    else:
        issues.append(
            ValidationIssue(
                "invalid_assertion_mode",
                "Assertion mode must be image or text",
                f"{assertion_pointer}/recognition_mode",
            )
        )
    _number(
        value,
        "timeout_seconds",
        assertion_pointer,
        issues,
        minimum=0.1,
        maximum=300,
    )
    _number(
        value,
        "poll_interval_seconds",
        assertion_pointer,
        issues,
        minimum=0.05,
        maximum=10,
    )
    _integer(value, "max_retries", assertion_pointer, issues, minimum=1, maximum=20)


def _validate_failure_retry(
    step: dict[str, Any], pointer: str, issues: list[ValidationIssue]
) -> None:
    value = step.get("failure_retry")
    if value is None:
        return
    retry_pointer = f"{pointer}/failure_retry"
    if not isinstance(value, dict):
        issues.append(
            ValidationIssue(
                "invalid_failure_retry", "Failure retry must be an object", retry_pointer
            )
        )
        return
    if not isinstance(value.get("enabled", False), bool):
        issues.append(
            ValidationIssue(
                "invalid_failure_retry_enabled",
                "Failure retry enabled must be boolean",
                f"{retry_pointer}/enabled",
            )
        )
        return
    if value.get("enabled") is not True:
        return
    if step.get("action") == "start":
        issues.append(
            ValidationIssue(
                "start_step_cannot_retry", "Start step cannot use failure retry", retry_pointer
            )
        )
    process_script_id = value.get("process_script_id")
    try:
        UUID(str(process_script_id))
    except (TypeError, ValueError, AttributeError):
        issues.append(
            ValidationIssue(
                "invalid_retry_process_script",
                "Failure retry must reference a process script ID",
                f"{retry_pointer}/process_script_id",
            )
        )
    _integer(value, "max_retries", retry_pointer, issues, minimum=1, maximum=20)


def _validate_image_branches(value: Any, pointer: str, issues: list[ValidationIssue]) -> None:
    if value is None:
        return
    branches_pointer = f"{pointer}/image_branches"
    if not isinstance(value, list) or len(value) > 20:
        issues.append(
            ValidationIssue(
                "invalid_image_branches",
                "Image branches must be an array of at most 20 items",
                branches_pointer,
            )
        )
        return
    for index, branch in enumerate(value):
        branch_pointer = f"{branches_pointer}/{index}"
        if not isinstance(branch, dict):
            issues.append(
                ValidationIssue(
                    "invalid_image_branch", "Image branch must be an object", branch_pointer
                )
            )
            continue
        # Legacy branches did not always assign presentation-only IDs or names.
        # They are deliberately optional because runtime selection is positional.
        _optional_string_field(branch, "id", branch_pointer, issues, maximum=100)
        _optional_string_field(branch, "name", branch_pointer, issues, maximum=200)
        if "template_base64" not in branch:
            issues.append(
                ValidationIssue(
                    "branch_template_missing",
                    "Image branch must contain a template",
                    f"{branch_pointer}/template_base64",
                )
            )
        _optional_rect(branch, "template_rect", branch_pointer, issues)
        _number(branch, "threshold", branch_pointer, issues, minimum=0.000001, maximum=1)
        click_mode = branch.get("click_mode", "match_center")
        if click_mode not in {"image", "match_center"}:
            issues.append(
                ValidationIssue(
                    "invalid_branch_click_mode",
                    "Image branch click mode is invalid",
                    f"{branch_pointer}/click_mode",
                )
            )
        if click_mode == "image" and "click_template_base64" not in branch:
            issues.append(
                ValidationIssue(
                    "branch_click_template_missing",
                    "Image click branch must contain a click template",
                    f"{branch_pointer}/click_template_base64",
                )
            )
        _optional_rect(branch, "click_template_rect", branch_pointer, issues)
        _optional_number(branch, "click_threshold", branch_pointer, issues, 0.000001, 1)
        _optional_integer(branch, "click_count", branch_pointer, issues, 1, None)
        _optional_integer(branch, "click_interval_ms", branch_pointer, issues, 0, 3_600_000)
        _optional_number(
            branch, "timeout_seconds", branch_pointer, issues, 0.1, MAX_RUNTIME_SECONDS
        )
        _optional_number(
            branch, "click_timeout_seconds", branch_pointer, issues, 0.1, MAX_RUNTIME_SECONDS
        )
        _optional_number(branch, "wait_after_click_seconds", branch_pointer, issues, 0, 300)
        _optional_rect(branch, "search_region", branch_pointer, issues)
        _optional_rect(branch, "click_search_region", branch_pointer, issues)


def _validate_global_popups(value: Any, step_count: int, issues: list[ValidationIssue]) -> None:
    if value is None:
        return
    if not isinstance(value, list) or len(value) > 50:
        issues.append(
            ValidationIssue(
                "invalid_independent_rules",
                "Independent rules must be an array of at most 50 items",
                "/global_popups",
            )
        )
        return
    for index, rule in enumerate(value):
        pointer = f"/global_popups/{index}"
        if not isinstance(rule, dict):
            issues.append(
                ValidationIssue(
                    "invalid_independent_rule", "Independent rule must be an object", pointer
                )
            )
            continue
        # The display name is not part of popup matching or click execution.
        _optional_string_field(rule, "name", pointer, issues, maximum=200)
        _optional_bool(rule, "enabled", pointer, issues)
        _validate_click(rule, pointer, issues)
        if rule.get("click_mode") in {"match_offset", "color_marker"} or rule.get("image_branches"):
            issues.append(
                ValidationIssue(
                    "invalid_independent_rule_click",
                    "Independent rules do not support offset, color or image branches",
                    pointer,
                )
            )
        if "template_base64" not in rule:
            issues.append(
                ValidationIssue(
                    "independent_rule_template_missing",
                    "Independent rule must contain a template",
                    f"{pointer}/template_base64",
                )
            )
        _optional_rect(rule, "template_rect", pointer, issues)
        _optional_rect(rule, "click_template_rect", pointer, issues)
        _optional_point(rule, "click", pointer, issues)
        _optional_number(rule, "threshold", pointer, issues, 0.000001, 1)
        _optional_number(rule, "click_threshold", pointer, issues, 0.000001, 1)
        _optional_number(rule, "cooldown_seconds", pointer, issues, 0, 300)
        _optional_number(rule, "timeout_seconds", pointer, issues, 0.1, MAX_RUNTIME_SECONDS)
        _optional_number(rule, "wait_after_click_seconds", pointer, issues, 0, 300)
        _optional_integer(rule, "click_count", pointer, issues, 1, 10)
        _optional_integer(rule, "click_interval_ms", pointer, issues, 0, 60_000)
        indexes = rule.get("step_indexes")
        start = rule.get("from_step_index", 1)
        end = rule.get("through_step_index", step_count)
        has_range = "from_step_index" in rule or "through_step_index" in rule
        if has_range and (
            indexes is not None
            or not _is_integer(start)
            or not _is_integer(end)
            or not 1 <= int(start) <= int(end) <= step_count
        ):
            issues.append(
                ValidationIssue(
                    "invalid_independent_rule_scope",
                    "Independent rule range is invalid or mixed with a list",
                    pointer,
                )
            )
        if indexes is not None and (
            not isinstance(indexes, list)
            or not indexes
            or any(not _is_integer(item) or not 1 <= int(item) <= step_count for item in indexes)
        ):
            issues.append(
                ValidationIssue(
                    "invalid_independent_rule_scope",
                    "Independent rule step indexes are invalid",
                    f"{pointer}/step_indexes",
                )
            )


def _validate_lifecycle(
    document: dict[str, Any],
    steps: list[Any],
    script_type: ScriptType | None,
    issues: list[ValidationIssue],
) -> None:
    if script_type is ScriptType.MODULE_START:
        if not steps or not isinstance(steps[0], dict) or steps[0].get("action") != "start":
            issues.append(
                ValidationIssue(
                    "start_script_missing_start",
                    "Start module must begin with the start action",
                    "/steps/0",
                )
            )
        if not any(isinstance(step, dict) and step.get("action") == "launch_app" for step in steps):
            issues.append(
                ValidationIssue(
                    "start_script_missing_launch",
                    "Start module must launch the application",
                    "/steps",
                )
            )
    elif script_type is ScriptType.MODULE_PROCESS:
        for index, step in enumerate(steps):
            if isinstance(step, dict) and step.get("action") in {
                "start",
                "launch_app",
                "cleanup",
            }:
                issues.append(
                    ValidationIssue(
                        "process_script_lifecycle_action",
                        "Process module cannot start or clean up the application",
                        f"/steps/{index}/action",
                    )
                )
        if document.get("cleanup_on_finish") is True:
            issues.append(
                ValidationIssue(
                    "process_script_cleanup_flag",
                    "Process module cannot request final cleanup",
                    "/cleanup_on_finish",
                )
            )
    elif script_type is ScriptType.MODULE_END:
        # Cleanup is a creation default, not an immutable import/edit requirement.
        for index, step in enumerate(steps):
            if isinstance(step, dict) and step.get("action") in {
                "start",
                "launch_app",
            }:
                issues.append(
                    ValidationIssue(
                        "end_script_lifecycle_action",
                        "End module cannot contain start or launch actions",
                        f"/steps/{index}/action",
                    )
                )


def _validate_resources(
    value: Any, pointer: str, issues: list[ValidationIssue], allow_inline: bool
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_pointer = f"{pointer}/{_escape_pointer(str(key))}"
            if str(key).endswith(RESOURCE_FIELD_SUFFIX) and child is not None:
                _validate_resource_value(child, child_pointer, issues, allow_inline)
            _validate_resources(child, child_pointer, issues, allow_inline)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_resources(child, f"{pointer}/{index}", issues, allow_inline)


def _validate_resource_value(
    value: Any, pointer: str, issues: list[ValidationIssue], allow_inline: bool
) -> None:
    if isinstance(value, str):
        if allow_inline and value.startswith("data:image/png;base64,"):
            return
        issues.append(
            ValidationIssue(
                "inline_resource_not_allowed",
                "Persisted script resources must use Blob references",
                pointer,
            )
        )
        return
    if not isinstance(value, dict):
        issues.append(
            ValidationIssue("invalid_blob_reference", "Blob reference must be an object", pointer)
        )
        return
    required = {"$blob", "media_type", "sha256", "size_bytes"}
    if set(value) != required:
        issues.append(
            ValidationIssue("invalid_blob_reference", "Blob reference fields are invalid", pointer)
        )
        return
    try:
        UUID(str(value["$blob"]))
    except (TypeError, ValueError, AttributeError):
        issues.append(ValidationIssue("invalid_blob_id", "Blob ID is invalid", f"{pointer}/$blob"))
    digest = value.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        issues.append(
            ValidationIssue("invalid_blob_hash", "Blob SHA-256 is invalid", f"{pointer}/sha256")
        )
    if not _non_empty_string(value.get("media_type"), 255):
        issues.append(
            ValidationIssue(
                "invalid_blob_media_type", "Blob media type is invalid", f"{pointer}/media_type"
            )
        )
    if not _is_integer(value.get("size_bytes")) or int(value["size_bytes"]) < 0:
        issues.append(
            ValidationIssue("invalid_blob_size", "Blob size is invalid", f"{pointer}/size_bytes")
        )


def _collect_blob_resources(
    value: Any, pointer: str, resources: list[DeclaredBlobResource]
) -> None:
    if isinstance(value, dict):
        if "$blob" in value:
            resources.append(
                DeclaredBlobResource(
                    blob_id=UUID(str(value["$blob"])),
                    sha256=str(value["sha256"]),
                    media_type=str(value["media_type"]),
                    size_bytes=int(value["size_bytes"]),
                    json_pointer=pointer,
                    resource_role=_resource_role(pointer),
                    ordinal=len(resources),
                )
            )
            return
        for key, child in value.items():
            _collect_blob_resources(child, f"{pointer}/{_escape_pointer(str(key))}", resources)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _collect_blob_resources(child, f"{pointer}/{index}", resources)


def _find_forbidden_code(value: Any, pointer: str, issues: list[ValidationIssue]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_pointer = f"{pointer}/{_escape_pointer(str(key))}"
            if str(key).casefold() in FORBIDDEN_CODE_KEYS:
                issues.append(
                    ValidationIssue(
                        "arbitrary_code_not_allowed",
                        "Script data cannot contain executable code",
                        child_pointer,
                    )
                )
            _find_forbidden_code(child, child_pointer, issues)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _find_forbidden_code(child, f"{pointer}/{index}", issues)


def _required_string(
    value: dict[str, Any],
    field: str,
    pointer: str,
    issues: list[ValidationIssue],
    *,
    maximum: int,
) -> None:
    if not _non_empty_string(value.get(field), maximum):
        issues.append(
            ValidationIssue(
                "invalid_string_parameter", "String parameter is invalid", f"{pointer}/{field}"
            )
        )


def _optional_string_field(
    value: dict[str, Any],
    field: str,
    pointer: str,
    issues: list[ValidationIssue],
    *,
    maximum: int,
) -> None:
    if field not in value or value[field] is None:
        return
    raw = value[field]
    if not isinstance(raw, str) or len(raw) > maximum:
        issues.append(
            ValidationIssue(
                "invalid_string_parameter", "String parameter is invalid", f"{pointer}/{field}"
            )
        )


def _number(
    value: dict[str, Any],
    field: str,
    pointer: str,
    issues: list[ValidationIssue],
    minimum: float | None = None,
    maximum: float | None = None,
) -> None:
    raw = value.get(field)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        valid = False
    else:
        number = float(raw)
        valid = math.isfinite(number)
        valid = valid and (minimum is None or number >= minimum)
        valid = valid and (maximum is None or number <= maximum)
    if not valid:
        issues.append(
            ValidationIssue(
                "invalid_numeric_parameter", "Numeric parameter is invalid", f"{pointer}/{field}"
            )
        )


def _optional_number(
    value: dict[str, Any],
    field: str,
    pointer: str,
    issues: list[ValidationIssue],
    minimum: float,
    maximum: float,
) -> None:
    if field in value and value[field] is not None:
        _number(value, field, pointer, issues, minimum, maximum)


def _integer(
    value: dict[str, Any],
    field: str,
    pointer: str,
    issues: list[ValidationIssue],
    *,
    minimum: int,
    maximum: int | None,
) -> None:
    raw = value.get(field)
    if not _is_integer(raw) or int(raw) < minimum or (maximum is not None and int(raw) > maximum):
        issues.append(
            ValidationIssue(
                "invalid_integer_parameter", "Integer parameter is invalid", f"{pointer}/{field}"
            )
        )


def _optional_integer(
    value: dict[str, Any],
    field: str,
    pointer: str,
    issues: list[ValidationIssue],
    minimum: int,
    maximum: int | None,
) -> None:
    if field in value and value[field] is not None:
        _integer(value, field, pointer, issues, minimum=minimum, maximum=maximum)


def _optional_bool(
    value: dict[str, Any], field: str, pointer: str, issues: list[ValidationIssue]
) -> None:
    if field in value and not isinstance(value[field], bool):
        issues.append(
            ValidationIssue(
                "invalid_boolean_parameter", "Boolean parameter is invalid", f"{pointer}/{field}"
            )
        )


def _optional_rect(
    value: dict[str, Any], field: str, pointer: str, issues: list[ValidationIssue]
) -> None:
    if field not in value or value[field] is None:
        return
    rect = value[field]
    rect_pointer = f"{pointer}/{field}"
    if not isinstance(rect, dict) or set(rect) != {"height", "width", "x", "y"}:
        issues.append(ValidationIssue("invalid_rectangle", "Rectangle is invalid", rect_pointer))
        return
    _integer(rect, "x", rect_pointer, issues, minimum=0, maximum=100_000)
    _integer(rect, "y", rect_pointer, issues, minimum=0, maximum=100_000)
    _integer(rect, "width", rect_pointer, issues, minimum=1, maximum=100_000)
    _integer(rect, "height", rect_pointer, issues, minimum=1, maximum=100_000)


def _optional_point(
    value: dict[str, Any], field: str, pointer: str, issues: list[ValidationIssue]
) -> None:
    if field not in value or value[field] is None:
        return
    point = value[field]
    point_pointer = f"{pointer}/{field}"
    if not isinstance(point, dict) or set(point) != {"x", "y"}:
        issues.append(ValidationIssue("invalid_point", "Point is invalid", point_pointer))
        return
    _integer(point, "x", point_pointer, issues, minimum=0, maximum=100_000)
    _integer(point, "y", point_pointer, issues, minimum=0, maximum=100_000)


def _non_empty_string(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _is_integer(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> TypeGuard[int | float]:
    return type(value) in (int, float) and math.isfinite(value)


def _resource_role(pointer: str) -> str:
    token = pointer.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
    return (token.removesuffix(RESOURCE_FIELD_SUFFIX) or "resource")[:64]


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")
