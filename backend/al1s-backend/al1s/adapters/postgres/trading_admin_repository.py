import hashlib
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from al1s.adapters.postgres.bot_models import BotServiceRow
from al1s.adapters.postgres.freqtrade_models import FreqtradeFuturesProfileRow, FreqtradeProfileRow, FreqtradeRequestRow
from al1s.adapters.postgres.meme_base import fingerprint
from al1s.adapters.postgres.quant_accounts import _qq_id
from al1s.adapters.postgres.quant_models import QuantAccountRow, QuantOrderRow
from al1s.adapters.postgres.trading_admin_models import TradingAdminOperationRow, TradingAdminOwnerRow
from al1s.freqtrade.contracts import WRITE_ACTIONS


def _operation(row):
    return {"id": str(row.id), "service_id": str(row.service_id), "qq_id": row.qq_id,
            "market": row.market, "action": row.action, "status": row.status,
            "event_time": row.event_time, "result": row.result}


class TradingAdminRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    def owner(self):
        with self.sessions() as session:
            row = session.get(TradingAdminOwnerRow, 1)
            return {"qq_id": row.qq_id, "row_version": row.row_version} if row else {"qq_id": None, "row_version": 0}

    def owner_for(self, service_id: UUID):
        with self.sessions() as session:
            owner = session.get(TradingAdminOwnerRow, 1)
            service = session.get(BotServiceRow, service_id)
            if owner is None:
                raise ValueError("trading_owner_required")
            if service is None or service.kind != "onebot_gateway" or not service.enabled or service.deleted_at is not None:
                raise ValueError("qq_bot_not_found")
            return owner.qq_id, owner.row_version

    def pending(self, service_id: UUID, market: str):
        with self.sessions() as session:
            owner = session.get(TradingAdminOwnerRow, 1)
            if owner is None:
                return None
            row = session.scalar(select(TradingAdminOperationRow).where(
                TradingAdminOperationRow.qq_id == owner.qq_id, TradingAdminOperationRow.market == market,
                TradingAdminOperationRow.status.in_(("running", "unknown"))).limit(1))
            if row is None:
                return None
            return _operation(row) if row.service_id == service_id else {"status": "other_service"}

    def set_owner(self, qq_id: str, expected_version: int):
        qq_id = _qq_id(qq_id)
        with self.sessions.begin() as session:
            row = session.get(TradingAdminOwnerRow, 1, with_for_update=True)
            if row is None:
                if expected_version != 0:
                    raise ValueError("trading_owner_conflict")
                inserted = session.scalar(insert(TradingAdminOwnerRow).values(
                    id=1, qq_id=qq_id, row_version=1, updated_at=datetime.now(UTC))
                    .on_conflict_do_nothing(index_elements=["id"]).returning(TradingAdminOwnerRow.id))
                if inserted is None:
                    raise ValueError("trading_owner_conflict")
                return {"qq_id": qq_id, "row_version": 1}
            if row.row_version != expected_version:
                if row.qq_id == qq_id and row.row_version == expected_version + 1:
                    return {"qq_id": row.qq_id, "row_version": row.row_version}
                raise ValueError("trading_owner_conflict")
            if row.qq_id != qq_id:
                pending = session.scalar(select(TradingAdminOperationRow.id).where(
                    TradingAdminOperationRow.qq_id == row.qq_id,
                    TradingAdminOperationRow.status.in_(("running", "unknown"))).limit(1))
                if pending is not None:
                    raise ValueError("trading_owner_busy")
                row.qq_id, row.row_version, row.updated_at = qq_id, row.row_version + 1, datetime.now(UTC)
            return {"qq_id": row.qq_id, "row_version": row.row_version}

    def claim(self, service_id: UUID, market: str, action: str, arguments: tuple[str, ...], key: str, expected_owner_version: int):
        if not isinstance(key, str):
            raise ValueError("invalid_idempotency_key")
        try:
            operation_id = UUID(key)
            if operation_id.version != 4 or str(operation_id) != key.lower():
                raise ValueError
        except (ValueError, AttributeError) as error:
            raise ValueError("invalid_idempotency_key") from error
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        with self.sessions.begin() as session:
            owner = session.get(TradingAdminOwnerRow, 1, with_for_update=True)
            if owner is None:
                raise ValueError("trading_owner_required")
            if owner.row_version != expected_owner_version:
                raise ValueError("trading_owner_changed")
            service = session.get(BotServiceRow, service_id)
            if service is None or service.kind != "onebot_gateway" or not service.enabled or service.deleted_at is not None:
                raise ValueError("qq_bot_not_found")
            digest = fingerprint({"qq_id": owner.qq_id, "market": market, "action": action, "arguments": arguments})
            existing = session.get(TradingAdminOperationRow, operation_id)
            if existing is not None:
                if existing.service_id != service_id or existing.request_hash != digest:
                    raise ValueError("idempotency_conflict")
                return _operation(existing), False
            pending = session.scalar(select(TradingAdminOperationRow.id).where(
                TradingAdminOperationRow.qq_id == owner.qq_id, TradingAdminOperationRow.market == market,
                TradingAdminOperationRow.status.in_(("running", "unknown"))).limit(1))
            if pending is not None:
                raise ValueError("trading_operation_pending")
            now = datetime.now(UTC)
            row = TradingAdminOperationRow(id=operation_id, service_id=service_id, qq_id=owner.qq_id,
                market=market, action=action, key_hash=key_hash, request_hash=digest, status="running",
                dispatch_started=False, event_time=int(now.timestamp()), created_at=now, updated_at=now)
            session.add(row)
            session.flush()
            return _operation(row), True

    def start_dispatch(self, operation_id: UUID):
        with self.sessions.begin() as session:
            row = session.get(TradingAdminOperationRow, operation_id, with_for_update=True)
            if row is None or row.status != "running" or row.dispatch_started:
                return False
            row.dispatch_started, row.updated_at = True, datetime.now(UTC)
            return True

    @staticmethod
    def _uncertain(session, row):
        if row.action == "open":
            return
        if row.market == "a-share":
            account = session.scalar(select(QuantAccountRow).where(QuantAccountRow.qq_user_id == row.qq_id).with_for_update())
            if account is not None and account.state != "needs_review":
                account.state, account.version, account.updated_at = "needs_review", account.version + 1, datetime.now(UTC)
                row.account_protected, row.protected_version = True, account.version
        else:
            table = FreqtradeProfileRow if row.market == "spot" else FreqtradeFuturesProfileRow
            account = session.scalar(select(table).where(table.qq_id == row.qq_id).with_for_update())
            if account is not None and account.state == "ready":
                account.state = "unknown"
                row.account_protected = True

    def finish(self, operation_id: UUID, status: str, result: str):
        if status not in {"completed", "failed", "unknown"} or not isinstance(result, str) or len(result) > 4096:
            raise ValueError("trading_operation_invalid")
        with self.sessions.begin() as session:
            row = session.get(TradingAdminOperationRow, operation_id, with_for_update=True)
            if row is None or row.status != "running":
                return False
            row.status, row.result, row.updated_at = status, result, datetime.now(UTC)
            if status == "unknown":
                self._uncertain(session, row)
            return True

    def freqtrade_status(self, operation_id: UUID):
        with self.sessions() as session:
            row = session.get(TradingAdminOperationRow, operation_id)
            if row is None:
                return None
            receipt = session.scalar(select(FreqtradeRequestRow).where(FreqtradeRequestRow.service_id == row.service_id,
                FreqtradeRequestRow.scope_kind == "admin", FreqtradeRequestRow.sender_id == row.qq_id,
                FreqtradeRequestRow.message_id == row.id.hex, FreqtradeRequestRow.event_time == row.event_time))
            return receipt.status if receipt is not None else None

    def recover(self):
        with self.sessions.begin() as session:
            rows = session.scalars(select(TradingAdminOperationRow).where(
                TradingAdminOperationRow.status == "running").with_for_update()).all()
            for row in rows:
                row.status = "unknown" if row.dispatch_started else "failed"
                row.result = "结果待核实，请查看委托与成交，勿重复操作。" if row.dispatch_started else "请求未发送，请重新操作。"
                row.updated_at = datetime.now(UTC)
                if row.dispatch_started:
                    self._uncertain(session, row)

    def operation(self, service_id: UUID, market: str, operation_id: UUID):
        with self.sessions.begin() as session:
            owner = session.get(TradingAdminOwnerRow, 1)
            row = session.get(TradingAdminOperationRow, operation_id, with_for_update=True)
            if owner is None or row is None or row.qq_id != owner.qq_id or row.service_id != service_id or row.market != market:
                raise ValueError("trading_operation_not_found")
            if row.status == "unknown":
                if market == "a-share":
                    if row.action == "open":
                        account = session.scalar(select(QuantAccountRow).where(QuantAccountRow.qq_user_id == row.qq_id))
                        if account is not None:
                            row.status, row.result = "completed", "账户已存在，未重复开户。"
                    recorded = session.scalar(select(QuantOrderRow).where(QuantOrderRow.service_id == service_id,
                        QuantOrderRow.scope_kind == "admin", QuantOrderRow.sender_id == row.qq_id,
                        QuantOrderRow.message_id == row.id.hex, QuantOrderRow.event_time == row.event_time))
                    if recorded is not None and recorded.status in {"processing", "partially_filled", "unknown"}:
                        row.result = "委托已记录，成交状态待核实，请勿重复操作。"
                    elif recorded is not None:
                        row.status, row.result = "completed", "委托已记录，请查看委托与成交状态。"
                        account = session.get(QuantAccountRow, recorded.account_id, with_for_update=True)
                        other = session.scalar(select(QuantOrderRow.id).where(QuantOrderRow.account_id == recorded.account_id,
                            QuantOrderRow.status.in_(("processing", "partially_filled", "unknown"))).limit(1))
                        if (row.account_protected and account is not None and account.state == "needs_review"
                                and account.version == row.protected_version and other is None):
                            account.state, account.version, account.updated_at = "active", account.version + 1, datetime.now(UTC)
                else:
                    recorded = session.scalar(select(FreqtradeRequestRow).where(FreqtradeRequestRow.service_id == service_id,
                        FreqtradeRequestRow.scope_kind == "admin", FreqtradeRequestRow.sender_id == row.qq_id,
                        FreqtradeRequestRow.message_id == row.id.hex, FreqtradeRequestRow.event_time == row.event_time))
                    if recorded is not None and recorded.status == "completed":
                        row.status, row.result = "completed", recorded.result or "请求已处理，请查看成交。"
                        table = FreqtradeProfileRow if market == "spot" else FreqtradeFuturesProfileRow
                        account = session.scalar(select(table).where(table.qq_id == row.qq_id).with_for_update())
                        other = session.scalar(select(FreqtradeRequestRow.id).where(
                            FreqtradeRequestRow.sender_id == row.qq_id, FreqtradeRequestRow.market == market,
                            FreqtradeRequestRow.status.in_(("processing", "unknown")),
                            FreqtradeRequestRow.action.in_(list(WRITE_ACTIONS))).limit(1))
                        if row.account_protected and account is not None and account.state == "unknown" and other is None:
                            account.state = "ready"
                row.updated_at = datetime.now(UTC)
            return _operation(row)
