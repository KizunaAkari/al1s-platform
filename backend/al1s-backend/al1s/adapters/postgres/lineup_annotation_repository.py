"""Bounded, transactional human labels; original reports are never mutated."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, insert, select
from sqlalchemy.orm import Session, sessionmaker

from al1s.adapters.postgres.lineup_models import LineupRecognitionRow as Record
from al1s.adapters.postgres.lineup_workspace_models import (
    LineupAnnotationRegionRow as Region,
)
from al1s.adapters.postgres.lineup_workspace_models import (
    LineupAnnotationRow as Annotation,
)
from al1s.adapters.postgres.lineup_workspace_models import (
    LineupAnnotationSlotRow as Slot,
)
from al1s.execution.errors import ConflictError, NotFoundError
from al1s.lineup.annotations import validate_annotation


def load_annotations(session: Session, ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    if not ids:
        return {}
    rows = session.execute(
        select(Annotation, Slot, Region)
        .outerjoin(Slot, Slot.record_id == Annotation.record_id)
        .outerjoin(
            Region,
            and_(
                Region.record_id == Slot.record_id,
                Region.side == Slot.side,
                Region.slot_index == Slot.slot_index,
            ),
        )
        .where(Annotation.record_id.in_(ids))
        .order_by(Annotation.record_id, Slot.side, Slot.slot_index, Region.kind)
    )
    result: dict[UUID, dict[str, Any]] = {}
    slots: dict[tuple[UUID, str, int], dict[str, Any]] = {}
    for head, slot, region in rows:
        item = result.setdefault(
            head.record_id,
            dict(
                record_id=head.record_id,
                version=head.version,
                state=head.state,
                updated_at=head.updated_at,
                slots=[],
                teams={
                    side: size
                    for side, size in (("attack", head.attack_size), ("defense", head.defense_size))
                    if size
                },
            ),
        )
        if slot is None:
            continue
        key = (head.record_id, slot.side, slot.slot_index)
        if key not in slots:
            slots[key] = dict(
                side=slot.side, index=slot.slot_index, student_id=slot.student_id, regions=[]
            )
            item["slots"].append(slots[key])
        if region:
            slots[key]["regions"].append(
                dict(kind=region.kind, box=[region.x, region.y, region.width, region.height])
            )
    return result


class LineupAnnotationRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def save(self, record_id: UUID, version: int, payload: dict[str, Any]) -> dict[str, Any]:
        from al1s.adapters.postgres.lineup_record_reads import refresh_attention

        with self.sessions.begin() as session:
            record = session.scalar(select(Record).where(Record.id == record_id).with_for_update())
            if record is None:
                raise NotFoundError("lineup")
            head = session.get(Annotation, record_id)
            if version != (head.version if head else 0):
                raise ConflictError("lineup_annotation_conflict", "标注已被修改, 请重新读取后核对")
            doc = validate_annotation(payload, record.width, record.height)
            if head is None:
                head = Annotation(record_id=record_id)
                session.add(head)
            head.version, head.state = version + 1, doc["state"]
            head.attack_size = doc["teams"].get("attack", 0)
            head.defense_size = doc["teams"].get("defense", 0)
            head.updated_at = datetime.now(UTC)
            session.flush()
            self._replace_slots(session, record_id, doc["slots"])
            record.row_version += 1
            session.flush()
            refresh_attention(session, [record])
            return load_annotations(session, [record_id])[record_id]

    @staticmethod
    def _replace_slots(session: Session, record_id: UUID, slots: list[dict[str, Any]]) -> None:
        session.execute(delete(Region).where(Region.record_id == record_id))
        session.execute(delete(Slot).where(Slot.record_id == record_id))
        if slots:
            session.execute(
                insert(Slot),
                [
                    dict(
                        record_id=record_id,
                        side=s["side"],
                        slot_index=s["index"],
                        student_id=s["student_id"],
                    )
                    for s in slots
                ],
            )
        regions = [
            dict(
                record_id=record_id,
                side=s["side"],
                slot_index=s["index"],
                kind=r["kind"],
                x=r["box"][0],
                y=r["box"][1],
                width=r["box"][2],
                height=r["box"][3],
            )
            for s in slots
            for r in s["regions"]
        ]
        if regions:
            session.execute(insert(Region), regions)
