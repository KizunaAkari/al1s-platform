"""Observe actual local Bot identity without login, mutation, or chat messages."""

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from al1s.bots.qq_identity.contracts import QqIdentitySummary
from al1s.bots.qq_identity.summary import stored_summary
from al1s.infrastructure.direct_http import DirectHttpError, post_json_direct


class OperationIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["qq", "discord"]
    binding: dict[str, str]
    account_state: Literal["online", "offline"]
    qq_identity: QqIdentitySummary | None = None

    @model_validator(mode="after")
    def complete(self) -> "OperationIdentity":
        keys = (
            {"account", "device_guid_sha256", "protocol"}
            if self.role == "qq"
            else {"credential_sha256", "service_id"}
        )
        if self.role == "qq" and self.binding.get("mode") == "pmhq":
            keys = {"account", "mode", "client_instance_id"}
            UUID(self.binding.get("client_instance_id", ""))
        if set(self.binding) != keys:
            raise ValueError("bot_identity_incomplete")
        return self


def read_json(path: Path, maximum: int = 128 * 1024) -> Any:
    if path.stat().st_size > maximum:
        raise ValueError("bot_identity_metadata_oversized")
    return json.loads(path.read_text(encoding="utf-8"))


def stored_identity(
    role: Literal["qq", "discord"], env: Mapping[str, str], root: Path
) -> OperationIdentity:
    if role == "qq" and env.get("PROTOCOL_MODE") == "pmhq":
        from al1s.components.host.qq_headed_identity import observe_headed_identity
        return observe_headed_identity(root, env.get("AUTO_LOGIN_QQ", ""))
    if role == "qq":
        account = env.get("AUTO_LOGIN_QQ", "")
        if (
            not account.isdecimal()
            or not 1 <= len(account) <= 20
            or env.get("PROTOCOL", "linux").lower() not in {"", "linux", "lnx"}
            or env.get("PROTOCOL_MODE", "") == "pmhq"
        ):
            raise ValueError("qq_identity_unconfigured")
        guid = (root / "machine_guid.bin").read_bytes()
        if len(guid) != 16:
            raise ValueError("qq_device_identity_invalid")
        read_json(root / f"config_{account}.json")
        binding = {
            "account": account,
            "device_guid_sha256": hashlib.sha256(guid).hexdigest(),
            "protocol": "linux",
        }
    else:
        credential = (root / "credential").read_text(encoding="utf-8").strip()
        if not credential or len(credential) > 4096:
            raise ValueError("discord_worker_identity_invalid")
        source = read_json(root / "last-known-good.json")
        binding = {
            "credential_sha256": hashlib.sha256(credential.encode()).hexdigest(),
            "service_id": str(UUID(source["service_id"])),
        }
    return OperationIdentity(role=role, binding=binding, account_state="offline",
                             qq_identity=stored_summary(root) if role == "qq" else None)


