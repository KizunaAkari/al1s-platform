import hashlib
import io
import json
import tarfile

import pytest

from al1s.components.releases import ComponentReleaseService
from al1s.components.verification import DockerArchiveVerifier
from tests.test_component_inventory import release


def archive(path, *, architecture="amd64", unsafe=False, tag="postgres:17.12-alpine"):
    config = json.dumps(
        {"architecture": architecture, "os": "linux", "rootfs": {"diff_ids": []}}
    ).encode()
    digest = hashlib.sha256(config).hexdigest()
    manifest = [{"Config": digest + ".json", "RepoTags": [tag], "Layers": []}]
    with tarfile.open(path, "w") as output:
        for name, body in [
            ("manifest.json", json.dumps(manifest).encode()),
            (digest + ".json", config),
        ]:
            item = tarfile.TarInfo(name)
            item.size = len(body)
            output.addfile(item, io.BytesIO(body))
        if unsafe:
            item = tarfile.TarInfo("../escape")
            output.addfile(item, io.BytesIO())
    return "sha256:" + digest


def test_archive_is_bound_to_image_architecture_and_actual_native_probe(tmp_path):
    path = tmp_path / "image.tar"
    image = archive(path)
    body = release(expected_image_id=image, size_bytes=path.stat().st_size)
    calls = []

    def probe(path, release):
        calls.append(release.release_id)
        return "17.12"

    verifier = DockerArchiveVerifier(probe)
    verifier.verify(path, body)
    assert calls == [body.release_id]
    with pytest.raises(ValueError):
        verifier.verify(path, body.model_copy(update={"expected_image_id": "sha256:" + "0" * 64}))
    with pytest.raises(ValueError):
        verifier.verify(path, body.model_copy(update={"architecture": "arm64"}))
    with pytest.raises(ValueError):
        DockerArchiveVerifier(lambda *_: "18.0").verify(path, body)
    archive(path, unsafe=True)
    with pytest.raises(ValueError):
        verifier.verify(path, body)


def test_oci_manifest_identity_is_bound_to_the_selected_configuration(tmp_path):
    path = tmp_path / "oci.tar"
    config = json.dumps({"architecture": "amd64", "os": "linux"}).encode()
    config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
    image_manifest = json.dumps({"schemaVersion": 2, "config": {"digest": config_digest}, "layers": []}).encode()
    image_digest = "sha256:" + hashlib.sha256(image_manifest).hexdigest()
    index = json.dumps({"manifests": [{"digest": image_digest}]}).encode()
    entries = [("manifest.json", json.dumps([{"Config": "blobs/sha256/" + config_digest[7:], "RepoTags": ["postgres:17.12-alpine"], "Layers": []}]).encode()), ("index.json", index), ("blobs/sha256/" + image_digest[7:], image_manifest), ("blobs/sha256/" + config_digest[7:], config)]
    with tarfile.open(path, "w") as output:
        for name, content in entries:
            item = tarfile.TarInfo(name); item.size = len(content); output.addfile(item, io.BytesIO(content))
    body = release(expected_image_id=image_digest, size_bytes=path.stat().st_size)
    DockerArchiveVerifier(lambda *_: "17.12").verify(path, body)
    with pytest.raises(ValueError):
        DockerArchiveVerifier(lambda *_: "17.12").verify(path, body.model_copy(update={"expected_image_id": "sha256:" + "f" * 64}))


def test_publish_requires_source_state_and_hash_verification(repository, tmp_path):

    path = tmp_path / "image.tar"
    image = archive(path)
    body = release(
        expected_image_id=image,
        size_bytes=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    class Objects:
        def finalize_artifact(self, source, destination, **kwargs):
            assert kwargs["sha256"] == body.sha256

        def get_range(self, key, start, end):
            return path.read_bytes()[start : end + 1]

    service = ComponentReleaseService(
        repository,
        Objects(),
        DockerArchiveVerifier(lambda *_: "17.12"),
        temporary_directory=tmp_path,
    )
    draft = service.create(body)
    assert draft.state == "draft"
    service.publish(draft.release_id, draft.row_version)
    assert service.verify_once()
    published = repository.get_release(draft.release_id)
    assert published.state == "published" and published.blob_id
    assert (
        repository.enqueue_release(published.release_id, published.row_version).state == "published"
    )


from tests.test_component_postgres import repository as repository  # noqa: E402


def test_linux_publish_preserves_original_download_and_upgrade_contract(repository, tmp_path):
    from al1s.adapters.postgres.release_repository import ReleaseRepository

    tag = "al1s-terminal-next:component-test"
    path = tmp_path / "linux.tar"
    image = archive(path, architecture="arm64", tag=tag)
    body = release(
        component="linux-terminal",
        architecture="arm64",
        candidate_image=tag,
        native_version="0.1.0",
        version="terminal-test",
        compatible_from="0.1",
        expected_image_id=image,
        size_bytes=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    class Objects:
        def finalize_artifact(self, *args, **kwargs):
            pass

        def get_range(self, key, start, end):
            return path.read_bytes()[start : end + 1]

    service = ComponentReleaseService(
        repository,
        Objects(),
        DockerArchiveVerifier(lambda *_: "0.1.0"),
        temporary_directory=tmp_path,
    )
    draft = service.create(body)
    service.publish(draft.release_id, draft.row_version)
    assert service.verify_once()
    legacy = ReleaseRepository(repository.sessions).object_key(draft.release_id)[0]
    assert legacy.state == "published" and legacy.sha256 == body.sha256
