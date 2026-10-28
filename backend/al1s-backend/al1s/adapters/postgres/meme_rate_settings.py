from typing import cast

from sqlalchemy import select

from al1s.adapters.postgres.meme_base import MemeRepositoryBase
from al1s.adapters.postgres.meme_models import MemeSendGateRow
from al1s.meme_workshop.rate_limits import MemePlatform, RateLimitRecord, RateLimitWrite


def record(row: MemeSendGateRow) -> RateLimitRecord:
    return RateLimitRecord(
        platform=cast(MemePlatform, row.platform),
        interval_seconds=row.interval_seconds,
        row_version=row.row_version,
    )


class MemeRateSettingsRepository(MemeRepositoryBase):
    def get_rate_limits(self) -> list[RateLimitRecord]:
        with self.sessions() as session:
            rows = session.scalars(select(MemeSendGateRow).order_by(MemeSendGateRow.platform)).all()
            return [record(row) for row in rows]

    def save_rate_limit(
        self, platform: MemePlatform, data: RateLimitWrite, *, expected_version: int
    ) -> RateLimitRecord:
        with self.sessions.begin() as session:
            row = session.get(MemeSendGateRow, platform, with_for_update=True)
            if row is None:
                raise ValueError("meme_rate_gate_not_initialized")
            if row.row_version != expected_version:
                raise ValueError("rate_limit_version_conflict")
            row.interval_seconds = data.interval_seconds
            row.row_version += 1
            # Configuration edits never release an existing cooldown or send reservation.
            return record(row)
