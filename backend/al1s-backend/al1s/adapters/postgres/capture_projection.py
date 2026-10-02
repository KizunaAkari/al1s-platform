"""Resolve explicitly expired screenshot references in one bounded page query."""

from collections import defaultdict
from collections.abc import Sequence
from contextlib import suppress
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from al1s.adapters.postgres.artifact_models import TerminalArtifactRow as Artifact
from al1s.adapters.postgres.delivery_models import TerminalReportRow as Report
from al1s.execution.task_captures import task_captures


def expired_capture_references(
    session: Session, reports: Sequence[Report]
) -> dict[UUID, set[UUID]]:
    references = {
        r.attempt_id: {c.artifact_id for c in task_captures(r.result_diagnostic)} for r in reports
    }
    identities = set().union(*references.values())
    if not identities:
        return {}
    owners = {r.attempt_id: r.terminal_id for r in reports}
    rows = session.scalars(
        select(Artifact).where(
            Artifact.owner_kind == "formal_attempt",
            Artifact.artifact_kind == "screenshot",
            Artifact.owner_id.in_(references),
            Artifact.terminal_id.in_(owners.values()),
            or_(
                Artifact.id.in_(identities),
                Artifact.idempotency_key.in_([str(i) for i in identities]),
            ),
        )
    ).all()
    matches: dict[tuple[UUID, UUID], list[Artifact]] = defaultdict(list)
    for item in rows:
        if item.terminal_id != owners.get(item.owner_id):
            continue
        aliases = {item.id}
        with suppress(ValueError):
            aliases.add(UUID(item.idempotency_key))
        for identity in aliases & references.get(item.owner_id, set()):
            matches[item.owner_id, identity].append(item)
    expired: dict[UUID, set[UUID]] = defaultdict(set)
    for (attempt, identity), candidates in matches.items():
        if len(candidates) == 1 and candidates[0].status == "expired":
            expired[attempt].add(identity)
    return dict(expired)
