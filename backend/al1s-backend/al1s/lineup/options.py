from typing import Any

from al1s.execution.errors import InvalidRequestError


def recognition_options(parameters: dict[str, Any]) -> dict[str, str]:
    layout = parameters.get("layout_hint", "auto")
    mode = parameters.get("recognition_mode", "auto")
    if (
        set(parameters) - {"layout_hint", "recognition_mode"}
        or layout not in ("auto", "attack", "defense", "left_attack", "right_attack")
        or mode not in ("auto", "portrait", "text")
    ):
        raise InvalidRequestError("lineup_parameters_invalid", "阵容方向或识别方式无效")
    return dict(layout_hint=layout, recognition_mode=mode)
