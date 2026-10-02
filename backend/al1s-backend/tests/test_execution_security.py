from uuid import uuid4

from al1s.execution.security import (
    SecretDigester,
    digest_external_identifier,
    parse_registration_code,
    parse_terminal_credential,
)


def test_scrypt_digest_never_contains_plaintext_and_verifies() -> None:
    digester = SecretDigester()
    identifier = uuid4()
    compound, digest = digester.issue(identifier)
    parsed = parse_registration_code(compound)

    assert parsed.identifier == identifier
    assert parsed.secret not in digest
    assert digester.verify(parsed.secret, digest)
    assert not digester.verify(f"{parsed.secret}-wrong", digest)


def test_terminal_credential_uses_same_parseable_one_time_format() -> None:
    digester = SecretDigester()
    terminal_id = uuid4()
    credential, _ = digester.issue(terminal_id)

    assert parse_terminal_credential(credential).identifier == terminal_id


def test_external_identifiers_are_source_scoped_and_only_expose_hint() -> None:
    adb_digest, hint = digest_external_identifier("adb_serial", "  private-device-12345678  ")
    apk_digest, _ = digest_external_identifier("apk_installation", "private-device-12345678")

    assert adb_digest != apk_digest
    assert hint == "…12345678"
    assert "private-device" not in hint
