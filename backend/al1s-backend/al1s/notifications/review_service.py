import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from al1s.kernel.types import NewAuditEntry
from al1s.notifications.errors import NotificationAdapterError, NotificationDomainError
from al1s.notifications.lexicon import LexiconData
from al1s.notifications.lexicon_service import LexiconService
from al1s.notifications.ports import NotificationUnitOfWork
from al1s.notifications.review_policy import ReviewPolicy, ReviewState
from al1s.notifications.review_ports import ReviewRecord
from al1s.notifications.types import (
    ChannelKind,
    NewNotificationDelivery,
    NotificationIntentRecord,
    NotificationKind,
)
from al1s.secrets.security import FernetSecretCipher


def policy(row: ReviewRecord) -> ReviewPolicy:
    return ReviewPolicy(
        ReviewState(row.state), row.received_at, row.pending_at, row.approved_at, row.finalized_at
    )


def summary(row: ReviewRecord) -> dict[str, object]:
    return {
        "id": str(row.id),
        "service_id": str(row.service_id),
        "message_id": row.message_id,
        "state": row.state,
        "received_at": row.received_at.isoformat(),
        "row_version": row.row_version,
        "lexicon_commit": row.lexicon_commit,
        "intent_id": str(row.intent_id) if row.intent_id else None,
        "pending_at": row.pending_at.isoformat() if row.pending_at else None,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "finalized_at": row.finalized_at.isoformat() if row.finalized_at else None,
        "normalization_version": row.normalization_version,
        "failure_reason": (
            ("manual_rejection" if row.pending_at else "source_not_allowed")
            if row.state == ReviewState.REJECTED
            else None
        ),
    }


def audit(uow: NotificationUnitOfWork, action: str, row: ReviewRecord) -> None:
    uow.audit.add(
        NewAuditEntry(
            audit_id=uuid4(),
            actor_type="system",
            actor_id=None,
            action=f"review.{action}",
            target_type="message_review",
            target_id=row.id,
            correlation_id=row.id,
            details={"state": row.state, "row_version": row.row_version},
        )
    )


