from io import BytesIO

from PIL import Image

TRANSPARENT_INDEX = 255


def encode_transparent_gif(frames: list[Image.Image], duration_ms: int) -> bytes:
    indexed = []
    for frame in frames:
        rgba = frame.convert("RGBA")
        image = rgba.convert("RGB").quantize(
            colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
        )
        palette = image.getpalette() or []
        image.putpalette(palette[:765] + [0] * max(0, 765 - len(palette)) + [0, 0, 0])
        transparent = rgba.getchannel("A").point(lambda value: 255 if value < 128 else 0)
        image.paste(TRANSPARENT_INDEX, mask=transparent)
        image.info["transparency"] = TRANSPARENT_INDEX
        indexed.append(image)
    output = BytesIO()
    indexed[0].save(
        output,
        "GIF",
        save_all=True,
        append_images=indexed[1:],
        loop=0,
        duration=duration_ms,
        transparency=TRANSPARENT_INDEX,
        background=TRANSPARENT_INDEX,
        disposal=2,
        optimize=False,
    )
    return output.getvalue()
