import hashlib
from uuid import UUID, uuid4

from al1s.adapters.postgres.meme_repository import MemeRepository
from al1s.kernel.ports import BlobStore
from al1s.kernel.types import NewBlob
from al1s.meme_workshop.images import MAX_BYTES, normalized_png
from al1s.meme_workshop.records import AssetRecord


class MemeAssets:
    def __init__(self, repository: MemeRepository, store: BlobStore) -> None:
        self.repository, self.store = repository, store

    def upload(self, body: bytes) -> AssetRecord:
        body, width, height = normalized_png(body)
        pending = NewBlob(
            uuid4(),
            hashlib.sha256(body).hexdigest(),
            len(body),
            "image/png",
            f"memes/{uuid4()}.png",
        )
        existing = self.repository.reserve_asset(pending)
        if existing is None:
            self.store.put(pending.object_key, body, "image/png")
            head = self.store.head(pending.object_key)
            stored = self.store.get_range(pending.object_key, 0, len(body) - 1)
            if (
                head.size_bytes != len(body)
                or head.media_type.split(";", 1)[0] != "image/png"
                or hashlib.sha256(stored).hexdigest() != pending.sha256
            ):
                raise ValueError("asset_upload_mismatch")
        return self.repository.finalize_asset(pending, width, height)

    def read(self, identity: UUID) -> bytes:
        return self.read_many([identity])[identity]

    def read_many(self, identities: list[UUID]) -> dict[UUID, bytes]:
        result: dict[UUID, bytes] = {}
        # Three batched ownership/lease/READY statements; this loop performs S3 I/O only.
        blobs = self.repository.asset_blobs(identities)
        for identity, blob in blobs.items():
            if blob.media_type != "image/png" or not 0 < blob.size_bytes <= MAX_BYTES:
                raise ValueError("asset_not_bounded_png")
            body = self.store.get_range(blob.object_key, 0, blob.size_bytes - 1)
            if len(body) != blob.size_bytes or hashlib.sha256(body).hexdigest() != blob.sha256:
                raise ValueError("asset_checksum_mismatch")
            result[identity] = body
        return result
