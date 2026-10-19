import os

from al1s.bots.qq_identity.startup import native_command


def main() -> None:
    command = native_command(os.environ)
    if command is None:
        print("qq_identity_native_start_blocked", flush=True)
        raise SystemExit(78)
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
