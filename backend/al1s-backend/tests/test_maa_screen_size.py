from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from al1s.execution.definitions import canonical_manifest_hash
from al1s.maa.compiler import MaaRegisteredActionCompiler
from al1s.maa.native_coordinates import validate_coordinates
from al1s.maa.validation import validate_script_document


@pytest.mark.parametrize(
    "count,expected", [(1, "maa-registered-actions-v2"), (25, "maa-registered-actions-v2")]
)
def test_repeated_clicks_require_new_terminal_contract(count, expected):
    manifest = {
        "script_type": "module_process",
        "steps": [{"action": "wait_click", "click_mode": "match_center", "click_count": count}],
    }
    version = SimpleNamespace(
        manifest=manifest,
        manifest_hash=canonical_manifest_hash(manifest),
        script_id=uuid4(),
        script_version_id=uuid4(),
    )
    compiled = MaaRegisteredActionCompiler().compile(version, [])
    assert compiled.definition["compiler_version"] == expected


@pytest.mark.parametrize(
    "value,invalid",
    [
        ({"click": {"x": 63, "y": 95}}, False),
        ({"click": {"x": 64, "y": 20}}, True),
        ({"template_rect": {"x": 0, "y": 0, "width": 64, "height": 96}}, False),
        ({"region": {"x": 60, "y": 0, "width": 8, "height": 8}}, True),
        ({"swipe": {"x1": 1, "y1": 1, "x2": 2, "y2": 96}}, True),
    ],
)
def test_bound_coordinates(value, invalid):
    issues = []
    validate_coordinates({"steps": [value]}, 64, 96, issues)
    assert bool(issues) is invalid


@pytest.mark.parametrize(
    "size,valid",
    [
        ({"width": 1080, "height": 2400}, True),
        ({"width": True, "height": 2400}, False),
        ({"width": 0, "height": 2400}, False),
        ({"width": 8192, "height": 8192}, False),
        ({"width": 1080, "height": 2400, "blob": {}}, False),
        (None, False),
    ],
)
def test_screen_size_is_exact_bounded_native_pair(size, valid):
    manifest = {
        "version": 2,
        "script_type": "module_process",
        "cleanup_on_finish": False,
        "steps": [{"action": "wait", "seconds": 1}],
        "target": {"application_package": "com.example", "screen_size": size},
    }
    before = deepcopy(manifest)
    assert validate_script_document(manifest).valid is valid
    assert manifest == before
