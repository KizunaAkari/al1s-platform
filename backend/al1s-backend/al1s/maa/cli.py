from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast
from uuid import uuid4

from al1s.adapters.postgres.maa_unit_of_work import MaaSqlAlchemyUnitOfWork
from al1s.adapters.s3.blob_store import S3BlobStore
from al1s.app.config import Settings
from al1s.infrastructure.database import create_database_engine, create_session_factory
from al1s.maa.archive import parse_script_archive
from al1s.maa.import_service import MaaArchiveImportService, MaaUowFactory


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a controlled AL-1S script archive")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--expected-logical-sha256", required=True)
    arguments = parser.parse_args()

    content = arguments.archive.read_bytes()
    parsed = parse_script_archive(content)
    if parsed.logical_sha256 != arguments.expected_logical_sha256:
        parser.error("archive logical SHA-256 does not match the approved migration source")

    settings = Settings()
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    blob_store = S3BlobStore(settings)
    try:
        service = MaaArchiveImportService(
            cast(MaaUowFactory, lambda: MaaSqlAlchemyUnitOfWork(session_factory)),
            blob_store,
        )
        result = service.import_archive(content, correlation_id=uuid4())
        print(
            json.dumps(
                {
                    "batch_id": str(result.batch.batch_id),
                    "logical_sha256": result.batch.logical_sha256,
                    "status": result.batch.status.value,
                    "replayed": result.replayed,
                    "applications": result.batch.application_count,
                    "scripts": result.batch.script_count,
                    "resource_references": result.batch.resource_reference_count,
                    "unique_resources": result.batch.unique_resource_count,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        blob_store.close()
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
