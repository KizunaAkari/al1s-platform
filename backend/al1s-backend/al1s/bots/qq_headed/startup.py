import hashlib
import hmac
import json
import os
from pathlib import Path

from al1s.bots.qq_headed.profile import (
    prepare_client_profile,
    private_directory,
    validate_client_version,
)
from al1s.bots.qq_identity.repository import atomic_bytes, read_bytes


def prepare_pmhq_config(root: Path, account: str, authorization: str) -> Path:
    if not authorization or len(authorization) > 4096:
        raise ValueError("qq_pmhq_authorization_missing")
    prepare_client_profile(root, account)
    directory = private_directory(root / "pmhq")
    config = directory / "pmhq_config.json"
    atomic_bytes(config, json.dumps(dict(
        qq_path="/opt/QQ/qq", quick_login_qq=account, default_host="127.0.0.1",
        default_port=13000, auth_token=authorization, pmhq_api_token="",
        headless=False, debug=False, debug_pb=False, qq_console=False,
        qq_args="--no-sandbox --disable-dev-shm-usage",
    )).encode())
    return config


def prepare_runtime(root: Path, client_link: Path) -> None:
    authorization = os.environ.get("PMHQ_AUTH_TOKEN") or read_bytes(
        root / "auth_token.txt", 4096).decode().strip()
    prepare_pmhq_config(root, os.environ.get("AUTO_LOGIN_QQ", ""), authorization)
    client_link.parent.mkdir(parents=True, exist_ok=True)
    expected = root / "qq-client"
    if client_link.is_symlink():
        if client_link.resolve() != expected.resolve():
            raise ValueError("qq_client_mount_invalid")
    elif client_link.exists():
        if not client_link.is_dir() or any(client_link.iterdir()):
            raise ValueError("qq_client_mount_invalid")
        client_link.rmdir()
        client_link.symlink_to(expected, target_is_directory=True)
    else:
        client_link.symlink_to(expected, target_is_directory=True)


def main() -> None:
    os.environ.pop("LLBOT_DEV_NAME", None)
    meme_token = os.environ.get("AL1S_MEME_BACKEND_TOKEN", "")
    if meme_token and os.environ.get("AL1S_QUANT_DATABASE_URL"):
        os.environ["AL1S_QUANT_CONTROL_TOKEN"] = hmac.new(
            meme_token.encode(), b"al1s-quant-control-v1", hashlib.sha256).hexdigest()
        master_key = os.environ.get("AL1S_MEME_MASTER_KEY", "")
        if len(master_key.encode()) >= 32:
            from al1s.quant.credential_key import derive_quant_credential_key
            os.environ["AL1S_QUANT_CREDENTIAL_KEY"] = derive_quant_credential_key(master_key)
    try:
        from al1s.meme_workshop.runtime_identity import read_runtime_identity
        identity = read_runtime_identity()
        if identity.qq_protocol_mode != "pmhq" or os.environ.get("PROTOCOL_MODE") != "pmhq":
            raise ValueError("qq_runtime_mode_mismatch")
        prepare_runtime(Path("/app/llbot/data"), Path("/root/.config/QQ"))
        validate_client_version(Path("/app/llbot/data"), identity.qq_client_version or "")
        os.environ["AL1S_QQ_IDENTITY_BLOCKED"] = "0"
    except (OSError, ValueError):
        os.environ["AL1S_QQ_IDENTITY_BLOCKED"] = "1"
        print("qq_headed_bootstrap_failed", flush=True)
    os.execvp("supervisord", ["supervisord", "-c", "/etc/al1s-qq-supervisord.conf"])


if __name__ == "__main__":
    main()
