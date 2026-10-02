from urllib.parse import urlsplit
from uuid import UUID


def validate_public_url(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("platform_public_url contains control characters")
    value = value.strip().rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    _ = parsed.port  # Validate malformed and out-of-range ports before storing config.
    if (
        parsed.scheme not in {"http", "https"}
        or any(character.isspace() for character in value)
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("platform_public_url must be an HTTP(S) origin without credentials")
    return value


def task_link(public_url: str, payload: dict[str, object]) -> str | None:
    if not public_url:
        return None
    try:
        identity = UUID(str(payload.get("task_id")))
    except ValueError:
        return None
    return f"{public_url}/tasks/history?task_id={identity}"
