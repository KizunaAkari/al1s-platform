import unicodedata

from .contracts import PresetKind, PromptIntent, RouteDecision, WorkflowCatalog, WorkflowChoice


def normalize_lora_key(value: str) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("drawing_lora_key_invalid")
    result = unicodedata.normalize("NFKC", value).strip().casefold()
    if not result or any(not char.isprintable() for char in result):
        raise ValueError("drawing_lora_key_invalid")
    return result


def _requested(keys: tuple[str, ...]) -> dict[str, str]:
    result = {}
    for value in keys:
        result.setdefault(normalize_lora_key(value), value)
    return result


def _eligible(item: WorkflowChoice, kind: PresetKind) -> bool:
    return item.enabled and item.validation_state == "ready" and item.kind == kind


def _selected(item: WorkflowChoice, requested: dict[str, str], reason: str) -> RouteDecision:
    keys = {normalize_lora_key(key) for key in item.lora_keys}
    ignored = tuple(original for key, original in requested.items() if key not in keys)
    return RouteDecision(status="selected", workflow=item, reason=reason, ignored_lora_keys=ignored)


def select_workflow(catalog: WorkflowCatalog, selection: int | None, intent: PromptIntent,
                    kind: PresetKind, *, force_default: bool = False) -> RouteDecision:
    requested = _requested(intent.lora_keys)
    if selection is not None and not force_default:
        chosen = next((item for item in catalog.workflows if item.number == selection), None)
        if not chosen or not chosen.enabled or chosen.validation_state != "ready":
            return RouteDecision(status="selection_unavailable")
        if chosen.kind != kind:
            return RouteDecision(status="kind_mismatch")
        return _selected(chosen, requested, "manual")

    if requested and not force_default:
        matches = [item for item in catalog.workflows if _eligible(item, kind)
                   and requested.keys() <= {normalize_lora_key(key) for key in item.lora_keys}]
        if len(matches) > 1:
            return RouteDecision(status="ambiguous", candidate_numbers=tuple(sorted(item.number for item in matches)))
        if matches:
            return _selected(matches[0], requested, "lora")

    default_id = catalog.default_text_id if kind == "txt2img" else catalog.default_edit_id
    default = next((item for item in catalog.workflows if item.id == default_id), None)
    if not default or not _eligible(default, kind):
        return RouteDecision(status="default_unavailable")
    return _selected(default, requested, "lora_unmatched" if requested and not force_default else "default")
