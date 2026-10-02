"""Fixed upstream data reader. No executable upstream code and no message I/O."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from urllib.request import ProxyHandler, Request, build_opener

from al1s.infrastructure.direct_http import NoRedirect

REPOSITORY = "konsheng/Sensitive-lexicon"
SOURCE_PATH = "ThirdPartyCompatibleFormats/TrChat/SensitiveLexicon.json"
NORMALIZATION_VERSION = "nfkc-opencc1.4.2-t2s-casefold-nfkc-v1"
MAX_BYTES = 2 * 1024 * 1024


class TextNormalizer:
    def __init__(self) -> None:
        from opencc import OpenCC

        self._converter = OpenCC("t2s")

    def normalize(self, value: str) -> str:
        value = self._converter.convert(unicodedata.normalize("NFKC", value))
        return unicodedata.normalize("NFKC", value.casefold())


@dataclass(frozen=True)
class LexiconData:
    commit: str
    sha256: str
    license_text: str
    raw: str
    words: tuple[str, ...]
    normalization_version: str = NORMALIZATION_VERSION
    created_at: datetime | None = None

    def match(self, text: str, normalizer: TextNormalizer) -> tuple[str, ...]:
        normalized = normalizer.normalize(text)
        return tuple(word for word in self.words if word in normalized)


def parse_lexicon(
    raw: bytes, commit: str, license_text: str, normalizer: TextNormalizer
) -> LexiconData:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("invalid_lexicon_commit")
    if not raw or len(raw) > MAX_BYTES or not license_text.startswith("MIT License"):
        raise ValueError("invalid_lexicon_source")
    document = json.loads(raw.decode("utf-8-sig"))
    words = document.get("words") if isinstance(document, dict) else None
    if not isinstance(words, list) or not 1 <= len(words) <= 100_000:
        raise ValueError("invalid_lexicon_words")
    if any(not isinstance(word, str) or not word.strip() or len(word) > 128 for word in words):
        raise ValueError("invalid_lexicon_word")
    normalized = tuple(sorted({normalizer.normalize(word.strip()) for word in words}))
    if any(not word for word in normalized):
        raise ValueError("empty_normalized_word")
    return LexiconData(
        commit, hashlib.sha256(raw).hexdigest(), license_text, raw.decode("utf-8"), normalized
    )


def _download(url: str, limit: int) -> bytes:
    request = Request(url, headers={"User-Agent": "AL1S-lexicon-updater"})
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=20) as response:
        result = response.read(limit + 1)
    if len(result) > limit:
        raise ValueError("lexicon_download_too_large")
    return cast(bytes, result)


def fetch_lexicon(normalizer: TextNormalizer) -> LexiconData:
    metadata = json.loads(
        _download(
            f"https://api.github.com/repos/{REPOSITORY}/commits/main",
            512 * 1024,
        )
    )
    commit = metadata.get("sha")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("invalid_upstream_commit")
    root = f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}"
    license_text = _download(f"{root}/LICENSE", 16_384).decode("utf-8")
    raw = _download(f"{root}/{SOURCE_PATH}", MAX_BYTES)
    return parse_lexicon(raw, commit, license_text, normalizer)
