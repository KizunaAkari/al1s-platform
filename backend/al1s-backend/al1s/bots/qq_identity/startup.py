import os
from collections.abc import Mapping
from pathlib import Path

from al1s.bots.qq_identity.apply import apply_identity
from al1s.bots.qq_identity.repository import QqIdentityRepository
from al1s.meme_workshop.runtime_identity import read_runtime_identity


def native_command(environment: Mapping[str, str]) -> list[str] | None:
    return None if environment.get("AL1S_QQ_IDENTITY_BLOCKED") == "1" else ["/startup.sh"]


def main() -> None:
    if os.environ.get("PROTOCOL_MODE") == "pmhq":
        from al1s.bots.qq_headed.startup import main as headed_main
        headed_main()
        return
    try:
        identity = read_runtime_identity()
        result = apply_identity(QqIdentityRepository(Path("/app/llbot/data")),
                                identity.native_version or "")
        os.environ["AL1S_QQ_IDENTITY_BLOCKED"] = "1" if result.blocked else "0"
        if result.device_name is None:
            os.environ.pop("LLBOT_DEV_NAME", None)
        else:
            os.environ["LLBOT_DEV_NAME"] = result.device_name
        if result.blocked:
            print(result.error_code, flush=True)
    except (OSError, ValueError):
        os.environ["AL1S_QQ_IDENTITY_BLOCKED"] = "1"
        print("qq_identity_bootstrap_failed", flush=True)
    os.execvp("supervisord", ["supervisord", "-c", "/etc/al1s-qq-supervisord.conf"])


if __name__ == "__main__":
    main()