def qq_identity(
    env: Mapping[str, str],
    root: Path,
    post: Callable[..., object] = post_json_direct,
    *,
    webui: Callable[..., object] | None = None,
) -> dict[str, Any]:
    if env.get("PROTOCOL_MODE") == "pmhq":
        from al1s.bots.qq_headed.health import read_pmhq_health
        observed = stored_identity("qq", env, root)
        health = read_pmhq_health()
        status = (webui or qq_webui_status)(env, root)
        data = status.get("data") if isinstance(status, dict) else None
        if (not health.ready or not isinstance(data, dict) or data.get("mode") != "pmhq"
                or type(data.get("online")) is not bool):
            raise ValueError("qq_pmhq_identity_unavailable")
        if ((health.account and health.account != observed.binding["account"])
                or (data.get("uin") and str(data["uin"]) != observed.binding["account"])):
            raise ValueError("qq_account_identity_mismatch")
        online = health.online and data["online"]
        return {**observed.model_dump(), "account_state": "online" if online else "offline"}
    account = env.get("AUTO_LOGIN_QQ", "")
    protocol = env.get("PROTOCOL", "linux").lower()
    if (
        not account.isdecimal()
        or not 1 <= len(account) <= 20
        or protocol not in {"", "linux", "lnx"}
        or env.get("PROTOCOL_MODE", "") == "pmhq"
    ):
        raise ValueError("qq_identity_unconfigured")
    guid = (root / "machine_guid.bin").read_bytes()
    if len(guid) != 16:
        raise ValueError("qq_device_identity_invalid")
    config = read_json(root / f"config_{account}.json")
    connections = [
        row
        for row in config.get("ob11", {}).get("connect", [])
        if row.get("enable") is True and row.get("type") == "http"
    ]
    if (
        len(connections) != 1
        or type(connections[0].get("port")) is not int
        or not 1 <= connections[0]["port"] <= 65535
    ):
        raise ValueError("qq_identity_api_unavailable")
    connection = connections[0]
    base = "http://127.0.0.1:" + str(connection["port"])
    token = str(connection.get("token") or "")
    try:
        status = post(base + "/get_status", token, {}, timeout_seconds=3)
    except (OSError, DirectHttpError):
        status = None
    if (
        not isinstance(status, dict)
        or status.get("status") != "ok"
        or status.get("retcode") != 0
        or not isinstance(status.get("data"), dict)
        or type(status["data"].get("online")) is not bool
    ):
        value = (webui or qq_webui_status)(env, root)
        if (
            not isinstance(value, dict)
            or value.get("success") is not True
            or not isinstance(value.get("data"), dict)
        ):
            raise ValueError("qq_identity_status_unavailable")
        data = value["data"]
        if (
            type(data.get("online")) is not bool
            or data.get("mode") != "direct"
            or (data.get("uin") and str(data["uin"]) != account)
        ):
            raise ValueError("qq_account_identity_mismatch")
        observed = stored_identity("qq", env, root)
        return {**observed.model_dump(), "account_state": "online" if data["online"] else "offline"}
    online = status["data"]["online"]
    if online:
        login = post(base + "/get_login_info", token, {}, timeout_seconds=3)
        if (
            not isinstance(login, dict)
            or login.get("status") != "ok"
            or login.get("retcode") != 0
            or not isinstance(login.get("data"), dict)
            or str(login["data"].get("user_id")) != account
        ):
            raise ValueError("qq_account_identity_mismatch")
    return {
        "role": "qq",
        "qq_identity": stored_summary(root).model_dump(),
        "binding": {
            "account": account,
            "device_guid_sha256": hashlib.sha256(guid).hexdigest(),
            "protocol": "linux",
        },
        "account_state": "online" if online else "offline",
    }


def qq_webui_status(env: Mapping[str, str], root: Path) -> object:
    token = (root / "webui_token.txt").read_text(encoding="utf-8").strip()
    if not token or len(token) > 4096:
        raise ValueError("qq_webui_identity_unavailable")
    port = int(env.get("WEBUI_PORT", "3080"))
    if not 1 <= port <= 65535:
        raise ValueError("qq_webui_identity_unavailable")
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(
            "GET",
            "/api/login-info",
            headers={"X-Webui-Token": hashlib.sha256(token.encode()).hexdigest()},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("qq_webui_identity_unavailable")
        return json.loads(response.read(16_384))
    finally:
        connection.close()


def discord_identity(root: Path, health: dict[str, Any]) -> dict[str, Any]:
    credential = (root / "credential").read_text(encoding="utf-8").strip()
    if not credential or len(credential) > 4096:
        raise ValueError("discord_worker_identity_invalid")
    source = read_json(root / "last-known-good.json")
    service = str(UUID(source["service_id"]))
    digest = hashlib.sha256(credential.encode()).hexdigest()
    identity = health.get("identity")
    if (
        health.get("status") not in {"starting", "ready"}
        or not isinstance(identity, dict)
        or identity.get("credential_sha256") != digest
        or identity.get("service_id") != service
        or type(health.get("discordReady")) is not bool
    ):
        raise ValueError("discord_worker_identity_mismatch")
    return {
        "role": "discord",
        "binding": {"credential_sha256": digest, "service_id": service},
        "account_state": "online" if health["discordReady"] else "offline",
    }


if __name__ == "__main__":
    if os.environ.get("AL1S_MEME_ROLE") == "qq":
        result = qq_identity(os.environ, Path("/app/llbot/data"))
    else:
        connection = HTTPConnection("127.0.0.1", 3100, timeout=3)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError("discord_worker_identity_unavailable")
            health = json.loads(response.read(16_384))
        finally:
            connection.close()
        result = discord_identity(
            Path(os.environ.get("AL1S_BOT_STATE_DIR", "/var/lib/al1s-plachta")), health
        )
    print(OperationIdentity.model_validate(result).model_dump_json())
