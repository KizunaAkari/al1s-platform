"""Optional JSON selection for ZIP endpoints and JSON Blob imports."""

from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from al1s.maa.errors import MaaDomainError


class ImportSelectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_applications: dict[str, UUID] = Field(default_factory=dict, max_length=1000)
    confirmed_overwrites: dict[UUID, Annotated[int, Field(strict=True, ge=1)]] = Field(
        default_factory=dict,
        max_length=1000,
    )


def selection_header(value: str | None) -> dict[str, Any]:
    if value is None:
        return {}
    if len(value) > 65536:
        raise MaaDomainError("archive_selection_invalid", "Selection is too large", 422)
    try:
        return ImportSelectionBody.model_validate_json(value).model_dump()
    except ValidationError as exc:
        raise MaaDomainError("archive_selection_invalid", "Invalid import selection", 422) from exc
