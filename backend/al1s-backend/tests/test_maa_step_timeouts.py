import pytest

from al1s.maa.validation import validate_script_document


@pytest.mark.parametrize(
    "action,fields",
    [
        ("wait", {"seconds": 1}),
        ("home", {}),
        ("back", {}),
        ("feedback", {"message": "ok"}),
        ("screenshot", {}),
    ],
)
@pytest.mark.parametrize(
    "timeout,valid", [(0, False), (0.1, True), (14400, True), (14401, False), (True, False)]
)
def test_all_steps_have_bounded_timeout(action, fields, timeout, valid):
    result = validate_script_document(
        {
            "version": 2,
            "script_type": "module_process",
            "target": {"application_package": "com.example.game"},
            "cleanup_on_finish": False,
            "steps": [{"action": action, **fields, "timeout_seconds": timeout}],
        }
    )
    assert (not result.issues) is valid
