from io import BytesIO

import pytest
from PIL import Image

from al1s.maa.application_icon import MAX_ICON_BYTES, normalize_icon
from al1s.maa.errors import MaaDomainError


def _image(size: tuple[int, int], image_format: str = "PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (34, 123, 190)).save(output, format=image_format)
    return output.getvalue()


def test_normalizes_uploaded_and_device_images_to_bounded_png() -> None:
    icon = normalize_icon(_image((1200, 800), "JPEG"))
    assert len(icon) <= MAX_ICON_BYTES
    with Image.open(BytesIO(icon)) as image:
        assert image.size == (96, 96)
        assert image.mode == "RGBA"
        assert image.format == "PNG"


@pytest.mark.parametrize(
    "body", [b"", b"not an image", _image((2200, 2200))],
    ids=["empty", "invalid", "too-many-pixels"],
)
def test_rejects_invalid_or_oversized_source(body: bytes) -> None:
    with pytest.raises(MaaDomainError):
        normalize_icon(body)
