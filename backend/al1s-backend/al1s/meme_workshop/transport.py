from dataclasses import dataclass
from http.client import HTTPConnection, HTTPSConnection
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BackendResponse:
    status: int
    body: bytes
    media_type: str


class MemeBackendClient:
    """One bounded direct HTTP attempt to the existing Bot container; no retries or redirects."""

    def __init__(self, url: str, token: str, *, expected_role: str | None = None) -> None:
        self._address = urlsplit(url)
        self._token = token
        self._role = expected_role
        if url and (
            self._address.scheme not in {"http", "https"}
            or not self._address.hostname
            or self._address.path not in {"", "/"}
            or self._address.username
            or self._address.password
            or self._address.query
            or self._address.fragment
        ):
            raise ValueError("invalid_meme_backend_url")

    def forward(
        self, method: str, path: str, body: bytes, headers: dict[str, str], *, timeout: float | None = None
    ) -> BackendResponse:
        if not self._address.hostname or len(self._token.encode()) < 32:
            raise ValueError("meme_backend_not_configured")
        if len(body) > 56 * 1024 * 1024 or not path.startswith("/api/v1/bots/"):
            raise ValueError("invalid_meme_backend_request")
        if timeout is None:
            ai_test = method == "POST" and re.fullmatch(
                r"/api/v1/bots/ai/(?:providers/test/[0-9a-fA-F-]{36}|"
                r"chat-profiles/[0-9a-fA-F-]{36}/[0-9a-fA-F-]{36}/test)", path)
            timeout = 65 if ai_test else 15
        if not 0 < timeout <= 180:
            raise ValueError("invalid_meme_backend_timeout")
        factory = HTTPSConnection if self._address.scheme == "https" else HTTPConnection
        connection = factory(self._address.hostname, self._address.port, timeout=timeout)
        try:
            connection.request(
                method,
                path,
                body or None,
                headers={
                    **headers,
                    "X-AL1S-Meme-Control": self._token,
                },
            )
            response = connection.getresponse()
            if self._role is not None and response.getheader("X-AL1S-Meme-Role") != self._role:
                raise ValueError("meme_backend_role_mismatch")
            if 300 <= response.status < 400:
                raise ValueError("meme_backend_redirect_rejected")
            encoded = response.read(8 * 1024 * 1024 + 1)
            if len(encoded) > 8 * 1024 * 1024:
                raise ValueError("meme_backend_response_too_large")
            media_type = response.getheader("Content-Type", "application/json")
            if len(media_type) > 128 or "\r" in media_type or "\n" in media_type:
                raise ValueError("meme_backend_invalid_response")
            return BackendResponse(response.status, encoded, media_type)
        finally:
            connection.close()
