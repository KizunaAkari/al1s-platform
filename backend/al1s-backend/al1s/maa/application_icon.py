"""Validate and shrink category images before they enter a Maa application row."""

from io import BytesIO

from PIL import Image, UnidentifiedImageError

from al1s.maa.errors import MaaDomainError

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_ICON_BYTES = 65_536
MAX_SOURCE_PIXELS = 4_000_000
ICON_SIZE = 96


def normalize_icon(payload: bytes) -> bytes:
    if not payload or len(payload) > MAX_UPLOAD_BYTES:
        raise MaaDomainError("application_icon_size", "图标文件不得超过 2 MiB", 413)
    try:
        with Image.open(BytesIO(payload)) as source:
            width, height = source.size
            if width < 1 or height < 1 or width * height > MAX_SOURCE_PIXELS:
                raise ValueError("source image dimensions are out of bounds")
            source.seek(0)
            source.load()
            image = source.convert("RGBA")
        image.thumbnail((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
        canvas.alpha_composite(
            image, ((ICON_SIZE - image.width) // 2, (ICON_SIZE - image.height) // 2),
        )
        output = BytesIO()
        canvas.save(output, format="PNG", optimize=True)
        icon = output.getvalue()
        if len(icon) > MAX_ICON_BYTES:
            raise ValueError("normalized icon is too large")
        return icon
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise MaaDomainError("application_icon_invalid", "无法识别或处理该图标图片", 422) from exc
