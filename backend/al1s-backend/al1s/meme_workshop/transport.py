from dataclasses import dataclass
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BackendResponse:
    status: int
    body: bytes
    media_type: str


class MemeBackendClient:
    """One bounded direct HTTP attempt to the existing Bot container; no retries or redirects."""

    def __init__(self, url: str, token: str) -> None:
        self._address = urlsplit(url)
        self._token = token
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
        self, method: str, path: str, body: bytes, headers: dict[str, str]
    ) -> BackendResponse:
        if not self._address.hostname or len(self._token.encode()) < 32:
            raise ValueError("meme_backend_not_configured")
        if len(body) > 56 * 1024 * 1024 or not path.startswith("/api/v1/bots/"):
            raise ValueError("invalid_meme_backend_request")
        factory = HTTPSConnection if self._address.scheme == "https" else HTTPConnection
        connection = factory(self._address.hostname, self._address.port, timeout=15)
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
