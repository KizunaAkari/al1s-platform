import hashlib
import json
import time
from threading import RLock
from uuid import UUID, uuid4

from al1s.components.host.contracts import HostDriver, JsonObject
from al1s.components.host.journal import HostJournal

FINAL = {"succeeded", "failed_safe"}
PUBLIC_FIELDS = ("operation_id", "state", "stage", "proof", "error_code", "recovery_point")


class HostComponentExecutor:
    def __init__(self, journal: HostJournal, driver: HostDriver):
        self.journal, self.driver = journal, driver
        self.instance = str(uuid4())
        self.reservation_lock = RLock()

    @staticmethod
    def payload_hash(payload: JsonObject) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def check_identity(self, record: JsonObject | None, payload: JsonObject) -> None:
        if record is not None and record.get("request_hash") not in {
            None,
            self.payload_hash(payload),
        }:
            raise ValueError("component_operation_identity_conflict")

    def public(self, record: JsonObject) -> JsonObject:
        return {key: record[key] for key in PUBLIC_FIELDS if key in record}

    def reserve(self, identity: UUID, payload: JsonObject) -> JsonObject:
        with self.reservation_lock, self.journal.exclusive(self.journal.scope(payload)):
            return self._reserve(identity, payload)

    def _reserve(self, identity: UUID, payload: JsonObject) -> JsonObject:
        previous = self.journal.load(identity)
        self.check_identity(previous, payload)
        if previous is not None:
            return self.status(identity)
        owner = self.journal.scope_record(self.journal.scope(payload))
        if owner and owner.get("operation_id") == str(identity):
            return self.status(identity)
        self.journal.assert_available(self.journal.scope(payload), identity)
        record = {
            "operation_id": str(identity),
            "state": "accepted",
            "stage": "host_queued",
            "proof": False,
            "payload": payload,
            "executor_instance": self.instance,
            "request_hash": self.payload_hash(payload),
        }
        self.journal.save(identity, record)
        return self.public(record)

    def status(self, identity: UUID) -> JsonObject:
        record = self.journal.load(identity)
        if record is None:
            return {
                "operation_id": str(identity),
                "state": "unknown",
                "stage": "not_received",
                "proof": False,
            }
        if record["state"] not in FINAL and record.get("executor_instance") != self.instance:
            record.update(state="unknown", stage="executor_interrupted", proof=False)
            self.journal.save(identity, record)
        elif record["state"] in FINAL:
            owner = self.journal.scope_record(self.journal.scope(record.get("payload", {})))
            if (
                owner
                and owner.get("operation_id") == str(identity)
                and owner.get("state") != record["state"]
            ):
                self.journal.save(identity, record)
        return self.public(record)

    def execute(self, identity: UUID, payload: JsonObject) -> JsonObject:
        previous = self.journal.load(identity)
        if previous is None:
            owner = self.journal.scope_record(self.journal.scope(payload))
            if owner and owner.get("operation_id") == str(identity):
                return self.status(identity)
        self.check_identity(previous, payload)
        if previous is not None and not (
            previous["state"] == "accepted" and previous.get("executor_instance") == self.instance
        ):
            return self.status(
                identity
            )  # Never repeat a physical operation merely because POST repeated.
        with self.journal.exclusive(self.journal.scope(payload)):
            previous = self.journal.load(identity)
            self.check_identity(previous, payload)
            if previous is not None and not (
                previous["state"] == "accepted"
                and previous.get("executor_instance") == self.instance
            ):
                return self.status(identity)
            self.journal.assert_available(self.journal.scope(payload), identity)
            record = {
                "operation_id": str(identity),
                "state": "preparing",
                "stage": "preparing",
                "proof": False,
                "payload": payload,
                "executor_instance": self.instance,
                "request_hash": hashlib.sha256(
                    json.dumps(payload, sort_keys=True).encode()
                ).hexdigest(),
            }
            self.journal.save(identity, record)
            return self._perform(identity, record)

    def _perform(self, identity: UUID, record: JsonObject) -> JsonObject:
        started = time.monotonic()
        try:
            point = self.driver.prepare(identity, record["payload"])
            record.update(
                recovery=point, recovery_point=str(identity), state="switching", stage="switching"
            )
            self.journal.save(identity, record)
            self.driver.switch(identity, record["payload"], point)
            record.update(state="verifying", stage="verifying")
            self.journal.save(identity, record)
            if time.monotonic() - started > 1800 or not self.driver.verify(
                identity, record["payload"], point
            ):
                raise RuntimeError("candidate_not_verified")
            record.update(state="succeeded", stage="completed", proof=True)
        except Exception:
            if record["state"] == "preparing":
                record.update(
                    state="failed_safe",
                    stage="prepare_failed",
                    proof=True,
                    error_code="component_prepare_failed",
                )
            else:
                record.update(
                    state="unknown",
                    stage="recovery_required",
                    proof=False,
                    error_code="component_upgrade_unconfirmed",
                )
                try:
                    state = self.driver.recover(identity, record["payload"], record["recovery"])
                    if state in FINAL:
                        record.update(state=state, proof=True, stage="reconciled")
                except Exception:
                    pass
        self.journal.save(identity, record)
        return self.public(record)

    def reconcile(self, identity: UUID) -> JsonObject:
        prior = self.journal.load(identity)
        with self.journal.exclusive(self.journal.scope(prior.get("payload", {}) if prior else {})):
            record = self.journal.load(identity)
            if record is None:
                # A persisted tombstone fences delayed POSTs for an unreceived operation.
                record = {
                    "operation_id": str(identity),
                    "state": "failed_safe",
                    "stage": "not_received_fenced",
                    "proof": True,
                    "executor_instance": self.instance,
                }
            elif record["state"] not in FINAL:
                state = self.driver.recover(
                    identity, record.get("payload", {}), record.get("recovery", {})
                )
                record.update(
                    state=state if state in FINAL else "unknown",
                    proof=state in FINAL,
                    stage="reconciled" if state in FINAL else "recovery_required",
                )
            self.journal.save(identity, record)
            return self.public(record)
