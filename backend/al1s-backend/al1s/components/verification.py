"""Inspect only bounded tar metadata, then require an isolated native version probe."""

import hashlib
import json
import re
import tarfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from al1s.components.contracts import ComponentReleaseInput

MAX_METADATA_BYTES = 1024**2
MAX_MEMBERS = 60000
NativeProbe = Callable[[Path, ComponentReleaseInput], str]


def metadata(archive: tarfile.TarFile, name: str) -> bytes:
    member = archive.getmember(name)
    if not member.isfile() or member.size > MAX_METADATA_BYTES:
        raise ValueError("invalid_image_metadata")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError("missing_image_metadata")
    with stream:
        return stream.read(MAX_METADATA_BYTES + 1)


def valid_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and len(name) <= 1024
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in name
    )


def oci_identity(archive: tarfile.TarFile, expected: str, configuration: str) -> bool:
    """Containerd identifies an image by its OCI manifest/index, not config digest."""
    try:
        roots = json.loads(metadata(archive, "index.json"))["manifests"]
    except (KeyError, TypeError):
        return False
    images = [
        row for row in roots if not row.get("annotations", {}).get("io.containerd.manifest.subject")
    ]
    if len(images) != 1 or images[0].get("digest") != expected:
        return False
    pending, matched = [expected], False
    visited: set[str] = set()
    while pending:
        digest = pending.pop()
        if digest in visited:
            continue
        if len(visited) >= 32 or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("invalid_oci_manifest")
        visited.add(digest)
        body = metadata(archive, "blobs/sha256/" + digest[7:])
        if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
            raise ValueError("oci_manifest_digest_mismatch")
        document = json.loads(body)
        if document.get("config", {}).get("digest") == configuration:
            matched = True
        pending.extend(item["digest"] for item in document.get("manifests", []))
    # Docker Desktop may append untagged provenance referrers to the save index.
    # They must refer to this image graph and cannot introduce another image tag.
    for row in roots:
        subject = row.get("annotations", {}).get("io.containerd.manifest.subject")
        if subject and (
            subject not in visited or "io.containerd.image.name" in row.get("annotations", {})
        ):
            return False
    return matched


class DockerArchiveVerifier:
    def __init__(self, native_probe: NativeProbe):
        self.native_probe = native_probe

    def verify(self, path: Path, release: ComponentReleaseInput) -> None:
        if path.stat().st_size != release.size_bytes:
            raise ValueError("archive_size_mismatch")
        with tarfile.open(path, "r:") as archive:
            for index, member in enumerate(archive):
                if (
                    index >= MAX_MEMBERS
                    or not valid_path(member.name)
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError("unsafe_archive_member")
            manifests = json.loads(metadata(archive, "manifest.json"))
            if not isinstance(manifests, list) or len(manifests) != 1:
                raise ValueError("one_image_required")
            manifest = manifests[0]
            if not isinstance(manifest, dict) or manifest.get("RepoTags") != [
                release.candidate_image
            ]:
                raise ValueError("archive_image_tag_mismatch")
            config_name = manifest.get("Config")
            if not isinstance(config_name, str) or not valid_path(config_name):
                raise ValueError("invalid_image_config")
            config_bytes = metadata(archive, config_name)
            identity = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
            if identity != release.expected_image_id and not oci_identity(
                archive, release.expected_image_id, identity
            ):
                raise ValueError("archive_image_id_mismatch")
            config = json.loads(config_bytes)
            if config.get("architecture") != release.architecture or config.get("os") != "linux":
                raise ValueError("archive_architecture_mismatch")
        # Metadata cannot prove the installed native SDK/service version.
        if self.native_probe(path, release) != release.native_version:
            raise ValueError("native_version_mismatch")
