from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken


class SecretUnavailableError(RuntimeError):
    pass


class FernetSecretCipher:
    """Encrypt small secrets with a deployment key; plaintext never reaches persistence."""

    def __init__(self, master_key: str) -> None:
        raw = master_key.encode("utf-8")
        if len(raw) < 32:
            raise ValueError("platform master key must contain at least 32 bytes")
        # This domain separator is persisted compatibility: Stage 7A encrypted
        # notification secrets before the cipher became a shared platform service.
        derived = hashlib.sha256(b"al1s-notifications-v1\x00" + raw).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))
        self._fingerprint_key = hashlib.sha256(b"al1s-secret-fingerprint-v1\x00" + raw).digest()
        self.key_id = hashlib.sha256(raw).hexdigest()[:16]

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            raise ValueError("secret plaintext must not be empty")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str, key_id: str) -> str:
        if key_id != self.key_id:
            raise SecretUnavailableError("platform secret key is unavailable")
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise SecretUnavailableError("platform secret cannot be decrypted") from exc

    def fingerprint(self, plaintext: str) -> str:
        return hmac.new(
            self._fingerprint_key,
            plaintext.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
