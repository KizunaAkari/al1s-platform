"""Shared network policy for requests that carry platform or Bot credentials."""

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


@dataclass(frozen=True)
class DirectHttpError(Exception):
    code: str
    retryable: bool


def post_json_direct(
    url: str,
    token: str | None,
    payload: dict[str, object],
    *,
    auth_scheme: str = "Bearer",
    timeout_seconds: float = 20,
) -> object:
    """Post once without ambient proxies or redirects; never expose response text."""
    headers = {"Content-Type": "application/json", "User-Agent": "AL1S/0.1"}
    if token:
        headers["Authorization"] = f"{auth_scheme} {token}"
    request = Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(
            request, timeout=timeout_seconds
        ) as response:
            raw = response.read(65_537)
        if len(raw) > 65_536:
            raise DirectHttpError("response_too_large", False)
        return json.loads(raw)
    except HTTPError as exc:
        raise DirectHttpError("http_error", exc.code == 429 or exc.code >= 500) from None
    except (URLError, TimeoutError, OSError):
        raise DirectHttpError("transport_unknown", True) from None
    except (ValueError, UnicodeError):
        raise DirectHttpError("invalid_response", False) from None
