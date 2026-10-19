import hashlib
import json
import re
import time
from collections.abc import Callable
from urllib.request import ProxyHandler, Request, build_opener

from al1s.components.bot_contracts import QqCandidateDraft
from al1s.infrastructure.direct_http import NoRedirect

Fetch = Callable[[str, dict[str, str]], tuple[object, str | None]]


def fetch_json(url: str, headers: dict[str, str], timeout: float) -> tuple[object, str | None]:
    request = Request(url, headers={"User-Agent": "AL1S-Bot-Updates", **headers})
    try:
        with build_opener(ProxyHandler(), NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(2 * 1024**2 + 1)
            digest = response.getheader("Docker-Content-Digest")
        if len(raw) > 2 * 1024**2:
            raise ValueError("qq_upstream_response_limit")
        if digest is not None and digest != "sha256:" + hashlib.sha256(raw).hexdigest():
            raise ValueError("qq_registry_digest_mismatch")
        return json.loads(raw), digest
    except (OSError, ValueError):
        raise ValueError("qq_upstream_unavailable") from None


class OfficialQqSource:
    def __init__(self, fetch: Fetch | None = None):
        self.fetch = fetch

    def check(
        self, architecture: str, runtime_version: str, runtime_digest: str
    ) -> list[QqCandidateDraft]:
        if architecture not in {"amd64", "arm64"}:
            raise ValueError("qq_architecture_unsupported")
        deadline = time.monotonic() + 30

        def get(url: str, headers: dict[str, str]) -> tuple[object, str | None]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("qq_source_check_timeout")
            return (
                self.fetch(url, headers)
                if self.fetch
                else fetch_json(url, headers, min(5, remaining))
            )

        releases, _ = get(
            "https://api.github.com/repos/LLOneBot/LuckyLilliaBot/releases?per_page=20", {}
        )
        if not isinstance(releases, list):
            raise ValueError("qq_releases_invalid")
        auth, _ = get(
            "https://auth.docker.io/token?service=registry.docker.io&scope=repository:linyuchen/llbot:pull",
            {},
        )
        if not isinstance(auth, dict) or not isinstance(auth.get("token"), str):
            raise ValueError("qq_registry_auth_unavailable")
        result = []
        for release in releases[:20]:
            if (
                not isinstance(release, dict)
                or release.get("draft") is not False
                or release.get("prerelease") is not False
            ):
                continue
            tag = release.get("tag_name")
            if not isinstance(tag, str) or not re.fullmatch(r"v8\.[0-9]{1,4}\.[0-9]{1,4}", tag):
                continue
            try:
                manifest, digest = get(
                    "https://registry-1.docker.io/v2/linyuchen/llbot/manifests/" + tag[1:],
                    {
                        "Authorization": "Bearer " + auth["token"],
                        "Accept": (
                            "application/vnd.oci.image.index.v1+json, "
                            "application/vnd.docker.distribution.manifest.list.v2+json"
                        ),
                    },
                )
                if not isinstance(manifest, dict) or not digest:
                    continue
                platform = next(
                    (
                        m
                        for m in manifest.get("manifests", [])
                        if m.get("platform", {}).get("os") == "linux"
                        and m.get("platform", {}).get("architecture") == architecture
                    ),
                    None,
                )
                if platform is None:
                    continue
                result.append(
                    QqCandidateDraft(
                        native_version=tag[1:],
                        architecture=architecture,
                        registry_digest=digest,
                        platform_manifest_digest=platform["digest"],
                        runtime_artifact_digest=runtime_digest,
                        runtime_version=runtime_version,
                        source_url="https://github.com/LLOneBot/LuckyLilliaBot/releases/tag/" + tag,
                        source_published_at=release.get("published_at"),
                        source_notes=str(release.get("body") or "")[:4000],
                    )
                )
            except (ValueError, KeyError, TypeError):
                continue
        if not result:
            raise ValueError("qq_no_verified_candidates")
        return result
