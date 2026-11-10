from uuid import UUID

from .media import validate_image_data

TAGGER_MODEL = "wd-vit-tagger-v3"


def tag_workflow(image_data: str) -> dict:
    validate_image_data(image_data)
    return {
        "1": {"class_type": "ImageReceiver", "inputs": {
            "image": "#DATA", "link_id": 0, "save_to_workflow": True,
            "image_data": image_data, "trigger_always": False}},
        "2": {"class_type": "WD14Tagger|pysssss", "inputs": {
            "image": ["1", 0], "model": TAGGER_MODEL, "threshold": 0.35,
            "character_threshold": 0.85, "replace_underscore": True,
            "trailing_comma": False, "exclude_tags": ""}},
    }


def extract_tags(result: object, *, prompt_id: UUID, owner: str) -> str:
    if not isinstance(result, dict) or result.get("id") != str(prompt_id):
        raise ValueError("drawing_tagger_job_mismatch")
    workflow = result.get("workflow")
    extra = workflow.get("extra_data") if isinstance(workflow, dict) else None
    if not isinstance(extra, dict) or extra.get("client_id") != owner:
        raise ValueError("drawing_tagger_owner_mismatch")
    if result.get("status") != "completed":
        raise ValueError("drawing_tagger_not_completed")
    outputs = result.get("outputs")
    node = outputs.get("2") if isinstance(outputs, dict) else None
    tags = node.get("tags") if isinstance(node, dict) else None
    if (not isinstance(tags, list) or len(tags) != 1 or not isinstance(tags[0], str)
            or not tags[0].strip() or len(tags[0]) > 2500):
        raise ValueError("drawing_tagger_output_invalid")
    return tags[0].strip()
