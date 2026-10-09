from io import BytesIO

from PIL import Image, ImageDraw, ImageOps

from al1s.meme_workshop.contracts import AvatarSlot, Layout, RenderedImage, RenderInputs, TextSlot
from al1s.meme_workshop.images import MAX_BYTES, decode_image
from al1s.meme_workshop.petpet_hand import petpet_frames
from al1s.meme_workshop.text import draw_text


def avatar_tile(image: Image.Image, slot: AvatarSlot) -> Image.Image:
    size = (round(slot.width), round(slot.height))
    if slot.fit == "cover":
        image = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
    else:
        fitted = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
        image = Image.new("RGBA", size)
        image.alpha_composite(
            fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2)
        )
    if slot.shape == "circle":
        mask = Image.new("L", size)
        ImageDraw.Draw(mask).ellipse((0, 0, size[0] - 1, size[1] - 1), fill=255)
        image.putalpha(
            Image.frombytes(
                "L",
                size,
                bytes(
                    min(a, b)
                    for a, b in zip(image.getchannel("A").tobytes(), mask.tobytes(), strict=True)
                ),
            )
        )
    return image.rotate(-slot.rotation, resample=Image.Resampling.BICUBIC)


def compose(layout: Layout, inputs: RenderInputs, assets: dict[str, bytes]) -> Image.Image:
    size = (layout.width, layout.height)
    canvas = (
        decode_image(assets["background"]).resize(size, Image.Resampling.LANCZOS)
        if "background" in assets
        else Image.new("RGBA", size, layout.background_color)
    )
    layers = [(slot.order, "avatar", index) for index, slot in enumerate(layout.avatars)]
    layers += [(slot.order, "text", index) for index, slot in enumerate(layout.texts)]
    for _, kind, index in sorted(layers):
        if kind == "avatar":
            slot = layout.avatars[index]
            tile = avatar_tile(decode_image(inputs.avatars[index]), slot)
            canvas.alpha_composite(tile, (round(slot.x), round(slot.y)))
        else:
            draw_text(canvas, layout.texts[index], inputs.texts[index])
    if "foreground" in assets:
        canvas.alpha_composite(
            decode_image(assets["foreground"]).resize(size, Image.Resampling.LANCZOS)
        )
    return canvas


def caption(layout: Layout, inputs: RenderInputs) -> Image.Image:
    image = decode_image(inputs.images[0])
    image.thumbnail((2048, 1800), Image.Resampling.LANCZOS)
    band = min(248, max(64, image.width // 4))
    canvas = Image.new("RGBA", (image.width, image.height + band), layout.background_color)
    canvas.alpha_composite(image)
    style = layout.texts[0] if layout.texts else TextSlot(color="#222222", stroke_width=0)
    slot = style.model_copy(
        update={"x": 4, "y": image.height + 2, "width": max(1, image.width - 8), "height": band - 4}
    )
    draw_text(canvas, slot, inputs.texts[0])
    return canvas


def petpet(layout: Layout, inputs: RenderInputs) -> list[Image.Image]:
    return petpet_frames(layout, inputs, avatar_tile)


def render(layout: Layout, inputs: RenderInputs, assets: dict[str, bytes]) -> RenderedImage:
    avatars, pictures, texts = layout.requirements()
    if (
        len(inputs.avatars) < avatars
        or len(inputs.images) < pictures
        or len(inputs.texts) < texts
        or any(not text.strip() for text in inputs.texts[:texts])
    ):
        raise ValueError("缺少模板需要的头像、图片或文字")
    if sum(len(text) for text in inputs.texts) > 200:
        raise ValueError("文字最多200字符")
    if len(inputs.avatars) > 4 or len(inputs.images) > 1 or len(inputs.texts) > 4:
        raise ValueError("输入数量超限")
    output = BytesIO()
    if layout.engine == "petpet":
        frames = petpet(layout, inputs)
        frames[0].save(
            output,
            "GIF",
            save_all=True,
            append_images=frames[1:],
            loop=0,
            duration=layout.frame_duration_ms,
            disposal=2,
        )
        image, media_type = frames[0], "image/gif"
    else:
        image = (
            caption(layout, inputs)
            if layout.engine == "caption"
            else compose(layout, inputs, assets)
        )
        image.save(output, "PNG")
        media_type = "image/png"
    if output.tell() > MAX_BYTES:
        raise ValueError("生成结果超过8MiB")
    return RenderedImage(output.getvalue(), media_type, image.width, image.height)
