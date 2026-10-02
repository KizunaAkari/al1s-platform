"""Receive Docker syslog datagrams and physically remove records older than seven days."""

from __future__ import annotations

import base64
import json
import os
import re
import signal
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

RETENTION = timedelta(days=7)
PRUNE_INTERVAL = timedelta(seconds=30)
SEGMENT_NAME = re.compile(r"^\d{10}\.jsonl$")


def segment_start(path: Path) -> datetime:
    return datetime.strptime(path.stem, "%Y%m%d%H").replace(tzinfo=UTC)


def prune(directory: Path, now: datetime) -> None:
    cutoff = now - RETENTION
    for path in directory.iterdir():
        if path.is_file() and re.fullmatch(r"\d{10}\.tmp", path.name):
            path.unlink()
            continue
        if not path.is_file() or not SEGMENT_NAME.fullmatch(path.name):
            continue
        hour = segment_start(path)
        if hour + timedelta(hours=1) <= cutoff:
            path.unlink()
        elif hour < cutoff:
            rewrite_boundary(path, cutoff)


def rewrite_boundary(path: Path, cutoff: datetime) -> None:
    temporary = path.with_suffix(".tmp")
    with path.open("rb") as source, temporary.open("wb") as destination:
        for line in source:
            try:
                timestamp = datetime.fromisoformat(json.loads(line)["occurred_at"])
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
            if timestamp >= cutoff:
                destination.write(line)
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary, path)


def append(directory: Path, payload: bytes, occurred_at: datetime) -> None:
    path = directory / f"{occurred_at:%Y%m%d%H}.jsonl"
    record = {
        "occurred_at": occurred_at.isoformat(),
        "syslog_b64": base64.b64encode(payload).decode("ascii"),
    }
    with path.open("ab") as output:
        output.write(json.dumps(record, separators=(",", ":")).encode("ascii") + b"\n")


def event_time(payload: bytes, received_at: datetime) -> datetime:
    try:
        prefix, timestamp, _ = payload.split(b" ", 2)
        if not re.fullmatch(rb"<\d{1,3}>1", prefix):
            return received_at
        parsed = datetime.fromisoformat(timestamp.decode("ascii").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return received_at
        return min(parsed.astimezone(UTC), received_at)
    except (ValueError, UnicodeDecodeError):
        return received_at


def run() -> None:
    directory = Path(os.environ.get("AL1S_LOG_DIRECTORY", "/var/lib/al1s-logs"))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    port = int(os.environ.get("AL1S_SYSLOG_PORT", "15144"))
    bind_address = os.environ.get("AL1S_SYSLOG_BIND", "0.0.0.0")
    if not 1 <= port <= 65535:
        raise ValueError("invalid syslog port")
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
    receiver.bind((bind_address, port))
    receiver.settimeout(1)
    next_prune = datetime.min.replace(tzinfo=UTC)
    try:
        while not stopping:
            now = datetime.now(UTC)
            if now >= next_prune:
                prune(directory, now)
                next_prune = now + PRUNE_INTERVAL
            try:
                payload, _ = receiver.recvfrom(65535)
            except TimeoutError:
                continue
            received_at = datetime.now(UTC)
            occurred_at = event_time(payload, received_at)
            if occurred_at >= received_at - RETENTION:
                append(directory, payload, occurred_at)
    finally:
        receiver.close()


if __name__ == "__main__":
    run()
