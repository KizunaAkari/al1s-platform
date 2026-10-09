from io import BytesIO

from PIL import Image, ImageOps

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 8_000_000


def decode_image(body: bytes) -> Image.Image:
    if not body or len(body) > MAX_BYTES:
        raise ValueError("图片须小于8MiB")
    try:
        with Image.open(BytesIO(body), formats=["PNG", "JPEG", "WEBP", "GIF"]) as image:
            width, height = image.size
            if max(width, height) > 4096 or width * height > MAX_PIXELS:
                raise ValueError("图片尺寸超过4096边长或800万像素")
            image.seek(0)
            image.load()
            return ImageOps.exif_transpose(image).convert("RGBA")
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("无法读取图片") from exc


def normalized_png(body: bytes) -> tuple[bytes, int, int]:
    image = decode_image(body)
    output = BytesIO()
    image.save(output, "PNG")
    encoded = output.getvalue()
    if len(encoded) > MAX_BYTES:
        raise ValueError("转换后的PNG超过8MiB")
    return encoded, image.width, image.height
