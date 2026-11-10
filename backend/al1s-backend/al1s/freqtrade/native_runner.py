import json
import sys
from pathlib import Path

from al1s.freqtrade.native_capital import install, validate_mode


def main():
    arguments = sys.argv[1:]
    if not arguments or arguments[0] != "trade" or "--config" not in arguments:
        raise SystemExit("capital_runner_trade_config_required")
    config = json.loads(Path(arguments[arguments.index("--config") + 1]).read_text())
    validate_mode(config)
    install()
    from freqtrade.main import main as freqtrade_main

    return freqtrade_main(arguments)


if __name__ == "__main__":
    sys.exit(main())
