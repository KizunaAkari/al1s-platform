from types import SimpleNamespace
from uuid import uuid4

import pytest

from al1s.execution.errors import InvalidRequestError
from al1s.lineup.catalog import MODEL_VERSION, catalog
from al1s.lineup.decision import decision
from al1s.lineup.definition import LineupDefinitionProvider
from al1s.lineup.results import validated_result
from al1s.lineup.service import LineupService


def partial_result():
    ids = [s["id"] for s in catalog()["students"][:6]]
    return dict(
        model_version=MODEL_VERSION,
        catalog_version=catalog()["version"],
        teams=["defense"],
        team_sizes={"defense": 6},
        layout_hint="defense",
        recognition_mode="portrait",
        layout_valid=True,
        slots=[
            dict(
                side="attack" if i < 6 else "defense",
                index=i % 6,
                present=i >= 6,
                box=[i * 10, 0, 8, 8] if i >= 6 else None,
                image_id=ids[i % 6] if i >= 6 else None,
                ocr_id=None,
                ocr_text="",
                score=0.95 if i >= 6 else 0,
                margin=0.2 if i >= 6 else 0,
                ocr_score=0,
                selected_id=None,
                agreed=True,
                accepted=False,
                evidence="dual",
            )
            for i in range(12)
        ],
    )


def test_partial_result_recomputes_single_evidence_and_rejects_fabricated_side():
    raw = partial_result()
    record = dict(catalog_version=catalog()["version"], width=160, height=100)
    checked = validated_result(raw, record)
    assert checked["slots"][6]["accepted"] and not checked["slots"][6]["agreed"]
    assert checked["slots"][6]["evidence"] == "portrait_only"
    assert checked["slots"][0]["evidence"] == "absent"
    raw["slots"][0]["image_id"] = raw["slots"][6]["image_id"]
    assert validated_result(raw, record) is None


@pytest.mark.parametrize(
    "image_id,text_id,score,margin,ocr,expected",
    [
        (1, 1, 0.9, 0.2, 0.95, "dual"),
        (1, None, 0.9, 0.2, 0, "portrait_only"),
        (None, 1, 0, 0, 0.95, "text_only"),
        (1, 2, 0.99, 0.3, 0.99, "conflict"),
        (1, None, 0.7, 0.2, 0, "unresolved"),
        (None, 1, 0, 0, 0.7, "unresolved"),
    ],
)
def test_single_evidence_threshold_and_conflict(image_id, text_id, score, margin, ocr, expected):
    slot = dict(
        present=True, image_id=image_id, ocr_id=text_id, score=score, margin=margin, ocr_score=ocr
    )
    assert decision(slot, True)["evidence"] == expected
    assert not decision(slot, False)["accepted"]


def test_direction_and_mode_are_pinned_in_manifest():
    provider = LineupDefinitionProvider(
        SimpleNamespace(get=lambda _: dict(blob_id=uuid4(), catalog_version=catalog()["version"]))
    )
    definition = provider.resolve(
        str(uuid4()), dict(layout_hint="defense", recognition_mode="text")
    )
    assert definition.manifest["layout_hint"] == "defense"
    assert definition.manifest["recognition_mode"] == "text"
    for options in [dict(layout_hint="guess"), dict(recognition_mode="shell"), dict(unknown=True)]:
        with pytest.raises(InvalidRequestError):
            provider.resolve(str(uuid4()), options)


def test_review_cannot_fill_absent_side_but_keeps_existing_side_order():
    calls = []
    service = LineupService(SimpleNamespace(save_review=lambda *args: calls.append(args)), None)
    raw = partial_result()
    service.detail = lambda _: dict(state="success", result=raw)
    ids = [s["image_id"] for s in raw["slots"]]
    service.review(uuid4(), 1, ids)
    assert calls[0][2] == ids
    ids[0] = ids[6]
    with pytest.raises(InvalidRequestError, match="未提供"):
        service.review(uuid4(), 1, ids)
    assert len(calls) == 1


def test_v4_actual_count_and_absent_slot_review_guards():
    raw = partial_result()
    raw["team_sizes"] = {"defense": 3}
    for slot in raw["slots"][9:]:
        slot.update(present=False, box=None, image_id=None)
    record = dict(catalog_version=catalog()["version"], width=160, height=100)
    checked = validated_result(raw, record)
    assert checked and sum(s["present"] for s in checked["slots"]) == 3
    service = LineupService(SimpleNamespace(save_review=lambda *args: None), None)
    service.detail = lambda _: dict(state="success", result=checked)
    ids = [s["image_id"] for s in checked["slots"]]
    ids[9] = catalog()["students"][8]["id"]
    with pytest.raises(InvalidRequestError):
        service.review(uuid4(), 1, ids)
    raw["team_sizes"]["defense"] = 4
    assert validated_result(raw, record) is None


def test_v4_ambiguity_is_recomputed_even_when_terminal_claims_agreement():
    raw = partial_result()
    raw["slots"][6].update(
        image_id=10099,
        ocr_id=10099,
        ocr_text="星野(武裝)",
        ocr_score=0.99,
        score=0.87,
        ambiguous_ids=[],
    )
    record = dict(catalog_version=catalog()["version"], width=160, height=100)
    checked = validated_result(raw, record)
    assert checked["slots"][6]["ambiguous_ids"] == [10098, 10099]
    assert not checked["slots"][6]["accepted"]
    raw["model_version"] = "lineup-portrait-yolov8n-ppocrv5-v3"
    del raw["team_sizes"]
    assert validated_result(raw, record)["slots"][6]["accepted"]


def test_user_confirmed_hoshino_form_needs_stronger_portrait_evidence():
    slot = dict(
        present=True,
        image_id=10098,
        ocr_id=None,
        score=0.95,
        margin=0.19,
        ocr_score=0.99,
        ambiguous_ids=[10098, 10099],
    )
    assert decision(slot, True)["selected_id"] == 10098
    slot["margin"] = 0.14
    assert decision(slot, True)["selected_id"] is None
    slot.update(image_id=None, ocr_id=None, score=0, margin=0)
    assert not decision(slot, True)["accepted"]
