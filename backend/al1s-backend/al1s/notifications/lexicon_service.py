from dataclasses import replace
from threading import Lock
from typing import Protocol

from al1s.notifications.lexicon import (
    NORMALIZATION_VERSION,
    LexiconData,
    TextNormalizer,
    fetch_lexicon,
    parse_lexicon,
)


class LexiconStored(Protocol):
    commit: str
    sha256: str
    raw: str
    license_text: str
    normalization_version: str


class LexiconRepository(Protocol):
    def active(self) -> LexiconStored | None: ...
    def activate(self, candidate: LexiconData) -> bool: ...


class LexiconService:
    def __init__(self, repository: LexiconRepository):
        self._repository = repository
        self._normalizer = TextNormalizer()
        self._lock = Lock()
        self._cached: LexiconData | None = None

    def current(self) -> LexiconData | None:
        row = self._repository.active()
        if row is None:
            return None
        if row.normalization_version != NORMALIZATION_VERSION:
            raise ValueError("lexicon_normalizer_incompatible")
        with self._lock:
            if (
                self._cached is None
                or self._cached.sha256 != row.sha256
                or self._cached.commit != row.commit
            ):
                candidate = parse_lexicon(
                    row.raw.encode("utf-8"), row.commit, row.license_text, self._normalizer
                )
                if candidate.sha256 != row.sha256:
                    raise ValueError("lexicon_storage_hash_mismatch")
                self._cached = replace(candidate, created_at=getattr(row, "created_at", None))
            return self._cached

    def update(self) -> bool:
        # Network and parsing finish before the repository opens a write transaction.
        candidate = fetch_lexicon(self._normalizer)
        return self._repository.activate(candidate)

    def match(self, text: str) -> tuple[LexiconData, tuple[str, ...]] | None:
        lexicon = self.current()
        if lexicon is None:
            return None
        return lexicon, lexicon.match(text, self._normalizer)

    def match_snapshot(self, text: str, lexicon: LexiconData) -> tuple[str, ...]:
        return lexicon.match(text, self._normalizer)
