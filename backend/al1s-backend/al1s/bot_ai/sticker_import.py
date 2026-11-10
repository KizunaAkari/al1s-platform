import stat
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, is_zipfile

from PIL import Image

from al1s.meme_workshop.images import MAX_BYTES, decode_image

MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_FILES = 100
EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp"}


def _image(body: bytes) -> tuple[str, bytes]:
    try:
        decode_image(body)
        with Image.open(BytesIO(body), formats=list(EXTENSIONS)) as image:
            extension = EXTENSIONS.get(image.format)
    except (OSError, SyntaxError, ValueError) as error:
        raise ValueError("ai_sticker_image_invalid") from error
    if not extension:
        raise ValueError("ai_sticker_image_invalid")
    return extension, body


def extract_images(body: bytes) -> list[tuple[str, bytes]]:
    if not body or len(body) > MAX_ARCHIVE:
        raise ValueError("ai_sticker_upload_too_large")
    source = BytesIO(body)
    if not is_zipfile(source):
        return [_image(body)]
    try:
        with ZipFile(source) as bundle:
            entries = bundle.infolist()
            if len(entries) > 1000:
                raise ValueError("ai_sticker_archive_invalid")
            images, total = [], 0
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if (path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                        or stat.S_ISLNK(entry.external_attr >> 16)
                        or (path.parts and ":" in path.parts[0])):
                    raise ValueError("ai_sticker_archive_invalid")
                if (entry.is_dir()
                        or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}):
                    continue
                if (entry.flag_bits & 1 or entry.compress_type not in {ZIP_STORED, ZIP_DEFLATED}
                        or entry.file_size > MAX_BYTES):
                    raise ValueError("ai_sticker_archive_invalid")
                total += entry.file_size
                if total > MAX_EXPANDED or len(images) >= MAX_FILES:
                    raise ValueError("ai_sticker_archive_too_large")
                with bundle.open(entry) as member:
                    content = member.read(MAX_BYTES + 1)
                if len(content) != entry.file_size:
                    raise ValueError("ai_sticker_archive_invalid")
                images.append(_image(content))
            if not images:
                raise ValueError("ai_sticker_archive_empty")
            return images
    except (BadZipFile, OSError, RuntimeError, NotImplementedError) as error:
        raise ValueError("ai_sticker_archive_invalid") from error
