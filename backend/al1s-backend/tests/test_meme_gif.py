from io import BytesIO

from PIL import Image

from al1s.meme_workshop.gif_encoder import encode_transparent_gif


def test_moving_opaque_pixels_clear_the_old_position_and_preserve_green_and_white():
    first = Image.new("RGBA", (16, 16))
    second = Image.new("RGBA", (16, 16))
    first.putpixel((2, 2), (0, 255, 0, 255))
    second.putpixel((12, 2), (255, 255, 255, 255))
    body = encode_transparent_gif([first, second], 80)
    image = Image.open(BytesIO(body))
    assert image.n_frames == 2 and image.info["loop"] == 0
    assert image.convert("RGBA").getpixel((2, 2)) == (0, 255, 0, 255)
    image.seek(1)
    pixels = image.convert("RGBA")
    assert pixels.getpixel((2, 2))[3] == 0
    assert pixels.getpixel((12, 2)) == (255, 255, 255, 255)
    assert image.info["duration"] == 80
