import hashlib
import json
import shutil
import ssl
from pathlib import Path
from typing import cast
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from al1s.components.contracts import ComponentRelease
from al1s.components.host.contracts import JsonObject
from al1s.execution.host_management import HostManagerConnection
from al1s.infrastructure.direct_http import NoRedirect


class PlatformComponentClient:
    def __init__(self, connection: HostManagerConnection):
        self.connection = connection
        self.opener = build_opener(
            ProxyHandler({}),
            NoRedirect(),
            HTTPSHandler(context=ssl.create_default_context(cafile=connection.ca_file)),
        )

    def request(
        self, path: str, headers: JsonObject | None = None, maximum: int = 128 * 1024
    ) -> bytes:
        request = Request(
            self.connection.url + "/api/v1/components/internal" + path,
            headers={
                "Authorization": "Bearer " + self.connection.token.get_secret_value(),
                **(headers or {}),
            },
        )
        with self.opener.open(request, timeout=30) as response:
            body = response.read(maximum + 1)
        if len(body) > maximum:
            raise ValueError("component_download_response_limit")
        return cast(bytes, body)

    def operation(self, identity: UUID) -> JsonObject:
        return cast(JsonObject, json.loads(self.request(f"/operations/{identity}")))

    def release(self, identity: UUID) -> ComponentRelease:
        return ComponentRelease.model_validate_json(self.request(f"/releases/{identity}"))

    def download(self, release: ComponentRelease, path: Path) -> None:
        if shutil.disk_usage(path.parent).free < release.size_bytes + 64 * 1024**2:
            raise ValueError("component_download_disk_space")
        digest = hashlib.sha256()
        with path.open("xb") as output:
            for start in range(0, release.size_bytes, 4 * 1024**2):
                end = min(release.size_bytes, start + 4 * 1024**2) - 1
                body = self.request(
                    f"/releases/{release.release_id}/content",
                    {"Range": f"bytes={start}-{end}"},
                    maximum=4 * 1024**2,
                )
                if len(body) != end - start + 1:
                    raise ValueError("component_download_incomplete")
                digest.update(body)
                output.write(body)
        if digest.hexdigest() != release.sha256:
            raise ValueError("component_download_hash")
