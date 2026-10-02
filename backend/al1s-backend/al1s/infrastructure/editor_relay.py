"""Allow only configured direct WSS origins for editor relay traffic."""

import os
from urllib.parse import urlsplit

from websockets.asyncio.client import connect


class DirectConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        return exc


def allowed_target(url: str) -> bool:
    target = urlsplit(url)
    allowed = {
        origin.strip().lower().rstrip("/")
        for origin in os.environ.get("AL1S_EDITOR_RELAY_ORIGINS", "").split(",")
        if origin.strip()
    }
    return (
        target.scheme == "wss" and not target.username and not target.password
        and not target.query and not target.fragment
        and f"{target.scheme}://{target.netloc}".lower() in allowed
    )
