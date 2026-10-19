import hashlib
import json

from al1s.components.bot_sources import OfficialQqSource


def test_official_source_filters_prereleases_and_freezes_manifest_chain():
    manifest = {
        "manifests": [
            {"platform": {"os": "linux", "architecture": "amd64"}, "digest": "sha256:" + "b" * 64}
        ]
    }
    raw = json.dumps(manifest).encode()
    urls = []

    def request(url, headers):
        urls.append(url)
        if "api.github.com" in url:
            return [
                {"tag_name": "v8.3.2", "prerelease": False, "draft": False},
                {"tag_name": "v9.0.0", "prerelease": False, "draft": False},
                {"tag_name": "v8.4.0", "prerelease": True, "draft": False},
            ], None
        if "auth.docker.io" in url:
            return {"token": "test-only-token"}, None
        return manifest, "sha256:" + hashlib.sha256(raw).hexdigest()

    candidates = OfficialQqSource(request).check("amd64", "1.0.0", "sha256:" + "c" * 64)
    assert len(candidates) == 1 and candidates[0].native_version == "8.3.2"
    assert candidates[0].platform_manifest_digest == "sha256:" + "b" * 64
    assert all("/linyuchen/llbot/" in u for u in urls if "registry-1" in u)
