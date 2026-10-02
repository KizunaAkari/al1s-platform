"""Additional memory bounds for interactive browser imports."""

from io import BytesIO
from zipfile import BadZipFile, ZipFile

from al1s.maa.errors import MaaDomainError

MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024


def validate_upload_size(content: bytes) -> None:
    if not 0 < len(content) <= MAX_UPLOAD_BYTES:
        raise MaaDomainError(
            "archive_upload_size", "Archive must be between 1 byte and 32 MiB", 422
        )
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 4096 or sum(item.file_size for item in entries) > MAX_EXPANDED_BYTES:
                raise MaaDomainError(
                    "archive_upload_expansion",
                    "Archive exceeds 4096 entries or 128 MiB expanded",
                    422,
                )
    except BadZipFile as exc:
        raise MaaDomainError("invalid_zip", "Archive is not a valid ZIP file", 422) from exc
