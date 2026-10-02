"""Release-time bounded rebuild of the attention projection (no source-data rewrite)."""

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres import execution_models as execution_models
from al1s.adapters.postgres import models as models
from al1s.adapters.postgres.lineup_models import LineupRecognitionRow
from al1s.adapters.postgres.lineup_record_reads import refresh_attention
from al1s.app.config import get_settings


def main() -> None:
    engine = create_engine(get_settings().database_url)
    sessions = sessionmaker(engine)
    with engine.connect() as connection:
        if (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            != "20261001_0051"
        ):
            raise RuntimeError("Lineup workspace migration must finish before backfill")
    cursor = None
    count = 0
    while True:
        with sessions.begin() as session:
            query = select(LineupRecognitionRow).order_by(LineupRecognitionRow.id).limit(200)
            if cursor:
                query = query.where(LineupRecognitionRow.id > cursor)
            records = list(session.scalars(query.with_for_update()))
            if not records:
                break
            refresh_attention(session, records)
            cursor, count = records[-1].id, count + len(records)
    engine.dispose()
    print(f"Lineup attention projection rebuilt for {count} records")


if __name__ == "__main__":
    main()
