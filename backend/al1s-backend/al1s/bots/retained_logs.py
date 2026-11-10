"""Bounded, read-only access to the existing retained Docker syslog files."""

from __future__ import annotations

import base64
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_ITEMS = 100
MAX_MESSAGE = 500
SEGMENT = re.compile(r"^\d{10}\.jsonl$")
PRIORITY = re.compile(r"^<(\d{1,3})>1$")
SECRET_KEY = re.compile(r"(?i)(token|secret|password|passwd|authorization|cookie|api[_-]?key|private[_-]?key)")
SECRET_VALUE = re.compile(
    r"(?i)([\"']?(?:access[_-]?token|refresh[_-]?token|token|secret|password|passwd|"
    r"authorization|cookie|api[_-]?key|private[_-]?key)[\"']?\s*[:=]\s*)"
    r"(?:\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s,;&}]+)"
)
BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;\"']+")
URL_CREDENTIAL = re.compile(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@")
URL_QUERY = re.compile(r"(?i)(https?://[^\s?#]+)\?[^\s\"']+")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
NETWORK = re.compile(r"(?i)connect|disconnect|reconnect|network|socket|timeout|offline|online|连接|断开|网络|超时|登录|离线|上线")
ERROR = re.compile(r"(?i)\b(error|exception|fatal|critical|failed|failure)\b|错误|失败|异常")
WARNING = re.compile(r"(?i)\b(warn|warning)\b|警告")
HTTP_ERROR = re.compile(r'"\s+([45]\d\d)\s+')


def clean_text(value: str) -> str:
    value = ANSI.sub("", value)
    value = "".join(char if char in "\n\t" or ord(char) >= 32 else "�" for char in value)
    value = URL_CREDENTIAL.sub(r"\1[已隐藏]@", value)
    value = URL_QUERY.sub(r"\1?[已隐藏]", value)
    value = BEARER.sub("Bearer [已隐藏]", value)
    return SECRET_VALUE.sub(r"\1[已隐藏]", value)


def clean_value(value):
    if isinstance(value, dict):
        return {key: "[已隐藏]" if SECRET_KEY.search(str(key)) else clean_value(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [clean_value(item) for item in value]
    return clean_text(value) if isinstance(value, str) else value


def describe(message: str, priority: int) -> tuple[str, str, bool]:
    parsed = None
    try:
        if message.lstrip().startswith(("{", "[")):
            parsed = json.loads(message)
    except (ValueError, RecursionError):
        pass
    level = str(parsed.get("level", "")).lower() if isinstance(parsed, dict) else ""
    http = HTTP_ERROR.search(message)
    if priority % 8 <= 3 or level in {"error", "critical", "fatal", "exception"} or ERROR.search(message) or (http and http[1].startswith("5")):
        category = "error"
    elif priority % 8 == 4 or level in {"warn", "warning"} or WARNING.search(message) or http:
        category = "warning"
    else:
        category = "info"
    try:
        sanitized = json.dumps(clean_value(parsed), ensure_ascii=False, separators=(",", ":")) if parsed is not None else clean_text(message)
    except (ValueError, RecursionError, TypeError):
        sanitized = clean_text(message)
    sanitized = sanitized.strip()
    if len(sanitized) > MAX_MESSAGE:
        sanitized = sanitized[:MAX_MESSAGE] + "…"
    return category, sanitized, category != "info" or bool(NETWORK.search(message))


def query_logs(directory: Path, alias: str, source: str, level: str, *,
               now: datetime | None = None, max_bytes: int = MAX_SCAN_BYTES,
               limit: int = MAX_ITEMS) -> dict:
    if alias not in {"qq", "discord"} or source not in {"all", "bot", "backend", "gateway", "ops"} or level not in {"all", "important"}:
        raise ValueError("invalid_log_filter")
    if not 1 <= max_bytes <= MAX_SCAN_BYTES or not 1 <= limit <= MAX_ITEMS:
        raise ValueError("invalid_log_limit")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = now - timedelta(days=7)
    names = {
        f"al1s/al1s-bot-{alias}": "bot",
        "al1s/al1s-platform-app": "backend",
        "al1s/al1s-platform-gateway": "gateway",
        "al1s/al1s-platform-ops": "ops",
    }
    paths = sorted((p for p in directory.iterdir() if SEGMENT.fullmatch(p.name)), reverse=True)
    entries = []
    remaining = max_bytes
    limited = False
    for index, path in enumerate(paths):
        try:
            hour = datetime.strptime(path.stem, "%Y%m%d%H").replace(tzinfo=UTC)
        except ValueError:
            continue
        if hour + timedelta(hours=1) <= cutoff:
            break
        try:
            with path.open("rb") as file:
                file.seek(0, 2)
                size = file.tell()
                start = max(0, size - remaining)
                file.seek(start)
                data = file.read(size - start)
        except FileNotFoundError:
            continue
        remaining -= len(data)
        if start:
            limited = True
            data = data.partition(b"\n")[2]
        for line in reversed(data.splitlines()):
            try:
                record = json.loads(line)
                observed = datetime.fromisoformat(record["occurred_at"].replace("Z", "+00:00"))
                if observed.tzinfo is None or not cutoff <= observed <= now:
                    continue
                payload = base64.b64decode(record["syslog_b64"], validate=True).decode("utf-8", "replace")
                parts = payload.split(" ", 7)
                if len(parts) != 8 or not (match := PRIORITY.fullmatch(parts[0])):
                    continue
                kind = names.get(parts[3])
                if not kind or (source != "all" and kind != source):
                    continue
                category, message, important = describe(parts[7], int(match[1]))
                if level == "important" and not important:
                    continue
                if message:
                    entries.append({"occurred_at": observed.isoformat(), "source": kind,
                                    "level": category, "message": message})
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                continue
            if len(entries) >= limit:
                limited = True
                break
        if len(entries) >= limit:
            break
        if remaining <= 0:
            limited = limited or index + 1 < len(paths)
            break
    entries.sort(key=lambda item: item["occurred_at"], reverse=True)
    return {"observed_at": now.isoformat(), "items": entries, "scan_limited": limited}


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(2)
    print(json.dumps(query_logs(Path("/var/lib/al1s-logs"), *sys.argv[1:]), ensure_ascii=False))
