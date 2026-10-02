from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryFile
from typing import Any, BinaryIO, cast
from urllib.parse import urlsplit, urlunsplit

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from al1s.app.config import Settings
from al1s.execution.artifact_upload_types import (
    ArtifactObjectHead,
    PresignedArtifactUpload,
)
from al1s.execution.errors import ConflictError
from al1s.kernel.types import BlobObjectHead


class BlobNotFoundError(KeyError):
    pass


class S3BlobStore:
    """Small S3 contract used by the platform instead of exposing boto3 upstream."""

    def __init__(
        self,
        settings: Settings,
        client: BaseClient | None = None,
        presign_client: BaseClient | None = None,
    ) -> None:
        self._bucket = settings.s3_bucket
        self._client = client or _s3_client(settings, settings.s3_endpoint_url)
        self._presign_client = presign_client or (
            client
            if client is not None
            else _s3_client(
                settings,
                settings.s3_public_endpoint_url or settings.s3_endpoint_url,
            )
        )

    def put(self, object_key: str, body: bytes | BinaryIO, media_type: str) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=object_key,
            Body=body,
            ContentType=media_type,
        )

    def head(self, object_key: str) -> BlobObjectHead:
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=object_key)
        except ClientError as exc:
            self._raise_if_missing(exc, object_key)
            raise
        return BlobObjectHead(
            size_bytes=int(response["ContentLength"]),
            media_type=str(response.get("ContentType") or "application/octet-stream"),
            etag=_clean_etag(response.get("ETag")),
        )

    def get_range(self, object_key: str, start: int, end_inclusive: int) -> bytes:
        if start < 0 or end_inclusive < start:
            raise ValueError("invalid byte range")
        try:
            response = self._client.get_object(
                Bucket=self._bucket,
                Key=object_key,
                Range=f"bytes={start}-{end_inclusive}",
            )
        except ClientError as exc:
            self._raise_if_missing(exc, object_key)
            raise
        body = response["Body"]
        try:
            expected_size = end_inclusive - start + 1
            data = bytes(body.read(expected_size + 1))
            if len(data) != expected_size:
                raise RuntimeError("Blob store returned an invalid byte range length")
            return data
        finally:
            body.close()

    def delete(self, object_key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=object_key)

    def create_presigned_upload(
        self,
        object_key: str,
        *,
        media_type: str,
        sha256: str,
        expires_in: timedelta,
    ) -> PresignedArtifactUpload:
        seconds = max(1, min(3600, int(expires_in.total_seconds())))
        url = self._presign_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self._bucket,
                "Key": object_key,
                "ContentType": media_type,
                "Metadata": {"sha256": sha256},
            },
            ExpiresIn=seconds,
            HttpMethod="PUT",
        )
        return PresignedArtifactUpload(
            method="PUT",
            url=str(url),
            headers={
                "Content-Type": media_type,
                "x-amz-meta-sha256": sha256,
            },
            expires_at=datetime.now(UTC) + timedelta(seconds=seconds),
        )

    def head_artifact(self, object_key: str) -> ArtifactObjectHead:
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=object_key)
        except ClientError as exc:
            self._raise_if_missing(exc, object_key)
            raise
        metadata = response.get("Metadata") or {}
        return ArtifactObjectHead(
            size_bytes=int(response["ContentLength"]),
            media_type=str(response.get("ContentType") or "application/octet-stream"),
            sha256=str(metadata.get("sha256")) if metadata.get("sha256") else None,
        )

    def finalize_artifact(
        self, source_key: str, destination_key: str, *,
        size_bytes: int, sha256: str, media_type: str,
    ) -> None:
        if source_key == destination_key:
            raise ValueError("Upload and published object keys must differ")
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=source_key)
        except ClientError as exc:
            self._raise_if_missing(exc, source_key)
            raise
        body = response["Body"]
        try:
            actual_type = str(response.get("ContentType") or "application/octet-stream")
            if (int(response["ContentLength"]) != size_bytes
                    or actual_type.split(";", 1)[0].strip().lower()
                    != media_type.split(";", 1)[0].strip().lower()):
                raise ConflictError("artifact_upload_mismatch", "Uploaded object metadata mismatch")
            with TemporaryFile() as verified:
                digest = hashlib.sha256()
                remaining = size_bytes
                while True:
                    chunk = body.read(min(1024 * 1024, remaining + 1))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise ConflictError(
                            "artifact_upload_mismatch", "Uploaded object is too long"
                        )
                    digest.update(chunk)
                    verified.write(chunk)
                if remaining or digest.hexdigest() != sha256:
                    raise ConflictError(
                        "artifact_upload_mismatch", "Uploaded object digest mismatch"
                    )
                verified.seek(0)
                # Windows typeshed models TemporaryFile as a binary wrapper.
                self.put(destination_key, cast(BinaryIO, verified), media_type)
        finally:
            body.close()

    @staticmethod
    def _raise_if_missing(exc: ClientError, object_key: str) -> None:
        error = exc.response.get("Error", {})
        if str(error.get("Code")) in {"404", "NoSuchKey", "NotFound"}:
            raise BlobNotFoundError(object_key) from exc

    def close(self) -> None:
        self._client.close()
        if self._presign_client is not self._client:
            self._presign_client.close()


def _s3_client(settings: Settings, endpoint_url: str) -> BaseClient:
    endpoint_url = _canonical_endpoint_host(endpoint_url)
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        config=Config(
            connect_timeout=settings.probe_timeout_seconds,
            read_timeout=settings.probe_timeout_seconds,
            retries={"max_attempts": 3, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


def _canonical_endpoint_host(endpoint_url: str) -> str:
    """SigV4 signs Host bytes; clients may lowercase a DNS name before sending it."""
    parsed = urlsplit(endpoint_url)
    if not parsed.hostname:
        return endpoint_url
    if parsed.username or parsed.password:
        raise ValueError("S3 endpoint credentials belong in separate settings")
    host = parsed.hostname
    netloc = f"[{host}]" if ":" in host else host
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _clean_etag(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip('"')
