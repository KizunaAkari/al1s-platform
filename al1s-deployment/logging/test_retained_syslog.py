import base64
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from retained_syslog import append, event_time, prune


class RetentionTest(unittest.TestCase):
    def test_boundary_file_keeps_only_records_within_seven_days(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            now = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
            old = now - timedelta(days=7, hours=1)
            boundary_old = now - timedelta(days=7, minutes=1)
            boundary_new = now - timedelta(days=7) + timedelta(seconds=1)
            append(directory, b"delete-old", old)
            append(directory, b"delete-boundary", boundary_old)
            append(directory, b"keep-boundary", boundary_new)
            append(directory, b"keep-current", now)
            (directory / "2026091810.tmp").write_bytes(b"interrupted old rewrite")
            prune(directory, now)
            assert not (directory / "2026091810.tmp").exists()
            records = [json.loads(line) for path in sorted(directory.glob("*.jsonl"))
                       for line in path.read_text().splitlines()]
            assert [base64.b64decode(row["syslog_b64"]) for row in records] == [
                b"keep-boundary", b"keep-current",
            ]
            prune(directory, now + timedelta(seconds=30))
            records = [json.loads(line) for path in sorted(directory.glob("*.jsonl"))
                       for line in path.read_text().splitlines()]
            assert [base64.b64decode(row["syslog_b64"]) for row in records] == [
                b"keep-current",
            ]

    def test_syslog_event_timestamp_controls_retention(self) -> None:
        now = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
        assert event_time(b"<30>1 2026-09-18T10:29:00Z host app - - - msg", now) == (
            now - timedelta(days=7, minutes=1)
        )
        assert event_time(b"invalid", now) == now


if __name__ == "__main__":
    unittest.main()
