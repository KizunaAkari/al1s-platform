import json
import os
import sys
from http.client import HTTPConnection


def healthcheck(role: str) -> bool:
    if role == "qq" and os.environ.get("PROTOCOL_MODE") == "pmhq":
        from al1s.bots.qq_headed.health import read_pmhq_health
        if not read_pmhq_health().ready:
            return False
    checks = (
        [(3101, "/health/live"), (3080, "/")]
        if role == "qq"
        else [(3101, "/health/live"), (3100, "/health")]
    )
    for port, path in checks:
        connection = HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            if response.status not in {200, 401}:
                return False
            body = response.read(16_384)
            if port == 3101 and (
                not json.loads(body).get("live") or response.getheader("X-AL1S-Meme-Role") != role
            ):
                return False
        except (OSError, ValueError):
            return False
        finally:
            connection.close()
    return True


if __name__ == "__main__":
    sys.exit(0 if healthcheck(os.environ.get("AL1S_MEME_ROLE", "qq")) else 1)
