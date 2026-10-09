import random
from uuid import UUID

from al1s.adapters.postgres.meme_repository import MemeRepository
from al1s.meme_workshop.assets import MemeAssets
from al1s.meme_workshop.contracts import RenderedImage, RenderInputs, TemplateWrite
from al1s.meme_workshop.pool import RenderPool
from al1s.meme_workshop.records import TemplateRecord
from al1s.meme_workshop.renderer import render


class MemeWorkshopService:
    def __init__(self, repository: MemeRepository, assets: MemeAssets) -> None:
        self.repository, self.assets = repository, assets
        self.pool = RenderPool()

    def preview(self, template: TemplateWrite, inputs: RenderInputs) -> RenderedImage:
        return self.pool.run(lambda: self._render(template, inputs))

    def _render(self, template: TemplateWrite, inputs: RenderInputs) -> RenderedImage:
        refs = {
            "background": template.background_blob_id,
            "foreground": template.foreground_blob_id,
        }
        identities = [identity for identity in refs.values() if identity is not None]
        bodies = self.assets.read_many(identities) if identities else {}
        assets = {role: bodies[identity] for role, identity in refs.items() if identity is not None}
        return render(template.layout, inputs, assets)

    def catalog(self, bot_id: UUID) -> dict[str, object]:
        settings = self.repository.get_settings(bot_id)
        items = (
            self.repository.list_templates(limit=500, enabled_only=True) if settings.enabled else []
        )
        return {
            **settings.model_dump(mode="json"),
            "items": [
                {
                    "id": str(item.id),
                    "name": item.name,
                    "keyword": item.keyword,
                    "aliases": item.aliases,
                    "random_enabled": item.random_enabled,
                    "requirements": dict(
                        zip(("avatars", "images", "texts"), item.layout.requirements(), strict=True)
                    ),
                }
                for item in items
            ],
        }

    def render_command(
        self, bot_id: UUID, receipt: UUID, keyword: str, inputs: RenderInputs
    ) -> RenderedImage:
        def operation() -> RenderedImage:
            if not self.repository.receipt_available(bot_id, receipt):
                raise ValueError("receipt_unavailable")
            template = self._choose(keyword, inputs)
            if template is None:
                raise ValueError("template_not_found_or_inputs_missing")
            if not self.repository.bind_template(bot_id, receipt, template.version_id):
                raise ValueError("receipt_unavailable")
            if not self.repository.lease_version(template.version_id):
                raise ValueError("template_not_found")
            return self._render(template, inputs)

        return self.pool.run(operation)

    def _choose(self, keyword: str, inputs: RenderInputs) -> TemplateRecord | None:
        if keyword != "随机表情":
            return self.repository.find_keyword(keyword)
        candidates = self.repository.random_candidates(
            len(inputs.avatars), len(inputs.images), len(inputs.texts)
        )
        return random.SystemRandom().choice(candidates) if candidates else None
