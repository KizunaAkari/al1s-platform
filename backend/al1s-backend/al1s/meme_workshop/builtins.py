from uuid import UUID

from al1s.meme_workshop.contracts import AvatarSlot, Layout, TemplateWrite

BUILTINS: tuple[tuple[UUID, TemplateWrite], ...] = (
    (
        UUID("bfd27594-fc37-4f97-82f8-000000000001"),
        TemplateWrite(
            name="摸摸",
            keyword="摸摸",
            aliases=["petpet"],
            layout=Layout(engine="petpet", width=128, height=128),
        ),
    ),
    (
        UUID("bfd27594-fc37-4f97-82f8-000000000002"),
        TemplateWrite(
            name="图片配字",
            keyword="配字",
            aliases=["caption"],
            layout=Layout(engine="caption"),
        ),
    ),
    (
        UUID("bfd27594-fc37-4f97-82f8-000000000003"),
        TemplateWrite(
            name="圆形头像",
            keyword="圆形头像",
            aliases=["头像圆圈"],
            layout=Layout(
                width=320,
                height=320,
                background_color="#e7eefb",
                avatars=[AvatarSlot(x=32, y=32, width=256, height=256)],
            ),
        ),
    ),
)

RESERVED_WORDS = {"头像合成", "随机表情", "表情列表"}
