from datetime import timedelta
from hashlib import sha256
from io import BytesIO
from unittest.mock import Mock

import pytest

from al1s.adapters.s3.blob_store import S3BlobStore, _canonical_endpoint_host
from al1s.app.config import Settings
from al1s.execution.errors import ConflictError


@pytest.mark.parametrize("payload", [b"evil", b"abc", b"abcde", b"abcd"])
def test_finalize_validates_body_not_claimed_hash(payload: bytes) -> None:
    expected = sha256(b"abcd").hexdigest()
    body = Mock(wraps=BytesIO(payload))
    client = Mock()
    client.get_object.return_value = {
        "Body": body, "ContentLength": 4, "ContentType": "image/png",
        "Metadata": {"sha256": expected},
    }
    copied = []
    client.put_object.side_effect = lambda **kw: copied.append(kw["Body"].read())
    store = S3BlobStore(Settings(_env_file=None), client=client)
    if payload == b"abcd":
        store.finalize_artifact("incoming", "published", size_bytes=4,
                                sha256=expected, media_type="image/png")
        assert copied == [b"abcd"]
        assert client.put_object.call_args.kwargs["Key"] == "published"
    else:
        with pytest.raises(ConflictError):
            store.finalize_artifact("incoming", "published", size_bytes=4,
                                    sha256=expected, media_type="image/png")
        client.put_object.assert_not_called()
    body.close.assert_called_once()
    assert all(0 < call.args[0] <= 1024 * 1024 for call in body.read.call_args_list)


def test_finalize_reads_one_snapshot_and_closes_failed_stream() -> None:
    client = Mock()
    body = Mock()
    body.read.side_effect = OSError("disconnected")
    client.get_object.return_value = {
        "Body": body, "ContentLength": 4, "ContentType": "image/png",
    }
    store = S3BlobStore(Settings(_env_file=None), client=client)
    with pytest.raises(OSError):
        store.finalize_artifact("incoming", "published", size_bytes=4,
                                sha256=sha256(b"abcd").hexdigest(), media_type="image/png")
    body.close.assert_called_once()
    client.put_object.assert_not_called()


def test_source_overwrite_during_publication_does_not_change_verified_bytes() -> None:
    objects = {"incoming": b"abcd"}
    client = Mock()
    client.get_object.side_effect = lambda **kw: {
        "Body": BytesIO(objects[kw["Key"]]), "ContentLength": 4, "ContentType": "image/png",
    }

    def put(**kwargs):
        objects["incoming"] = b"evil"
        objects[kwargs["Key"]] = kwargs["Body"].read()

    client.put_object.side_effect = put
    store = S3BlobStore(Settings(_env_file=None), client=client)
    store.finalize_artifact("incoming", "published", size_bytes=4,
                            sha256=sha256(b"abcd").hexdigest(), media_type="image/png")
    assert objects == {"incoming": b"evil", "published": b"abcd"}
    assert client.get_object.call_count == 1


@pytest.mark.parametrize("payload", [b"abc", b"abcd", b"abcde" * 100])
def test_range_reads_are_bounded_and_close_stream(payload: bytes) -> None:
    body = Mock(wraps=BytesIO(payload))
    client = Mock()
    client.get_object.return_value = {"Body": body}
    store = S3BlobStore(Settings(_env_file=None), client=client)
    try:
        if len(payload) == 4:
            assert store.get_range("resource", 10, 13) == payload
        else:
            with pytest.raises(RuntimeError, match="byte range length"):
                store.get_range("resource", 10, 13)
        body.read.assert_called_once_with(5)
        body.close.assert_called_once_with()
        client.get_object.assert_called_once_with(
            Bucket=Settings(_env_file=None).s3_bucket,
            Key="resource",
            Range="bytes=10-13",
        )
    finally:
        store.close()


def test_presigned_upload_uses_terminal_reachable_public_endpoint() -> None:
    store = S3BlobStore(
        Settings(
            _env_file=None,
            s3_endpoint_url="http://seaweed-s3:8333",
            s3_public_endpoint_url="https://Yukari.local:8443",
            s3_bucket="al1s-platform",
            s3_access_key="test-access",
            s3_secret_key="test-secret",
        )
    )
    try:
        upload = store.create_presigned_upload(
            "artifacts/terminal/video.mp4",
            media_type="video/mp4",
            sha256="a" * 64,
            expires_in=timedelta(minutes=5),
        )
    finally:
        store.close()

    assert upload.url.startswith(
        "https://yukari.local:8443/al1s-platform/artifacts/terminal/video.mp4?"
    )
    assert upload.headers == {
        "Content-Type": "video/mp4",
        "x-amz-meta-sha256": "a" * 64,
    }


def test_public_endpoint_host_normalization_preserves_path_and_query() -> None:
    assert _canonical_endpoint_host("https://Yukari.local:8443/Case/Path?Case=Yes") == (
        "https://yukari.local:8443/Case/Path?Case=Yes"
    )
