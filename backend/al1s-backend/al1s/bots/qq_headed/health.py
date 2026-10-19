import json
from http.client import HTTPConnection
from typing import cast

from al1s.bots.qq_headed.contracts import HeadedHealth


def parse_health(body: object, *, expected_version: str | None = None) -> HeadedHealth:
    if not isinstance(body, dict) or not body.get("qq_full_version"):
        raise ValueError("qq_pmhq_health_invalid")
    if expected_version and body["qq_full_version"] != expected_version:
        raise ValueError("qq_client_active_version_mismatch")
    account = str(body.get("uin") or "")
    online = bool(account and account != "0" and body.get("uid"))
    return HeadedHealth(ready=True, online=online, account=account if online else None)


def health_body() -> object | None:
    connection = HTTPConnection("127.0.0.1", 13000, timeout=2)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        body = response.read(16385)
        if response.status != 200 or len(body) > 16384:
            raise ValueError("qq_pmhq_health_invalid")
        return cast(object, json.loads(body))
    except (OSError, ValueError):
        return None
    finally:
        connection.close()


def read_pmhq_health() -> HeadedHealth:
    from al1s.meme_workshop.runtime_identity import read_runtime_identity
    try:
        identity = read_runtime_identity()
        return parse_health(health_body(), expected_version=identity.qq_client_version)
    except (OSError, ValueError):
        return HeadedHealth(ready=False, online=False)
