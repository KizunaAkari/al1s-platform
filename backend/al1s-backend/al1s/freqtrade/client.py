import hashlib
import json
from uuid import UUID

from al1s.freqtrade.contracts import (FreqtradeChartRequest, FreqtradeCommand, FreqtradeReadRequest, FreqtradeRecord)
from al1s.quant.process_client import _UnixConnection


def control_key(quant_key: str) -> str:
    if len(quant_key.encode()) < 32:
        raise ValueError("freqtrade_control_configuration_invalid")
    return hashlib.sha256(("al1s-freqtrade:" + quant_key).encode()).hexdigest()


class FreqtradeClient:
    def __init__(self, token: str, path: str = "/run/al1s-freqtrade/control.sock"):
        self.token, self.path = token, path

    def call(self, method: str, path: str, data=None, *, limit: int = 65536):
        connection = _UnixConnection(self.path, 150 if path.startswith(("/command/", "/admin/command/"))
                                     else (120 if path.startswith("/admin/query/") else (10 if path.startswith(("/query/", "/chart/")) else 5)))
        try:
            connection.request(
                method,
                path,
                body=json.dumps(data).encode() if data is not None else None,
                headers={"X-Freqtrade-Control": self.token, "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            content = response.read(limit + 1)
            if response.status != 200 or len(content) > limit:
                raise ValueError("freqtrade_process_unavailable")
            return json.loads(content)
        finally:
            connection.close()

    def activate(self, service_id: UUID, record: FreqtradeRecord) -> int:
        return self.call("PUT", f"/config/{service_id}", record.model_dump(mode="json"))[
            "row_version"
        ]

    def applied(self, service_id: UUID) -> int:
        return self.call("GET", f"/config/{service_id}")["row_version"]

    def status(self) -> dict:
        return self.call("GET", "/status")

    def execute(self, service_id: UUID, command: FreqtradeCommand) -> str:
        return self.call("POST", f"/command/{service_id}", command.model_dump(mode="json"))[
            "content"
        ]

    def execute_admin(self, service_id: UUID, command: FreqtradeCommand) -> str:
        return self.call("POST", f"/admin/command/{service_id}", command.model_dump(mode="json"))["content"]

    def query_admin(self, service_id: UUID, request: FreqtradeReadRequest) -> dict:
        return self.call("POST", f"/admin/query/{service_id}", request.model_dump(mode="json"))

    def query_for_ai(self, service_id: UUID, request: FreqtradeReadRequest) -> dict:
        return self.call("POST", f"/query/{service_id}", request.model_dump(mode="json"))

    def chart_for_ai(self, service_id: UUID, request: FreqtradeChartRequest) -> dict:
        return self.call("POST", f"/chart/{service_id}", request.model_dump(mode="json"),
                         limit=720896)

    def chart(self, service_id: UUID, command: FreqtradeCommand) -> dict:
        return self.call(
            "POST", f"/command/{service_id}", command.model_dump(mode="json"), limit=720896
        )
