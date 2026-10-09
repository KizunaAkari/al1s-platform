from uuid import UUID

from al1s.meme_workshop.contracts import ConnectionSettings, StrictModel, TemplateWrite


class TemplateRecord(TemplateWrite):
    id: UUID
    version_id: UUID
    version_no: int
    row_version: int
    builtin: bool = False


class SettingsRecord(ConnectionSettings):
    bot_service_id: UUID
    row_version: int = 0


class AssetRecord(StrictModel):
    blob_id: UUID
    width: int
    height: int
