from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ParsedSecret:
    identifier: UUID
    secret: str


class SecretDigester:
    """Issue and verify versioned scrypt digests without persisting plaintext."""

    _N = 2**14
    _R = 8
    _P = 1
    _DKLEN = 32

    def issue(self, identifier: UUID) -> tuple[str, str]:
        secret = secrets.token_urlsafe(32)
        return f"{identifier}.{secret}", self.digest(secret)

    def digest(self, secret: str) -> str:
        salt = secrets.token_bytes(16)
        derived = hashlib.scrypt(
            secret.encode("utf-8"),
            salt=salt,
            n=self._N,
            r=self._R,
            p=self._P,
            dklen=self._DKLEN,
        )
        salt_text = base64.urlsafe_b64encode(salt).decode("ascii")
        digest_text = base64.urlsafe_b64encode(derived).decode("ascii")
        return f"scrypt${self._N}${self._R}${self._P}${salt_text}${digest_text}"

    def verify(self, secret: str, encoded: str) -> bool:
        try:
            algorithm, n, r, p, salt_text, digest_text = encoded.split("$", 5)
            if algorithm != "scrypt":
                return False
            salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
            expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
            actual = hashlib.scrypt(
                secret.encode("utf-8"),
                salt=salt,
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=len(expected),
            )
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(actual, expected)
