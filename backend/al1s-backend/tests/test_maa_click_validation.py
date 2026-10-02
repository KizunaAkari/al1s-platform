import pytest

from al1s.maa.validation import validate_script_document


def validate(**parameters):
    return validate_script_document(
        {
            "version": 2,
            "script_type": "module_process",
            "target": {"application_package": "com.example"},
            "steps": [
                {
                    "action": "wait_click",
                    "template_base64": "data:image/png;base64,AQ==",
                    **parameters,
                }
            ],
        },
        allow_inline_resources=True,
    )


def test_implicit_fixed_click_requires_a_point():
    assert "click_point_missing" in {i.code for i in validate().issues}
    assert validate(click={"x": 10, "y": 20}).valid


def test_image_click_cannot_pass_without_its_template():
    assert "click_template_missing" in {i.code for i in validate(click_mode="image").issues}


def test_repeat_count_has_no_fixed_cap():
    assert validate(click_mode="match_center", click_count=1_000_001).valid


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_branch_repeat_is_validated_before_dispatch(count):
    assert not validate(
        click_mode="match_center",
        image_branches=[
            {
                "template_base64": "data:image/png;base64,AQ==",
                "threshold": 0.85,
                "click_count": count,
            }
        ],
    ).valid


def test_special_click_mode_cannot_silently_accept_image_branches():
    result = validate(
        click_mode="match_offset",
        template_rect={"x": 0, "y": 0, "width": 10, "height": 10},
        image_branches=[{"template_base64": "data:image/png;base64,AQ==", "threshold": 0.85}],
    )
    assert "incompatible_image_branches" in {i.code for i in result.issues}
