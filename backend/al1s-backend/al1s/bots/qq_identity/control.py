import json
import re
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from urllib.parse import urlsplit
from uuid import UUID

from al1s.bots.control_client import ControlClient
from al1s.bots.qq_identity.contracts import QqIdentityError, QqIdentitySave, QqIdentityState
from al1s.components.host.bot_maintenance import BotMaintenanceGuard
from al1s.meme_workshop.transport import BackendResponse, MemeBackendClient

RUNTIME_PATH = "/api/v1/bots/qq/device-identity"


def identity_response(status: int, body: bytes) -> QqIdentityState:
    if len(body) > 32768:
        raise QqIdentityError("qq_identity_response_invalid", 502)
    if status != 200:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise QqIdentityError("qq_identity_response_invalid", 502)
        code = value.get("code", "qq_identity_request_rejected")
        if not isinstance(code, str) or not re.fullmatch(r"qq_[a-z_]{1,60}", code):
            code = "qq_identity_request_rejected"
        raise QqIdentityError(code, status if status in {409, 422, 503} else 502)
    return QqIdentityState.model_validate_json(body)


class QqIdentityControlClient:
    def __init__(self, url: str, token: str):
        self.client = ControlClient(url, token)

    def read(self) -> QqIdentityState:
        return self._call(None, {})

    def save(self, body: QqIdentitySave, revision: str, request_id: UUID) -> QqIdentityState:
        return self._call(body, {"If-Match": revision, "Idempotency-Key": str(request_id)})

    def _call(self, body: QqIdentitySave | None, headers: dict[str, str]) -> QqIdentityState:
        address = urlsplit(self.client.url)
        assert address.hostname is not None
        factory = HTTPSConnection if address.scheme == "https" else HTTPConnection
        connection = factory(address.hostname, address.port, timeout=25)
        try:
            connection.request(
                "PUT" if body else "GET", "/qq/device-identity",
                body.model_dump_json().encode() if body else None,
                headers={**headers, "Content-Type": "application/json",
                         "Authorization": "Bearer " + self.client.token},
            )
            response = connection.getresponse()
            return identity_response(response.status, response.read(32769))
        except QqIdentityError:
            raise
        except (OSError, HTTPException, ValueError) as error:
            raise QqIdentityError("qq_identity_unavailable_or_result_unknown", 503) from error
        finally:
            connection.close()


class QqIdentityGateway:
    def __init__(self, backend: MemeBackendClient, maintenance: BotMaintenanceGuard | None):
        self.backend, self.maintenance = backend, maintenance

    def read(self) -> QqIdentityState:
        return self._forward("GET", b"", {})

    def save(self, body: QqIdentitySave, revision: str, request_id: UUID) -> QqIdentityState:
        if self.maintenance is None:
            raise QqIdentityError("qq_identity_maintenance_unconfigured", 503)
        try:
            with self.maintenance.exclusive("qq"):
                return self._forward("PUT", body.model_dump_json().encode(),
                                     {"If-Match": revision, "Idempotency-Key": str(request_id)})
        except QqIdentityError:
            raise
        except (OSError, HTTPException, ValueError) as error:
            raise QqIdentityError("qq_identity_maintenance_in_progress", 409) from error

    def _forward(self, method: str, body: bytes, headers: dict[str, str]) -> QqIdentityState:
        try:
            response: BackendResponse = self.backend.forward(
                method, RUNTIME_PATH, body, {**headers, "Content-Type": "application/json"}
            )
            return identity_response(response.status, response.body)
        except QqIdentityError:
            raise
        except (OSError, HTTPException, ValueError) as error:
            raise QqIdentityError("qq_identity_unavailable_or_result_unknown", 503) from error
