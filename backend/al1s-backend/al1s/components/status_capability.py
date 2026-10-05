import base64
import hmac
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from al1s.execution.errors import AuthenticationError


class StatusCapability:
    def __init__(self, key: str, now: Callable[[], datetime] | None = None):
        if len(key.encode()) < 32:
            raise ValueError("status key requires at least 32 bytes")
        self.key = key.encode()
        self.now = now or (lambda: datetime.now(UTC))

    def issue(self, identity: UUID, expires: datetime) -> str:
        lifetime = expires.timestamp() - self.now().timestamp()
        if not 0 < lifetime <= 2700:
            raise ValueError("status lifetime must be at most 45 minutes")
        payload = f"component-status-v1:{identity}:{int(expires.timestamp())}".encode()
        signature = hmac.digest(self.key, payload, "sha256")
        return base64.urlsafe_b64encode(payload + b"." + signature).decode().rstrip("=")

    def verify(self, token: str, identity: UUID) -> UUID:
        try:
            if len(token) > 256:
                raise ValueError("oversized")
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            payload, signature = raw[:-33], raw[-32:]
            if raw[-33:-32] != b"." or not hmac.compare_digest(
                hmac.digest(self.key, payload, "sha256"), signature
            ):
                raise ValueError("signature")
            purpose, value, expires = payload.decode().split(":")
            if purpose != "component-status-v1" or UUID(value) != identity:
                raise ValueError("scope")
            if int(expires) <= self.now().timestamp():
                raise ValueError("expired")
        except (ValueError, UnicodeError, IndexError):
            raise AuthenticationError(
                "invalid_component_status_token", "Status capability is invalid"
            ) from None
        return identity
