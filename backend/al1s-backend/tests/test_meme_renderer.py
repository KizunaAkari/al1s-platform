import hashlib
from importlib.resources import files
from io import BytesIO

import pytest
from PIL import Image
from pydantic import ValidationError

from al1s.meme_workshop.contracts import AvatarSlot, Layout, RenderInputs, TextSlot
from al1s.meme_workshop.images import decode_image
from al1s.meme_workshop.petpet_hand import hand_sprite
from al1s.meme_workshop.renderer import avatar_tile, petpet, render


def png(color: str, size: tuple[int, int] = (40, 40)) -> bytes:
    output = BytesIO()
    Image.new("RGBA", size, color).save(output, "PNG")
    return output.getvalue()


def test_composite_respects_avatar_circle_and_transparent_foreground() -> None:
    layout = Layout(width=80, height=80, avatars=[AvatarSlot(x=20, y=20, width=40, height=40)])
    foreground = Image.new("RGBA", (80, 80))
    foreground.putpixel((40, 40), (0, 255, 0, 255))
    output = BytesIO()
    foreground.save(output, "PNG")
    result = render(
        layout,
        RenderInputs(avatars=[png("red")]),
        {"background": png("blue", (80, 80)), "foreground": output.getvalue()},
    )
    image = Image.open(BytesIO(result.body)).convert("RGBA")
    assert result.media_type == "image/png"
    assert image.getpixel((20, 20)) == (0, 0, 255, 255)
    assert image.getpixel((40, 40)) == (0, 255, 0, 255)
    assert image.getpixel((40, 32)) == (255, 0, 0, 255)


def test_petpet_is_a_bounded_looping_animation() -> None:
    result = render(
        Layout(engine="petpet", width=128, height=128), RenderInputs(avatars=[png("purple")]), {}
    )
    image = Image.open(BytesIO(result.body))
    assert result.media_type == "image/gif"
    assert image.size == (128, 128)
    assert 2 <= image.n_frames <= 20
    assert image.info["loop"] == 0
    assert len(result.body) < 8 * 1024 * 1024


def test_petpet_transparent_corners_do_not_erase_normal_green_avatar_pixels():
    result = render(
        Layout(engine="petpet", width=112, height=112, frame_duration_ms=80),
        RenderInputs(avatars=[png("#00ff00")]),
        {},
    )
    image = Image.open(BytesIO(result.body))
    for index in range(image.n_frames):
        image.seek(index)
        pixels = image.convert("RGBA")
        assert pixels.getpixel((0, 111))[3] == 0
        assert any(
            pixels.getpixel((x, y)) == (0, 255, 0, 255)
            for y in range(pixels.height)
            for x in range(pixels.width)
        )
        assert pixels.getpixel((5, 40))[3] == 255


def test_petpet_uses_the_original_photographic_hand_and_synchronized_squash() -> None:
    frames = petpet(
        Layout(engine="petpet", width=112, height=112), RenderInputs(avatars=[png("#ff00ff")])
    )
    assert len(frames) == 5
    assert frames[0].convert("RGB").getpixel((20, 10)) == (208, 195, 183)
    boxes = []
    for frame in frames:
        pixels = frame.convert("RGB")
        points = [
            (x, y)
            for y in range(frame.height)
            for x in range(frame.width)
            if pixels.getpixel((x, y)) == (255, 0, 255)
        ]
        boxes.append(
            (
                min(x for x, _ in points),
                min(y for _, y in points),
                max(x for x, _ in points),
                max(y for _, y in points),
            )
        )
    assert boxes[2][1] > boxes[0][1]
    assert boxes[2][2] - boxes[2][0] > boxes[0][2] - boxes[0][0]
    assert boxes[2][3] - boxes[2][1] < boxes[0][3] - boxes[0][1]


def test_original_hand_resource_is_packaged_and_shared_pixels_are_not_mutated():
    body = files("al1s.meme_workshop").joinpath("assets/petpet-hand.png").read_bytes()
    assert (
        hashlib.sha256(body).hexdigest()
        == "fedef6b37205236297e8da5ab86b6ee9d67dc92494c831c140a6ad1cc920fdf2"
    )
    image = Image.open(BytesIO(body))
    assert image.size == (560, 112)
    assert image.convert("RGBA").getchannel("A").getextrema() == (0, 255)
    before = hand_sprite().tobytes()
    petpet(Layout(engine="petpet", width=128, height=128), RenderInputs(avatars=[png("red")]))
    petpet(Layout(engine="petpet", width=32, height=32), RenderInputs(avatars=[png("blue")]))
    assert hand_sprite().tobytes() == before


