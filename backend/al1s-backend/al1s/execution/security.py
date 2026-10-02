from __future__ import annotations

import hashlib
from uuid import UUID

from al1s.execution.errors import AuthenticationError, InvalidRequestError
from al1s.secrets.digests import ParsedSecret
from al1s.secrets.digests import SecretDigester as SecretDigester


def parse_registration_code(value: str) -> ParsedSecret:
    return _parse_compound_secret(value, "invalid_registration_code")


def parse_terminal_credential(value: str) -> ParsedSecret:
    try:
        return _parse_compound_secret(value, "invalid_terminal_credential")
    except InvalidRequestError as exc:
        raise AuthenticationError() from exc


def _parse_compound_secret(value: str, error_code: str) -> ParsedSecret:
    identifier_text, separator, secret = value.partition(".")
    if not separator or not secret:
        raise InvalidRequestError(error_code, "Credential format is invalid")
    try:
        identifier = UUID(identifier_text)
    except ValueError as exc:
        raise InvalidRequestError(error_code, "Credential format is invalid") from exc
    return ParsedSecret(identifier=identifier, secret=secret)


def digest_external_identifier(source_type: str, identifier: str) -> tuple[str, str]:
    normalized = identifier.strip()
    if not normalized:
        raise InvalidRequestError("empty_device_identifier", "Device identifier cannot be empty")
    digest = hashlib.sha256(f"{source_type}\0{normalized}".encode()).hexdigest()
    hint = normalized if len(normalized) <= 8 else f"…{normalized[-8:]}"
    return digest, hint
