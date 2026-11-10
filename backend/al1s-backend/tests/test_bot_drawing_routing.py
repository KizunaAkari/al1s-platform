from importlib import import_module
from uuid import UUID

from al1s.bot_drawing import contracts


def choice(number, kind="txt2img", keys=(), *, enabled=True, state="ready"):
    return contracts.WorkflowChoice(id=UUID(int=number), number=number, kind=kind, revision=1,
                                    enabled=enabled, validation_state=state, lora_keys=keys)


def catalogue(*workflows, text=1, edit=3):
    return contracts.WorkflowCatalog(workflows=workflows,
        default_text_id=UUID(int=text) if text else None,
        default_edit_id=UUID(int=edit) if edit else None)


def route(catalog, selection=None, keys=(), kind="txt2img", *, force_default=False):
    router = import_module("al1s.bot_drawing.routing")
    intent = contracts.PromptIntent(mode="direct", lora_keys=keys)
    return router.select_workflow(catalog, selection, intent, kind, force_default=force_default)


def test_default_is_selected_when_no_lora_is_requested():
    result = route(catalogue(choice(1), choice(2, keys=("初音",))))
    assert result.status == "selected" and result.workflow.number == 1
    assert result.ignored_lora_keys == ()


def test_lora_matching_is_normalized_exact_and_requires_all_requested_keys():
    cat = catalogue(choice(1), choice(2, keys=("MIKU", "snow")), choice(4, keys=("miku-extra",)))
    result = route(cat, keys=("ｍｉｋｕ", "SNOW"))
    assert result.status == "selected" and result.workflow.number == 2
    result = route(cat, keys=("mik",))
    assert result.workflow.number == 1 and result.ignored_lora_keys == ("mik",)


def test_multiple_matching_workflows_require_explicit_choice():
    result = route(catalogue(choice(1), choice(2, keys=("初音",)), choice(4, keys=("初音",))),
                   keys=("初音",))
    assert result.status == "ambiguous" and result.workflow is None
    assert result.candidate_numbers == (2, 4)


def test_personal_selection_has_priority_without_inserting_other_loras():
    cat = catalogue(choice(1), choice(2, keys=("初音",)), choice(4, keys=("派蒙",)))
    result = route(cat, selection=2, keys=("派蒙",))
    assert result.status == "selected" and result.workflow.number == 2
    assert result.ignored_lora_keys == ("派蒙",)


def test_p2p_uses_default_image_workflow_even_with_personal_selection():
    cat = catalogue(choice(1), choice(2, keys=("初音",)), choice(3, kind="image_edit"))
    result = route(cat, selection=2, keys=("初音",), kind="image_edit", force_default=True)
    assert result.status == "selected" and result.workflow.number == 3
    assert result.ignored_lora_keys == ("初音",)


def test_invalid_manual_or_default_selection_does_not_silently_switch():
    cat = catalogue(choice(1), choice(2, enabled=False), choice(3, kind="image_edit", state="missing"))
    assert route(cat, selection=2).status == "selection_unavailable"
    assert route(cat, selection=1, kind="image_edit").status == "kind_mismatch"
    assert route(cat, kind="image_edit").status == "default_unavailable"