@pytest.mark.parametrize("size", [32, 128, 512])
def test_petpet_keeps_five_looping_frames_and_configured_speed_at_supported_sizes(size):
    result = render(
        Layout(engine="petpet", width=size, height=size, frame_duration_ms=80),
        RenderInputs(avatars=[png("navy")]),
        {},
    )
    image = Image.open(BytesIO(result.body))
    assert image.size == (size, size) and image.n_frames == 5
    assert image.info["loop"] == 0 and image.info["duration"] == 80


def test_chinese_caption_changes_pixels_and_fits_the_text_box() -> None:
    layout = Layout(
        width=180,
        height=90,
        texts=[
            TextSlot(
                x=5,
                y=5,
                width=170,
                height=80,
                font_size=36,
                color="#ffffff",
                stroke_color="#000000",
            )
        ],
    )
    result = render(
        layout,
        RenderInputs(texts=["爱丽丝\n今天也要努力!" * 3]),
        {"background": png("#222222", (180, 90))},
    )
    image = Image.open(BytesIO(result.body)).convert("RGB")
    assert len(set(image.getdata())) > 4
    assert image.getpixel((0, 0)) == (34, 34, 34)


def test_input_and_layout_limits_are_enforced_before_rendering() -> None:
    for body in (b"not-an-image", b"x" * (8 * 1024 * 1024 + 1), png("red", (4097, 1))):
        with pytest.raises(ValueError):
            decode_image(body)
    with pytest.raises(ValidationError):
        Layout(width=2049)
    with pytest.raises(ValidationError):
        Layout(avatars=[AvatarSlot()] * 5)
    with pytest.raises(ValidationError):
        Layout(avatars=[AvatarSlot(x=300, y=300, width=200, height=200)])
    with pytest.raises(ValueError):
        render(Layout(texts=[TextSlot()]), RenderInputs(texts=["x" * 201]), {})


def test_caption_uses_the_uploaded_picture_and_requires_text() -> None:
    result = render(
        Layout(engine="caption"),
        RenderInputs(images=[png("navy", (320, 180))], texts=["测试字幕"]),
        {},
    )
    assert result.width == 320
    assert result.height > 180
    with pytest.raises(ValueError):
        render(Layout(engine="caption"), RenderInputs(images=[png("red")]), {})


def test_text_that_cannot_fit_is_rejected_instead_of_silently_clipped() -> None:
    with pytest.raises(ValueError, match="text_box_too_small"):
        render(Layout(texts=[TextSlot(width=1, height=1)]), RenderInputs(texts=["中文"]), {})


def test_rectangular_contain_and_rotation_keep_transparent_padding():
    source = Image.new("RGBA", (30, 10), "red")
    slot = AvatarSlot(width=60, height=60, shape="rectangle", fit="contain")
    tile = avatar_tile(source, slot)
    assert tile.getpixel((0, 0))[3] == 0
    assert tile.getpixel((5, 30)) == (255, 0, 0, 255)
    turned = avatar_tile(source, slot.model_copy(update={"rotation": 90}))
    assert turned.size == (60, 60)
    assert turned.getpixel((5, 30))[3] == 0
    assert turned.getpixel((30, 5)) == (255, 0, 0, 255)


def test_multiple_avatar_inputs_follow_slot_order_and_layer_priority():
    slots = [AvatarSlot(width=40, height=40, order=index, shape="rectangle") for index in range(2)]
    inputs = RenderInputs(avatars=[png("red"), png("blue")])
    image = Image.open(BytesIO(render(Layout(width=40, height=40, avatars=slots), inputs, {}).body))
    assert image.convert("RGB").getpixel((20, 20)) == (0, 0, 255)
    slots[0] = slots[0].model_copy(update={"order": 2})
    image = Image.open(BytesIO(render(Layout(width=40, height=40, avatars=slots), inputs, {}).body))
    assert image.convert("RGB").getpixel((20, 20)) == (255, 0, 0)
