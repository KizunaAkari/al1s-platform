from __future__ import annotations

import pytest

from al1s.notifications.security import FernetSecretCipher, SecretUnavailableError


def test_secret_cipher_round_trips_without_storing_plaintext() -> None:
    cipher = FernetSecretCipher("stage7-notification-master-key-at-least-32-bytes")

    encrypted = cipher.encrypt("smtp-app-password")

    assert encrypted != "smtp-app-password"
    assert "smtp-app-password" not in encrypted
    assert cipher.decrypt(encrypted, cipher.key_id) == "smtp-app-password"


def test_secret_cipher_rejects_unknown_key() -> None:
    cipher = FernetSecretCipher("stage7-notification-master-key-at-least-32-bytes")

    with pytest.raises(SecretUnavailableError, match="unavailable"):
        cipher.decrypt(cipher.encrypt("secret"), "retired-key")


def test_secret_cipher_requires_deployment_strength_key() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        FernetSecretCipher("too-short")
