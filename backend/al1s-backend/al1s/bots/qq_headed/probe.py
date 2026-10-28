import json
import shutil
import subprocess
from pathlib import Path

from al1s.meme_workshop.runtime_identity import RuntimeIdentity


def prove_headed(identity: RuntimeIdentity, supervisor: Path) -> None:
    from al1s.bots.qq_headed.health import health_body, parse_health
    try:
        qq = json.loads(Path("/opt/QQ/resources/app/package.json").read_text())["version"]
        pmhq = subprocess.run(["/opt/pmhq", "--version"], capture_output=True,
                              timeout=5, check=True).stdout.decode().strip()
        if (qq != identity.qq_client_version or pmhq != "PMHQ v" + str(identity.pmhq_version)
                or not shutil.which("Xvfb") or not shutil.which("xdpyinfo")
                or "command=python -m al1s.bots.qq_headed.pmhq_process"
                not in supervisor.read_text()):
            raise ValueError("qq_headed_capability_invalid")
        active = health_body()
        if active is not None:
            parse_health(active, expected_version=identity.qq_client_version)
    except (OSError, KeyError, subprocess.SubprocessError) as error:
        raise ValueError("qq_headed_capability_missing") from error
