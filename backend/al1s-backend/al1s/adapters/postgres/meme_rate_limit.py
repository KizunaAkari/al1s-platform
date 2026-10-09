from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.meme_models import MemeSendGateRow

INTERVALS = {"discord": 10, "qq": 30}
# A lost result keeps the reservation longer than the complete 15-second command deadline.
UNKNOWN_SEND_HOLD_SECONDS = 20


def reserve_send(session: Session, platform: str, receipt_id: UUID, now: datetime) -> bool:
    gate = session.get(MemeSendGateRow, platform, with_for_update=True)
    if gate is None:
        raise ValueError("meme_rate_gate_not_initialized")
    if gate.available_at > now:
        return False
    gate.owner_receipt_id = receipt_id
    gate.available_at = now + timedelta(seconds=INTERVALS[platform] + UNKNOWN_SEND_HOLD_SECONDS)
    return True


def finish_send(session: Session, receipt_id: UUID) -> None:
    gate = session.scalar(
        select(MemeSendGateRow)
        .where(
            MemeSendGateRow.owner_receipt_id == receipt_id,
        )
        .with_for_update()
    )
    if gate is not None:
        gate.available_at = datetime.now(UTC) + timedelta(seconds=INTERVALS[gate.platform])
        gate.owner_receipt_id = None
