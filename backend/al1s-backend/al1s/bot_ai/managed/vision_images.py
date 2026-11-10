import base64
import math
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PIL import Image, ImageOps


def _read_image(value: str, roots: tuple[Path, ...]) -> bytes:
    if value.startswith("data:image/") or value.startswith("base64://"):
        if value.startswith("data:") and ";base64," not in value:
            raise ValueError("comfy_image_source_invalid")
        encoded = value.partition(",")[2] if value.startswith("data:") else value[9:]
        if len(encoded) > 12 * 1024 * 1024:
            raise ValueError("comfy_image_size_invalid")
        try:
            body = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise ValueError("comfy_image_source_invalid") from None
    else:
        parsed = urlsplit(value)
        if Path(value).is_absolute():
            path = Path(value).resolve()
        elif parsed.scheme == "file" and not parsed.netloc:
            path = Path(unquote(parsed.path)).resolve()
        elif not parsed.scheme:
            path = Path(value).resolve()
        else:
            raise ValueError("comfy_image_source_invalid")
        if not any(path.is_relative_to(root.resolve()) for root in roots):
            raise ValueError("comfy_image_path_invalid")
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("comfy_image_size_invalid")
        body = path.read_bytes()
    if not body or len(body) > 8 * 1024 * 1024:
        raise ValueError("comfy_image_size_invalid")
    return body


def prepare_images(references: list[str], roots: tuple[Path, ...]) -> tuple[str | None, str]:
    if not references:
        return None, ""
    if len(references) > 4 or any(not isinstance(value, str) for value in references):
        raise ValueError("comfy_image_count_invalid")
    frames, labels, total = [], [], 0
    for number, reference in enumerate(references, 1):
        body = _read_image(reference, roots)
        total += len(body)
        if total > 16 * 1024 * 1024:
            raise ValueError("comfy_image_size_invalid")
        with Image.open(BytesIO(body), formats=["PNG", "JPEG", "WEBP", "GIF"]) as image:
            if max(image.size) > 4096 or image.width * image.height > 8_000_000:
                raise ValueError("comfy_image_pixels_invalid")
            count = image.n_frames if hasattr(image, "n_frames") else 1
            if count > 500:
                raise ValueError("comfy_image_frames_invalid")
            if len(references) == 1 and count == 1:
                mime = {"JPEG": "jpeg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}[image.format]
                return f"data:image/{mime};base64," + base64.b64encode(body).decode(), ""
            sample_count = min(count, max(1, 9 // len(references)))
            for sample in range(sample_count):
                index = round(sample * (count - 1) / (sample_count - 1)) if sample_count > 1 else 0
                image.seek(index)
                frame = ImageOps.exif_transpose(image).convert("RGB")
                frame.thumbnail((512, 512), Image.Resampling.LANCZOS)
                frames.append(frame)
                labels.append(f"图{number}第{index + 1}帧")
    columns = math.ceil(math.sqrt(len(frames)))
    width, height = max(frame.width for frame in frames), max(frame.height for frame in frames)
    sheet = Image.new("RGB", (columns * width, math.ceil(len(frames) / columns) * height), "white")
    for index, frame in enumerate(frames):
        sheet.paste(frame, ((index % columns) * width, (index // columns) * height))
    output = BytesIO()
    sheet.save(output, "PNG")
    hint = f"这张拼图有{len(frames)}格，按从左到右、从上到下依次是：" + "、".join(labels) + "。"
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode(), hint
