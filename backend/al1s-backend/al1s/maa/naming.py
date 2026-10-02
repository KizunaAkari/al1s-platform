from __future__ import annotations

import unicodedata

from al1s.maa.errors import MaaDomainError


def normalize_asset_name(value: str, *, asset_kind: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not normalized or len(normalized) > 255:
        raise MaaDomainError(
            f"invalid_{asset_kind}_name",
            f"{asset_kind.replace('_', ' ').title()} name must contain 1 to 255 characters",
        )
    return normalized
