from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from PIL import Image

from al1s.meme_workshop.contracts import AvatarSlot, Layout, RenderInputs
from al1s.meme_workshop.images import decode_image

SPRITE = Path(__file__).with_name("assets") / "petpet-hand.png"
POSE_SIZE = 112
# Geometry follows benisland's five-pose generator; dimensions use its default scale/squish.
OFFSETS = ((0, 0, 0, 0), (-4, 12, 4, -12), (-12, 18, 12, -18), (-8, 12, 4, -12), (-4, 0, 0, 0))


@lru_cache(maxsize=1)
def hand_sprite() -> Image.Image:
    with Image.open(SPRITE) as image:
        if image.size != (POSE_SIZE * 5, POSE_SIZE):
            raise ValueError("invalid_petpet_hand_asset")
        return image.convert("RGBA")


def petpet_frames(
    layout: Layout,
    inputs: RenderInputs,
    render_avatar: Callable[[Image.Image, AvatarSlot], Image.Image],
) -> list[Image.Image]:
    size = min(512, layout.width, layout.height)
    scale = size / POSE_SIZE
    avatar = decode_image(inputs.avatars[0])
    sheet = hand_sprite()  # Only crop/resize copies below; shared cached pixels remain unchanged.
    frames = []
    for index, (x, y, width, height) in enumerate(OFFSETS):
        left, top = int(14 + x * 0.5), int(20 + y * 1.125)
        w, h = int((112 + width * 1.25) * 0.875), int((112 + height * 1.25) * 0.875)
        slot = AvatarSlot(width=max(1, round(w * scale)), height=max(1, round(h * scale)))
        frame = Image.new("RGBA", (size, size))
        frame.alpha_composite(
            render_avatar(avatar, slot), (round(left * scale), round(top * scale))
        )
        hand = sheet.crop((index * POSE_SIZE, 0, (index + 1) * POSE_SIZE, POSE_SIZE))
        hand_y = max(0, int(top * 0.75 - 20 - 0.5))
        frame.alpha_composite(
            hand.resize((size, size), Image.Resampling.LANCZOS), (0, round(hand_y * scale))
        )
        frames.append(frame)
    return frames
