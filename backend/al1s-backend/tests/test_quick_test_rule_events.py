from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from al1s.maa.quick_test_rule_events import project_rule_event
from al1s.maa.quick_test_service import MaaQuickTestService
from al1s.maa.types import QuickTestEventRecord


def event(code, kind="log", step=None):
    return QuickTestEventRecord(uuid4(), 1, kind, step, code, datetime.now(UTC))


def definition():
    return {
        "manifest": {
            "definitions": {
                "entry": {"independent_rules": [{"name": "通知"}]},
                "recovery:abc": {"independent_rules": [{"name": "恢复弹窗"}]},
            }
        }
    }


@pytest.mark.parametrize("phase", ["started", "succeeded", "failed"])
def test_rule_name_uses_exact_immutable_module_and_does_not_modify_the_event(phase):
    original = event(f"maa_rule_{phase}:recovery:abc:0")
    projected = project_rule_event(original, definition())
    assert projected.rule_name == "恢复弹窗"
    assert original.rule_name is None
    assert projected.step_number is None and projected.code == original.code


@pytest.mark.parametrize(
    "code",
    [
        None,
        "execution_timeout",
        "maa_rule_started:missing:0",
        "maa_rule_started:entry:50",
        "maa_rule_started:entry:-1",
        "maa_rule_unknown:entry:0",
        "maa_rule_started:entry:1",
    ],
)
def test_old_unknown_and_invalid_events_are_not_given_an_invented_rule_name(code):
    original = event(code)
    assert project_rule_event(original, definition()) is original


def test_a_main_step_is_never_reclassified_based_only_on_code():
    original = event("maa_rule_started:entry:0", kind="step_started", step=3)
    assert project_rule_event(original, definition()) is original


def test_page_projection_uses_two_fixed_reads_without_loading_current_scripts():
    session_id, script_id = uuid4(), uuid4()
    uow = SimpleNamespace(quick_tests=Mock(), scripts=Mock())
    uow.quick_tests.get.return_value = SimpleNamespace(script_id=script_id, definition=definition())
    uow.quick_tests.list_events.return_value = [
        event("maa_rule_started:entry:0") for _ in range(100)
    ]
    service = MaaQuickTestService(lambda: nullcontext(uow), Mock(), Mock())
    results = service.list_events(script_id=script_id, session_id=session_id, after=0, limit=100)
    assert len(results) == 100 and all(item.rule_name == "通知" for item in results)
    uow.quick_tests.get.assert_called_once_with(session_id)
    uow.quick_tests.list_events.assert_called_once_with(session_id, after=0, limit=100)
    assert len(uow.quick_tests.mock_calls) == 2 and not uow.scripts.mock_calls