class ReviewService:
    def __init__(
        self,
        uow: Callable[[], NotificationUnitOfWork],
        cipher: FernetSecretCipher,
        lexicon: LexiconService,
    ):
        self._uow = uow
        self._cipher = cipher
        self._lexicon = lexicon

    def receive_rule_match(
        self,
        service_id: UUID,
        rule_id: UUID,
        rule_version: int,
        guild_id: str,
        channel_id: str,
        message_ids: list[str],
        body: str,
    ) -> dict[str, object]:
        if not message_ids or len(message_ids) > 64 or len(set(message_ids)) != len(message_ids):
            raise NotificationDomainError("invalid_forward_source", "Invalid message IDs")
        if any(not re.fullmatch(r"[1-9][0-9]{16,19}", item) for item in message_ids):
            raise NotificationDomainError("invalid_forward_source", "Invalid message IDs")
        if not body or len(body.encode("utf-8")) > 512_000:
            raise NotificationDomainError("invalid_forward_body", "Forward body exceeds limit")
        event_id = uuid5(NAMESPACE_URL, f"discord-rule:{rule_id}:{','.join(message_ids)}")
        digest = self._cipher.fingerprint(body)
        now = datetime.now(UTC)
        with self._uow() as uow:
            actions = uow.forward_rules.enabled_actions(
                service_id,
                rule_id,
                rule_version,
                guild_id,
                channel_id,
            )
            if actions is None:
                raise NotificationDomainError("discord_rule_stale", "Rule changed", 409)
            if not actions:
                raise NotificationDomainError(
                    "discord_rule_no_destination", "No active action", 403
                )
            lexicon_result = None
            if any(action.review_policy == "lexicon" for action in actions):
                try:
                    lexicon_result = self._lexicon.match(body)
                except Exception:
                    # A failed lexicon lookup holds reviewed targets for retry.
                    lexicon_result = None
            keys = {
                action.id: uuid5(NAMESPACE_URL, f"discord-rule-action:{event_id}:{action.id}").hex
                for action in actions
            }
            existing = {
                row.message_id: row
                for row in uow.reviews.by_sources(
                    service_id,
                    list(keys.values()),
                )
            }
            rows: list[dict[str, object]] = []
            for action in actions:
                key = keys[action.id]
                old = existing.get(key)
                if old:
                    if old.source_digest != digest:
                        raise NotificationDomainError(
                            "source_content_conflict", "Source changed", 409
                        )
                    continue
                requires_review = action.review_policy == "lexicon"
                state = (
                    (
                        ReviewState.HOLD
                        if lexicon_result is None
                        else ReviewState.PENDING
                        if lexicon_result[1]
                        else ReviewState.APPROVED
                    )
                    if requires_review
                    else ReviewState.APPROVED
                )
                target = (
                    f"private:{action.target}"
                    if action.kind == "qq_private"
                    else f"group:{action.target}"
                    if action.kind == "qq_group"
                    else action.target
                )
                document = {
                    "original": body,
                    "text": body,
                    "matches": list(lexicon_result[1])
                    if requires_review and lexicon_result
                    else [],
                    "source_message_ids": message_ids,
                    "direct_action": {
                        "action_id": str(action.id),
                        "channel_id": str(action.channel_id),
                        "kind": "smtp" if action.kind == "smtp" else "qq",
                        "target": target,
                    },
                }
                rows.append(
                    {
                        "id": uuid4(),
                        "service_id": service_id,
                        "message_id": key,
                        "source_digest": digest,
                        "state": state.value,
                        "body_cipher": self._cipher.encrypt(
                            json.dumps(document, ensure_ascii=False)
                        ),
                        "key_id": self._cipher.key_id,
                        "received_at": now,
                        "row_version": 1,
                        "checked_at": now,
                        "pending_at": now if state == ReviewState.PENDING else None,
                        "approved_at": now if state == ReviewState.APPROVED else None,
                        "finalized_at": None,
                        "lexicon_commit": lexicon_result[0].commit
                        if requires_review and lexicon_result
                        else None,
                        "normalization_version": (
                            lexicon_result[0].normalization_version
                            if requires_review and lexicon_result
                            else None
                        ),
                    }
                )
            inserted_ids = uow.reviews.add_many(rows)
            new_rows = [row for row in uow.reviews.batch(list(inserted_ids))]
            approved = [row for row in new_rows if row.state == ReviewState.APPROVED]
            self._enqueue_many(uow, approved, now)
            for row in new_rows:
                audit(uow, "received", row)
            uow.commit()
            return {
                "id": str(event_id),
                "state": "accepted",
                "actions": len(actions),
                "replayed": len(new_rows) == 0,
            }

    @staticmethod
    def _required(uow: NotificationUnitOfWork, review_id: UUID) -> ReviewRecord:
        row = uow.reviews.get(review_id)
        if row is None or row.deleted_at is not None:
            raise NotificationDomainError("review_not_found", "Review not found", 404)
        return row

    def _document(self, row: ReviewRecord) -> dict[str, object]:
        if not row.body_cipher or not row.key_id:
            raise NotificationDomainError("review_body_deleted", "Body unavailable", 410)
        value = json.loads(self._cipher.decrypt(row.body_cipher, row.key_id))
        if not isinstance(value, dict):
            raise ValueError("invalid_review_document")
        return value

    def _finish(self, row: ReviewRecord, state: ReviewState, now: datetime) -> None:
        retained = None
        if state == ReviewState.REJECTED and row.body_cipher:
            document = self._document(row)
            retained = self._cipher.encrypt(
                json.dumps(
                    {key: document[key] for key in ("original", "text") if key in document},
                    ensure_ascii=False,
                )
            )
        row.state = state.value
        row.finalized_at = now
        row.body_cipher = retained
        row.key_id = self._cipher.key_id if retained else None
        row.row_version += 1

    @staticmethod
    def _erase_summary(row: ReviewRecord, now: datetime) -> None:
        row.deleted_at = row.deleted_at or now
        row.body_cipher = row.key_id = None
        row.lexicon_commit = row.normalization_version = None
        row.pending_at = row.approved_at = row.finalized_at = None
        row.intent_id = None
        row.row_version += 1

    def _enqueue(self, uow: NotificationUnitOfWork, row: ReviewRecord, now: datetime) -> None:
        self._enqueue_many(uow, [row], now)

    def _enqueue_many(
        self, uow: NotificationUnitOfWork, rows: list[ReviewRecord], now: datetime
    ) -> None:
        if not rows:
            return
        intents = []
        outgoing: list[NewNotificationDelivery] = []
        for row in rows:
            document = self._document(row)
            direct = document.get("direct_action")
            if isinstance(direct, dict):
                intent_id = uuid4()
                intents.append(
                    NotificationIntentRecord(
                        intent_id,
                        row.id,
                        NotificationKind.FORWARD,
                        "discord",
                        row.id,
                        row.id,
                        1,
                        {"review_id": str(row.id)},
                        "queued",
                        now,
                        now,
                    )
                )
                outgoing.append(
                    NewNotificationDelivery(
                        uuid4(),
                        intent_id,
                        None,
                        UUID(str(direct["channel_id"])),
                        ChannelKind(str(direct["kind"])),
                        (str(direct["target"]),),
                        "forward.v1",
                        now,
                    )
                )
                continue
            intent_id = uuid4()
            intents.append(
                NotificationIntentRecord(
                    intent_id,
                    row.id,
                    NotificationKind.FORWARD,
                    "discord",
                    row.id,
                    row.id,
                    1,
                    {"review_id": str(row.id)},
                    "no_route",
                    now,
                    now,
                )
            )
        # Insert referenced intents before ORM flushes review.intent_id foreign keys.
        uow.intents.add_many(intents)
        uow.deliveries.add_many(outgoing)
        for row, intent in zip(rows, intents, strict=True):
            row.intent_id = intent.intent_id
            if intent.disposition == "no_route":
                self._finish(row, ReviewState.COMPLETED, now)

    def page(
        self, state: str | None, before: datetime | None, before_id: UUID | None, limit: int
    ) -> list[dict[str, object]]:
        with self._uow() as uow:
            return [summary(row) for row in uow.reviews.page(state, before, before_id, limit)]

    def detail(self, review_id: UUID) -> dict[str, object]:
        with self._uow() as uow:
            row = self._required(uow, review_id)
            now = datetime.now(UTC)
            if policy(row).expired(now) or policy(row).summary_expired(now):
                return {**summary(row), "body_available": False}
            return {
                **summary(row),
                "body_available": bool(row.body_cipher),
                "document": self._document(row) if row.body_cipher else None,
            }

    def approve(self, review_id: UUID, version: int, text: str | None) -> dict[str, object]:
        now = datetime.now(UTC)
        with self._uow() as uow:
            row = self._required(uow, review_id)
            if row.row_version != version or not policy(row).can_approve(now):
                raise NotificationDomainError(
                    "review_not_approvable",
                    "Review changed or expired",
                    409,
                )
            document = self._document(row)
            if not isinstance(document.get("direct_action"), dict):
                raise NotificationDomainError(
                    "legacy_forward_retired", "Legacy forwarding review cannot be approved", 409
                )
            if text is not None:
                if not 1 <= len(text) <= 16_000:
                    raise NotificationDomainError("invalid_review_text", "Invalid text")
                document["text"] = text
            row.body_cipher = self._cipher.encrypt(json.dumps(document, ensure_ascii=False))
            row.state, row.approved_at = ReviewState.APPROVED.value, now
            row.row_version += 1
            self._enqueue(uow, row, now)
            result = summary(row)
            audit(uow, "approved", row)
            uow.commit()
            return result

    def bulk(self, ids: list[UUID], action: str) -> list[dict[str, object]]:
        if not 1 <= len(ids) <= 50 or action not in {"reject", "delete"}:
            raise NotificationDomainError("invalid_review_batch", "Invalid batch")
        now = datetime.now(UTC)
        with self._uow() as uow:
            rows = {row.id: row for row in uow.reviews.batch(list(set(ids)))}
            result = []
            for identity in dict.fromkeys(ids):
                row = rows.get(identity)
                allowed = row is not None and (
                    (
                        action == "reject"
                        and (
                            policy(row).can_approve(now)
                            or (row.state == "rejected" and not policy(row).summary_expired(now))
                        )
                        and row.deleted_at is None
                    )
                    or (action == "delete" and policy(row).can_delete())
                )
                if allowed and row is not None:
                    if action == "reject" and row.state == "pending":
                        self._finish(row, ReviewState.REJECTED, now)
                    elif action == "delete":
                        self._erase_summary(row, now)
                    result.append({"id": str(identity), "accepted": True})
                    audit(uow, action, row)
                else:
                    result.append({"id": str(identity), "accepted": False})
            uow.commit()
            return result

    def sending_body(self, review_id: UUID) -> str:
        with self._uow() as uow:
            row = self._required(uow, review_id)
            if not policy(row).can_send(datetime.now(UTC)):
                raise NotificationAdapterError("review_not_sendable", retryable=False)
            return str(self._document(row)["text"])

    def maintain(self) -> int:
        now = datetime.now(UTC)
        try:
            lexicon = self._lexicon.current()
        except Exception:
            lexicon = None
        with self._uow() as uow:
            rows = uow.reviews.maintenance()
            unsettled = uow.reviews.unsettled([row.intent_id for row in rows if row.intent_id])
            expired_intents = [
                row.intent_id for row in rows if row.intent_id and policy(row).expired(now)
            ]
            uow.reviews.cancel_unsettled(expired_intents, now)
            newly_approved = []
            for row in rows:
                row.checked_at = now
                current = policy(row)
                before = (row.state, row.deleted_at)
                if current.expired(now):
                    self._finish(row, ReviewState.EXPIRED, now)
                elif row.state == "approved" and row.intent_id not in unsettled:
                    self._finish(row, ReviewState.COMPLETED, now)
                elif current.summary_expired(now):
                    self._erase_summary(row, now)
                elif row.state == "hold":
                    self._retry_hold(uow, row, now, lexicon)
                    if row.state == "approved":
                        newly_approved.append(row)
                if before != (row.state, row.deleted_at):
                    audit(uow, "maintained", row)
            self._enqueue_many(uow, newly_approved, now)
            uow.commit()
            return len(rows)

    def _retry_hold(
        self,
        uow: NotificationUnitOfWork,
        row: ReviewRecord,
        now: datetime,
        lexicon: LexiconData | None,
    ) -> None:
        if lexicon is None:
            return
        try:
            document = self._document(row)
            matches = self._lexicon.match_snapshot(str(document["original"]), lexicon)
        except Exception:
            return
        document["matches"] = list(matches)
        row.body_cipher = self._cipher.encrypt(json.dumps(document, ensure_ascii=False))
        row.lexicon_commit = lexicon.commit
        row.normalization_version = lexicon.normalization_version
        row.row_version += 1
        if matches:
            row.state, row.pending_at = "pending", now
        else:
            row.state, row.approved_at = "approved", now
