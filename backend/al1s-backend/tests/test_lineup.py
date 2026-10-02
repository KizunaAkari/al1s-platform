from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from al1s.execution.errors import InvalidRequestError
from al1s.lineup.catalog import MODEL_VERSION, catalog, validate_review
from al1s.lineup.definition import LineupDefinitionProvider
from al1s.lineup.images import normalize_image
from al1s.lineup.results import validated_result


def test_normalize_and_invalid_images():
    output = BytesIO()
    Image.new("RGB", (64, 32), "blue").save(output, "JPEG")
    data, width, height = normalize_image(output.getvalue())
    assert data.startswith(b"\x89PNG") and (width, height) == (64, 32)
    for invalid in (b"", b"not an image", b"x" * (16 * 1024 * 1024 + 1)):
        with pytest.raises(InvalidRequestError):
            normalize_image(invalid)


def test_review_preserves_positions_and_outfit_ids():
    ids = [s["id"] for s in catalog()["students"][:6]]
    validate_review(ids + ids)
    validate_review([None] * 12)
    for invalid in ([1] * 12, ids + [None] * 5, [True] + [None] * 11, ids[:1] * 2 + [None] * 10):
        with pytest.raises(InvalidRequestError):
            validate_review(invalid)


def test_definition_uses_linux_without_phone_and_pins_assets():
    record = dict(blob_id=uuid4(), catalog_version=catalog()["version"])
    provider = LineupDefinitionProvider(SimpleNamespace(get=lambda _: record))
    definition = provider.resolve(str(uuid4()), {})
    assert definition.capability_requirements.requires_target_device is False
    assert definition.blobs[0].blob_id == record["blob_id"]
    assert definition.manifest["catalog_version"] == record["catalog_version"]
    assert definition.manifest["model_version"] == MODEL_VERSION
    assert definition.capability_requirements.min_memory_bytes == 1024 * 1024 * 1024
    provider.authorize_target("", None)
    with pytest.raises(InvalidRequestError):
        provider.authorize_target("", uuid4())
    with pytest.raises(InvalidRequestError):
        provider.resolve(str(uuid4()), {"adb": True})


def test_result_never_trusts_reported_agreement():
    sid = catalog()["students"][0]["id"]
    raw = dict(
        model_version="lineup-portrait-yolov8n-v1",
        catalog_version=catalog()["version"],
        layout_valid=True,
        slots=[
            dict(
                side="attack" if i < 6 else "defense",
                index=i % 6,
                box=[0, 0, 10, 10],
                image_id=sid,
                ocr_id=sid,
                score=0.9,
                margin=0.2,
                ocr_score=0.9,
                ocr_text="test",
                agreed=True,
            )
            for i in range(12)
        ],
    )
    record = dict(catalog_version=catalog()["version"], width=100, height=100)
    assert validated_result(raw, record)["slots"][0]["agreed"]
    raw["model_version"] = "lineup-portrait-yolov8n-ppocrv5-v2"
    assert validated_result(raw, record) is None
    raw["attack_side"] = "right"
    assert validated_result(raw, record)["slots"][0]["agreed"]
    raw["model_version"] = []
    assert validated_result(raw, record) is None
    raw["model_version"] = "lineup-portrait-yolov8n-ppocrv5-v2"
    raw["slots"][0]["ocr_score"] = 0.1
    assert not validated_result(raw, record)["slots"][0]["agreed"]
    raw["slots"][0]["box"] = [99, 99, 5, 5]
    assert validated_result(raw, record) is None
