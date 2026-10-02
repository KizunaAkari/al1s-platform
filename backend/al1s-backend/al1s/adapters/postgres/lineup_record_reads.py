"""Per-image execution evidence, for both legacy singles and batch occurrences."""

from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.delivery_models import TerminalReportRow as Report
from al1s.adapters.postgres.lineup_annotation_repository import load_annotations
from al1s.adapters.postgres.lineup_models import LineupRecognitionRow as Record
from al1s.adapters.postgres.scheduling_models import ExecutionAttemptRow as Attempt
from al1s.adapters.postgres.scheduling_models import ExecutionRow as Execution
from al1s.adapters.postgres.scheduling_models import PlanOccurrenceRow as Occurrence
from al1s.adapters.postgres.scheduling_models import TaskRequestRow as Task
from al1s.lineup.attention import attention_reason, effective_lineup
from al1s.lineup.results import validated_result


def enrich_records(session: Session, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = [r["id"] for r in records]
    if not ids:
        return []
    query = (
        select(
            Record.id,
            Task.lifecycle_status,
            Execution.status,
            Attempt.result,
            Attempt.error_code,
            Report.result_diagnostic,
            Occurrence.status,
            Task.parameters,
        )
        .outerjoin(Task, Task.id == Record.task_id)
        .outerjoin(Occurrence, Occurrence.id == Record.occurrence_id)
        .outerjoin(
            Execution,
            or_(
                Execution.occurrence_id == Record.occurrence_id,
                and_(Record.occurrence_id.is_(None), Execution.task_request_id == Record.task_id),
            ),
        )
        .outerjoin(Attempt, Attempt.execution_id == Execution.id)
        .outerjoin(
            Report,
            and_(
                Report.attempt_id == Attempt.id,
                Report.report_kind == "attempt_result",
                Report.disposition == "accepted",
            ),
        )
        .where(Record.id.in_(ids))
        .distinct(Record.id)
        .order_by(
            Record.id,
            Execution.created_at.desc(),
            Attempt.attempt_no.desc(),
            Report.received_at.desc(),
        )
    )
    facts = {row[0]: row for row in session.execute(query)}
    annotations = load_annotations(session, ids)
    result = []
    for record in records:
        item = {
            key: value
            for key, value in record.items()
            if key not in {"blob_id", "terminal_id", "occurrence_id"}
        }
        item.update(
            state="uploaded", result=None, error_code=None, annotation=annotations.get(record["id"])
        )
        if record["task_id"]:
            _, lifecycle, state, outcome, error, diagnostic, occurrence_state, options = facts[
                record["id"]
            ]
            item["state"] = (
                "cancelled"
                if occurrence_state == "cancelled"
                else outcome or state or ("cancelled" if lifecycle == "cancelled" else "waiting")
            )
            item["error_code"] = error
            item["result"] = validated_result((diagnostic or {}).get("lineup"), record)
            item.update(
                {
                    key: (options or {}).get(key, "auto")
                    for key in ("layout_hint", "recognition_mode")
                }
            )
            if item["state"] == "success" and item["result"] is None:
                item.update(state="failure", error_code="lineup_result_invalid")
        item["attention_reason"] = attention_reason(item)
        item["needs_attention"] = item["attention_reason"] != "none"
        item["usable_result"] = effective_lineup(item)
        result.append(item)
    return result


def refresh_attention(session: Session, records: list[Record]) -> None:
    from al1s.adapters.postgres.lineup_repository import _dict

    details = enrich_records(session, [_dict(r) for r in records])
    for record, detail in zip(records, details, strict=True):
        record.needs_attention = detail["needs_attention"]
        record.attention_reason = detail["attention_reason"]
