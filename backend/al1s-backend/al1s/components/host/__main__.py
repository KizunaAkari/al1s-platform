import argparse
from pathlib import Path

import uvicorn

from al1s.components.host.app import create_host
from al1s.components.host.config import HostSettings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    arguments = parser.parse_args()
    settings = HostSettings.model_validate_json(arguments.config.read_text(encoding="utf-8"))
    uvicorn.run(
        create_host(settings),
        host=settings.listen,
        port=settings.port,
        workers=1,
        ssl_certfile=str(settings.certificate_file),
        ssl_keyfile=str(settings.private_key_file),
        access_log=False,
    )


if __name__ == "__main__":
    main()
