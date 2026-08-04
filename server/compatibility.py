"""Static, non-invasive script compatibility checks.

These checks intentionally never execute a script.  They only compare the
script's declared target and recorded geometry with the latest ADB device
profile reported by an agent.
"""

import base64
import binascii
import json
import re
import struct
from typing import Any


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number > 0 else None


def _resolution(value: Any) -> tuple[int, int] | None:
    if isinstance(value, dict):
        width = _integer(value.get("width"))
        height = _integer(value.get("height"))
        return (width, height) if width and height else None
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        width = _integer(value[0])
        height = _integer(value[1])
        return (width, height) if width and height else None
    if isinstance(value, str):
        match = re.search(r"(\d+)\s*[x×]\s*(\d+)", value, re.IGNORECASE)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def device_profile(device: dict[str, Any] | None) -> dict[str, Any]:
    device = device if isinstance(device, dict) else {}
    width = _integer(device.get("screen_width"))
    height = _integer(device.get("screen_height"))
    size = _resolution(device.get("screen_size")) or _resolution(device.get("display_size"))
    if not width or not height:
        width, height = size or (None, None)
    return {
        "serial": str(device.get("serial") or "").strip(),
        "model": str(device.get("model") or "").strip(),
        "android_version": str(device.get("android_version") or "").strip(),
        "screen_width": width,
        "screen_height": height,
        "density": _number(device.get("density")),
    }


def _target_resolution(target: dict[str, Any]) -> tuple[int, int] | None:
    for key in ("screen_size", "resolution", "device_resolution"):
        result = _resolution(target.get(key))
        if result:
            return result
    width = _integer(target.get("screen_width")) or _integer(target.get("width"))
    height = _integer(target.get("screen_height")) or _integer(target.get("height"))
    return (width, height) if width and height else None


def _image_size(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded = value.split(",", 1)[1] if "," in value else value
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data.startswith(b"\xff\xd8"):
        position = 2
        while position + 9 < len(data):
            if data[position] != 0xFF:
                position += 1
                continue
            marker = data[position + 1]
            position += 2
            if marker in {0xD8, 0xD9}:
                continue
            if position + 2 > len(data):
                break
            length = struct.unpack(">H", data[position:position + 2])[0]
            if marker in set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0)):
                if position + 7 <= len(data):
                    height, width = struct.unpack(">HH", data[position + 3:position + 7])
                    return width, height
            position += max(2, length)
    return None


def _iter_geometry(value: Any, path: str = ""):
    if isinstance(value, dict):
        if all(key in value for key in ("x", "y", "width", "height")):
            yield "rect", path, value
        elif all(key in value for key in ("x", "y")):
            yield "point", path, value
        if all(key in value for key in ("x1", "y1", "x2", "y2")):
            yield "swipe", path, value
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                child_path = f"{path}.{key}" if path else str(key)
                yield from _iter_geometry(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_geometry(child, f"{path}[{index}]")


def analyze_script_compatibility(content: str, device: dict[str, Any] | None) -> dict[str, Any]:
    profile = device_profile(device)
    warnings: list[dict[str, Any]] = []
    try:
        document = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return {
            "status": "unknown",
            "summary": "脚本内容无法解析",
            "warnings": [{"code": "invalid_json", "message": "脚本不是有效 JSON"}],
            "device": profile,
        }
    if not isinstance(document, dict):
        return {
            "status": "unknown",
            "summary": "脚本结构无法判断",
            "warnings": [{"code": "invalid_document", "message": "脚本根节点不是对象"}],
            "device": profile,
        }

    target = document.get("target") if isinstance(document.get("target"), dict) else {}
    target_model = str(target.get("device_model") or target.get("model") or "").strip()
    target_serial = str(target.get("device_serial") or target.get("serial") or "").strip()
    if target_model and profile["model"] and target_model != profile["model"]:
        warnings.append({"code": "model_mismatch", "message": f"脚本记录设备为 {target_model}，当前设备为 {profile['model']}"})
    if target_serial and profile["serial"] and target_serial not in {"default", profile["serial"]}:
        warnings.append({"code": "serial_mismatch", "message": f"脚本记录序列号为 {target_serial}，当前设备为 {profile['serial']}"})

    width = profile["screen_width"]
    height = profile["screen_height"]
    expected = _target_resolution(target)
    if expected and width and height and expected != (width, height):
        warnings.append({
            "code": "resolution_mismatch",
            "message": f"脚本记录分辨率为 {expected[0]}×{expected[1]}，当前设备为 {width}×{height}",
        })
    if not width or not height:
        warnings.append({"code": "unknown_resolution", "message": "当前设备尚未上报屏幕分辨率"})

    if width and height:
        for kind, path, geometry in _iter_geometry(document):
            if kind == "rect":
                values = [_number(geometry.get(key)) for key in ("x", "y", "width", "height")]
                if not all(value is not None for value in values):
                    continue
                x, y, rect_width, rect_height = values
                if x < 0 or y < 0 or rect_width <= 0 or rect_height <= 0 or x + rect_width > width or y + rect_height > height:
                    warnings.append({"code": "geometry_out_of_bounds", "message": f"{path} 超出当前屏幕 {width}×{height}"})
            elif kind == "point":
                x, y = _number(geometry.get("x")), _number(geometry.get("y"))
                if x is not None and y is not None and (x < 0 or y < 0 or x >= width or y >= height):
                    warnings.append({"code": "point_out_of_bounds", "message": f"{path} 坐标超出当前屏幕 {width}×{height}"})
            elif kind == "swipe":
                points = [_number(geometry.get(key)) for key in ("x1", "y1", "x2", "y2")]
                if all(point is not None for point in points):
                    x1, y1, x2, y2 = points
                    if any(x < 0 or y < 0 or x >= width or y >= height for x, y in ((x1, y1), (x2, y2))):
                        warnings.append({"code": "swipe_out_of_bounds", "message": f"{path} 滑动轨迹超出当前屏幕 {width}×{height}"})

        for path, value in _iter_image_fields(document):
            image_size = _image_size(value)
            if image_size and (image_size[0] > width or image_size[1] > height):
                warnings.append({"code": "template_too_large", "message": f"{path} 模板尺寸 {image_size[0]}×{image_size[1]} 大于当前屏幕"})

    status = "warning" if warnings else "compatible"
    return {
        "status": status,
        "summary": "发现静态兼容性提示" if warnings else "未发现明显静态问题",
        "warnings": warnings,
        "device": profile,
    }


def _iter_image_fields(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key.endswith("template_base64") or key == "preview_base64":
                yield child_path, child
            elif isinstance(child, (dict, list)):
                yield from _iter_image_fields(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_image_fields(child, f"{path}[{index}]")


