"""Trusted reference ownership; shared blob bodies have no exclusive module owner.

Adding a reference requires registering its table here AND installing the 0024
reference-lock trigger in a migration. GC must check all owners, never just one.
These predicates are infrastructure helpers, not end-user authorization.
"""

from collections.abc import Iterator
from types import MappingProxyType

from sqlalchemy import column, exists, select, table
from sqlalchemy.sql.elements import ColumnElement

from al1s.adapters.postgres.models import BlobObjectRow

BLOB_REFERENCE_OWNERS = MappingProxyType({
    'lineup_recognitions': 'lineup',
    "linux_releases": "platform",
    "terminal_artifacts": "maa",
    "maa_script_version_blobs": "maa",
    "maa_quick_test_blobs": "maa",
    "execution_snapshot_blobs": "maa",
})


def no_blob_references() -> Iterator[ColumnElement[bool]]:
    """Return correlated predicates covering every registered business reference."""
    for name in BLOB_REFERENCE_OWNERS:
        reference = table(name, column("blob_id"))
        yield ~exists(select(1).select_from(reference).where(
            reference.c.blob_id == BlobObjectRow.id,
        ))
