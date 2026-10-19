"""Bounded native startup; launcher exit is not QQ exit. Own process group only."""
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from al1s.bots.qq_headed.health import read_pmhq_health


def wait_ready(check: Callable[[], bool], *, attempts: int,
               sleep: Callable[[float], None] = time.sleep,
               cancelled: Callable[[], bool] = lambda: False) -> bool:
    for _ in range(attempts):
        if cancelled():
            return False
        if check():
            return True
        sleep(1)
    return False


def display_ready() -> bool:
    return subprocess.run(["xdpyinfo", "-display", ":99"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=3, check=False).returncode == 0


def owned_qq(group: int) -> bool:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if os.getpgid(int(entry.name)) != group:
                continue
            if (entry / "comm").read_text().strip() == "qq":
                return True
        except (OSError, ProcessLookupError):
            continue
    return False


def stop_group(group: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            return
        time.sleep(1)


def run_pmhq(config: Path) -> int:
    if not wait_ready(display_ready, attempts=20):
        return 78
    process = subprocess.Popen(["dbus-run-session", "/opt/pmhq", "--config=" + str(config)],
                               start_new_session=True)
    stopping = False

    def terminate(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        if not wait_ready(lambda: not stopping and owned_qq(process.pid)
                          and read_pmhq_health().ready, attempts=90, cancelled=lambda: stopping):
            print("qq_pmhq_start_failed", flush=True)
            return 78
        while not stopping:
            if not owned_qq(process.pid) or not display_ready():
                print("qq_client_stopped", flush=True)
                return 78
            time.sleep(2)
        return 0
    finally:
        stop_group(process.pid)
        process.wait(timeout=5)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main() -> int:
    if os.environ.get("AL1S_QQ_IDENTITY_BLOCKED") == "1":
        return 78
    if len(sys.argv) > 1 and sys.argv[1] == "llbot":
        if not wait_ready(lambda: read_pmhq_health().ready, attempts=100):
            return 78
        os.execvp("node", ["node", "--enable-source-maps", "/app/llbot/llbot.js",
                          "--pmhq-host=127.0.0.1", "--pmhq-port=13000"])
    return run_pmhq(Path("/app/llbot/data/pmhq/pmhq_config.json"))


if __name__ == "__main__":
    raise SystemExit(main())
