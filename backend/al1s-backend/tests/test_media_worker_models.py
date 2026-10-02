"""The media process must load all tables referenced by its ORM mappings."""

import subprocess
import sys


def test_media_worker_import_resolves_foreign_key_tables() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from al1s.infrastructure import media_worker; "
            "from al1s.adapters.postgres.base import Base; "
            "Base.metadata.sorted_tables",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
