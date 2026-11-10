import base64
from importlib import import_module
from io import BytesIO
from uuid import UUID

import pytest
from PIL import Image


def image_data():
    output = BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def test_tag_workflow_uses_installed_wd14_node_and_inline_image_only():
    tagger = import_module("al1s.bot_drawing.tagger")
    data = image_data()
    graph = tagger.tag_workflow(data)
    assert graph["1"]["class_type"] == "ImageReceiver"
    assert graph["1"]["inputs"]["image"] == "#DATA"
    assert graph["1"]["inputs"]["image_data"] == data
    assert graph["2"]["class_type"] == "WD14Tagger|pysssss"
    assert graph["2"]["inputs"]["image"] == ["1", 0]
    assert graph["2"]["inputs"]["model"] == "wd-vit-tagger-v3"


def test_invalid_reference_never_becomes_a_blank_tagger_image():
    tagger = import_module("al1s.bot_drawing.tagger")
    with pytest.raises(ValueError, match="drawing_image_invalid"):
        tagger.tag_workflow("data:image/png;base64,YQ==")


def test_tag_result_requires_matching_task_owner_and_one_text_result():
    tagger = import_module("al1s.bot_drawing.tagger")
    prompt = UUID("00000000-0000-4000-8000-000000000001")
    result = {"id": str(prompt), "status": "completed",
              "workflow": {"extra_data": {"client_id": "owner"}},
              "outputs": {"2": {"tags": ["1girl, red hair"]}}}
    assert tagger.extract_tags(result, prompt_id=prompt, owner="owner") == "1girl, red hair"
    result["workflow"]["extra_data"]["client_id"] = "other"
    with pytest.raises(ValueError, match="drawing_tagger_owner_mismatch"):
        tagger.extract_tags(result, prompt_id=prompt, owner="owner")


@pytest.mark.parametrize("tags", [[], ["a", "b"], [123], [""], ["x" * 2501]])
def test_tagger_rejects_empty_multiple_or_oversized_results(tags):
    tagger = import_module("al1s.bot_drawing.tagger")
    prompt = UUID(int=2)
    result = {"id": str(prompt), "status": "completed",
              "workflow": {"extra_data": {"client_id": "owner"}}, "outputs": {"2": {"tags": tags}}}
    with pytest.raises(ValueError, match="drawing_tagger_output_invalid"):
        tagger.extract_tags(result, prompt_id=prompt, owner="owner")
