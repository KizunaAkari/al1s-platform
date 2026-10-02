import subprocess
import urllib.request


def main() -> None:
    result = subprocess.run(
        ["supervisorctl", "-c", "/etc/al1s/supervisord.conf", "status"],
        capture_output=True, text=True, timeout=3, check=True,
    )
    states = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            states[fields[0]] = fields[1]
    if set(states) != {
        "api", "events", "scheduler", "media", "notifications", "terminal-monitor", "release-verifier",
    }:
        raise SystemExit(1)
    if any(state != "RUNNING" for state in states.values()):
        raise SystemExit(1)
    with urllib.request.urlopen(
        "http://127.0.0.1:8000/api/v1/system/health/ready", timeout=3
    ):
        pass


if __name__ == "__main__":
    main()
