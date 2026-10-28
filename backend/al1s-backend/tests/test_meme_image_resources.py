from io import BytesIO

import pytest
from PIL import Image

from al1s.meme_workshop.contracts import AvatarSlot, TextSlot
from al1s.meme_workshop.images import decode_image
from al1s.meme_workshop.renderer import avatar_tile
from al1s.meme_workshop.text import draw_text, font


def test_circle_preserves_partial_alpha_and_leaves_source_unchanged() -> None:
    source = Image.new("RGBA", (4, 4), (17, 91, 173, 255))
    source.putalpha(Image.frombytes("L", (4, 4), bytes(range(0, 256, 17))))
    before = source.tobytes()
    tile = avatar_tile(source, AvatarSlot(width=4, height=4))
    assert list(tile.getchannel("A").tobytes()) == [
        0, 17, 34, 0, 68, 85, 102, 119, 136, 153, 170, 187, 0, 221, 238, 0,
    ]
    assert tile.convert("RGB").tobytes() == source.convert("RGB").tobytes()
    assert source.tobytes() == before


@pytest.mark.parametrize(
    ("orientation", "size", "pixels"),
    [
        (1, (3, 2), [1, 2, 3, 4, 5, 6]),
        (2, (3, 2), [3, 2, 1, 6, 5, 4]),
        (3, (3, 2), [6, 5, 4, 3, 2, 1]),
        (4, (3, 2), [4, 5, 6, 1, 2, 3]),
        (5, (2, 3), [1, 4, 2, 5, 3, 6]),
        (6, (2, 3), [4, 1, 5, 2, 6, 3]),
        (7, (2, 3), [6, 3, 5, 2, 4, 1]),
        (8, (2, 3), [3, 6, 2, 5, 1, 4]),
    ],
)
def test_decode_applies_orientation_once(
    orientation: int, size: tuple[int, int], pixels: list[int]
) -> None:
    source = Image.frombytes("L", (3, 2), bytes([1, 2, 3, 4, 5, 6]))
    exif = Image.Exif()
    exif[274] = orientation
    body = BytesIO()
    source.save(body, "PNG", exif=exif)
    result = decode_image(body.getvalue())
    assert result.mode == "RGBA" and result.size == size
    assert list(result.getchannel("R").tobytes()) == pixels
    assert result.getexif().get(274, 1) == 1


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP", "GIF"])
def test_decode_returns_usable_pixels_after_source_closes(format: str) -> None:
    source = Image.new("RGB", (8, 6), "white")
    body = BytesIO()
    source.save(body, format)
    result = decode_image(body.getvalue())
    assert result.size == (8, 6) and result.mode == "RGBA"
    assert result.getpixel((0, 0)) == (255, 255, 255, 255)
    result.putpixel((0, 0), (1, 2, 3, 4))
    assert decode_image(body.getvalue()).getpixel((0, 0)) == (255, 255, 255, 255)


def test_decode_animation_uses_first_frame() -> None:
    body = BytesIO()
    Image.new("RGB", (8, 8), "red").save(
        body, "GIF", save_all=True, append_images=[Image.new("RGB", (8, 8), "blue")],
    )
    result = decode_image(body.getvalue())
    assert result.getpixel((0, 0)) == (255, 0, 0, 255)


def test_font_recreation_preserves_wrapping_stroke_and_alignment() -> None:
    font.cache_clear()
    try:
        slot = TextSlot(width=120, height=64, font_size=28, stroke_width=2, align="right")
        first = Image.new("RGBA", (120, 64))
        draw_text(first, slot, "中文换行\nAL-1S")
        for size in range(80, 256):
            font(size)
        second = Image.new("RGBA", first.size)
        draw_text(second, slot, "中文换行\nAL-1S")
        assert first.getbbox() is not None
        assert first.tobytes() == second.tobytes()
    finally:
        font.cache_clear()
