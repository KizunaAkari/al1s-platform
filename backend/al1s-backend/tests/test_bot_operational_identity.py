import hashlib
import json

import pytest

from al1s.infrastructure.direct_http import DirectHttpError
from al1s.meme_workshop.operation_identity import discord_identity, qq_identity


def qq_data(tmp_path):
    (tmp_path / "machine_guid.bin").write_bytes(b"a" * 16)
    (tmp_path / "config_10001.json").write_text(
        json.dumps(
            {
                "ob11": {
                    "enable": True,
                    "connect": [
                        {"type": "http", "enable": True, "port": 3000, "token": "private-token"}
                    ],
                }
            }
        )
    )
    return {"AUTO_LOGIN_QQ": "10001"}


def test_qq_verification_rejects_a_different_live_account(tmp_path):
    env = qq_data(tmp_path)

    def post(url, token, body, **kwargs):
        assert token == "private-token"
        return (
            {"status": "ok", "retcode": 0, "data": {"online": True, "good": True}}
            if url.endswith("get_status")
            else {"status": "ok", "retcode": 0, "data": {"user_id": 20002}}
        )

    with pytest.raises(ValueError, match="identity"):
        qq_identity(env, tmp_path, post)


def test_offline_qq_keeps_a_proven_configured_identity(tmp_path):
    env = qq_data(tmp_path)
    result = qq_identity(
        env,
        tmp_path,
        lambda *_args, **_kw: {
            "status": "ok",
            "retcode": 0,
            "data": {"online": False, "good": True},
        },
    )
    assert result["account_state"] == "offline"
    assert result["binding"]["account"] == "10001"
    assert result["binding"]["device_guid_sha256"] == hashlib.sha256(b"a" * 16).hexdigest()


@pytest.mark.parametrize(
    "error", [OSError("native API not yet started"), DirectHttpError("transport_unknown", True)]
)
def test_prelogin_native_api_absence_uses_authenticated_actual_webui_status(tmp_path, error):
    env = qq_data(tmp_path)

    def unavailable(*_args, **_kwargs):
        raise error

    result = qq_identity(
        env,
        tmp_path,
        unavailable,
        webui=lambda *_: {"success": True, "data": {"online": False, "uin": "", "mode": "direct"}},
    )
    assert result["account_state"] == "offline"
    with pytest.raises(ValueError, match="identity"):
        qq_identity(
            env,
            tmp_path,
            unavailable,
            webui=lambda *_: {
                "success": True,
                "data": {"online": True, "uin": "20002", "mode": "direct"},
            },
        )


def test_discord_requires_live_worker_credential_and_service_binding(tmp_path):
    (tmp_path / "credential").write_text("private-worker\n")
    service = "12345678-1234-4123-8123-123456789abc"
    (tmp_path / "last-known-good.json").write_text(json.dumps({"service_id": service}))
    health = {
        "status": "starting",
        "discordReady": False,
        "identity": {
            "credential_sha256": hashlib.sha256(b"private-worker").hexdigest(),
            "service_id": service,
        },
    }
    assert discord_identity(tmp_path, health)["account_state"] == "offline"
    health["identity"]["credential_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="identity"):
        discord_identity(tmp_path, health)
