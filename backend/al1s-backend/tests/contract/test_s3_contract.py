from __future__ import annotations

import os
from uuid import uuid4

import pytest

from al1s.adapters.s3.blob_store import BlobNotFoundError, S3BlobStore
from al1s.app.config import Settings

pytestmark = pytest.mark.contract


def test_s3_blob_store_fixed_contract() -> None:
    if os.getenv("AL1S_TEST_S3") != "1":
        pytest.skip("AL1S_TEST_S3=1 is required for the external S3 contract")

    store = S3BlobStore(Settings())
    object_key = f"contract-tests/{uuid4()}/payload.bin"
    payload = b"al1s-stage2-s3-contract"
    try:
        store.put(object_key, payload, "application/octet-stream")

        head = store.head(object_key)
        assert head.size_bytes == len(payload)
        assert head.media_type == "application/octet-stream"
        assert store.get_range(object_key, 5, 10) == payload[5:11]

        store.delete(object_key)
        with pytest.raises(BlobNotFoundError):
            store.head(object_key)
    finally:
        store.delete(object_key)
        store.close()
