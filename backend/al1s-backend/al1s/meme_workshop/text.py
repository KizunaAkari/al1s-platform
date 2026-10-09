from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from al1s.meme_workshop.contracts import TextSlot

FONT = Path(__file__).with_name("assets") / "NotoSansCJKsc-Regular.otf"


@lru_cache(maxsize=128)
def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT), size)


def wrap(text: str, face: ImageFont.FreeTypeFont, width: int) -> str:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for character in paragraph:
            if line and face.getlength(line + character) > width:
                lines.append(line)
                line = ""
            line += character
        lines.append(line)
    return "\n".join(lines)


def draw_text(canvas: Image.Image, slot: TextSlot, text: str) -> None:
    tile = Image.new("RGBA", (round(slot.width), round(slot.height)))
    draw = ImageDraw.Draw(tile)
    size = slot.font_size
    available = max(1, tile.width - 2 * slot.stroke_width)
    while True:
        face = font(size)
        wrapped = wrap(text, face, available)
        bounds = draw.multiline_textbbox(
            (0, 0), wrapped, font=face, spacing=2, stroke_width=slot.stroke_width, align=slot.align
        )
        if size <= 8 or (
            bounds[2] - bounds[0] <= tile.width and bounds[3] - bounds[1] <= tile.height
        ):
            break
        size -= 1
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    if width > tile.width or height > tile.height:
        raise ValueError("text_box_too_small")
    left = 0 if slot.align == "left" else (tile.width - width)
    if slot.align == "center":
        left /= 2
    draw.multiline_text(
        (left - bounds[0], (tile.height - height) / 2 - bounds[1]),
        wrapped,
        font=face,
        fill=slot.color,
        spacing=2,
        align=slot.align,
        stroke_width=slot.stroke_width,
        stroke_fill=slot.stroke_color,
    )
    canvas.alpha_composite(tile, (round(slot.x), round(slot.y)))
