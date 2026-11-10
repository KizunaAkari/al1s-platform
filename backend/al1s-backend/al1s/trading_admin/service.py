from uuid import UUID

from al1s.freqtrade.contracts import FreqtradeCommand, FreqtradeReadRequest
from al1s.quant.commands import normalize_keyword
from al1s.quant.messages import QuantAdminCommandRequest, QuantAdminReadRequest
from al1s.trading_admin.contracts import AdminTradingSource, TradingActionWrite


def _public(row):
    return {key: row[key] for key in ("id", "market", "action", "status", "result")}


class TradingAdminService:
    def __init__(self, repository, quant_settings, quant_gateway, quant_process, freqtrade_repository, freqtrade_client):
        self.repository = repository
        self.quant_settings, self.quant_gateway, self.quant_process = quant_settings, quant_gateway, quant_process
        self.freqtrade_repository, self.freqtrade_client = freqtrade_repository, freqtrade_client

    def _settings(self, service_id: UUID, market: str):
        if market == "a-share":
            record = self.quant_settings.get(service_id)
            versions = (record.row_version, record.mapping_version)
            if (not record.settings.enabled or self.quant_process is None
                    or self.quant_gateway.applied_versions(service_id) != versions
                    or self.quant_process.applied_versions(service_id) != versions):
                raise ValueError("trading_service_unavailable")
            return record
        if self.freqtrade_repository is None or self.freqtrade_client is None:
            raise ValueError("trading_service_unavailable")
        record = self.freqtrade_repository.get(service_id)
        if (not record.settings.enabled or market == "futures" and not record.settings.futures_enabled
                or self.freqtrade_client.applied(service_id) != record.row_version):
            raise ValueError("trading_service_unavailable")
        return record

    def query(self, service_id: UUID, market: str, kind: str, page: int):
        owner = self.repository.owner_for(service_id)
        self._settings(service_id, market)
        source = AdminTradingSource(sender_id=owner[0], target_id=owner[0], message_id="0" * 32)
        if market == "a-share":
            result = self.quant_process.query_admin(service_id, QuantAdminReadRequest(source=source, kind=kind, page=page))
        else:
            result = self.freqtrade_client.query_admin(service_id, FreqtradeReadRequest(
                source=source, market=market, kind=kind, page=page))
        pending = self.repository.pending(service_id, market)
        if self.repository.owner_for(service_id) != owner:
            raise ValueError("trading_owner_changed")
        result["owner_version"] = owner[1]
        result["pending_operation"] = (_public(pending) if pending and pending.get("id") else pending)
        return result

    def submit(self, service_id: UUID, market: str, data: TradingActionWrite, key: str):
        arguments = data.arguments(market)
        record = self._settings(service_id, market)
        operation, created = self.repository.claim(service_id, market, data.action, arguments, key, data.owner_version)
        if not created:
            return _public(operation)
        identity = UUID(operation["id"])
        try:
            source = AdminTradingSource(sender_id=operation["qq_id"], target_id=operation["qq_id"], message_id=identity.hex)
            action = data.action
            if market == "futures":
                action = {"open": "f_open", "deposit": "f_deposit", "margin": "f_margin"}.get(action, action)
            keyword = normalize_keyword(record.commands[action])
            if market == "a-share":
                command = QuantAdminCommandRequest(source=source, action=action, arguments=list(arguments),
                    mapping_version=record.mapping_version, settings_version=record.row_version,
                    event_timestamp=operation["event_time"], normalized_keyword=keyword,
                    settings=record.settings, commands=record.commands, reply_templates=record.reply_templates).command()
            else:
                command = FreqtradeCommand(source=source, action=action, arguments=arguments,
                    event_timestamp=operation["event_time"], settings_version=record.row_version,
                    mapping_version=record.row_version, normalized_keyword=keyword,
                    settings=record.settings, commands=record.commands, reply_templates=record.reply_templates)
        except (KeyError, TypeError, ValueError):
            self.repository.finish(identity, "failed", "请求未发送，请检查配置后重试。")
            return _public(self.repository.operation(service_id, market, identity))
        if not self.repository.start_dispatch(identity):
            raise ValueError("trading_operation_conflict")
        try:
            if market == "a-share":
                result = self.quant_process.execute_admin(service_id, command)
                status = "completed"
            else:
                result = self.freqtrade_client.execute_admin(service_id, command)
                status = "completed" if self.repository.freqtrade_status(identity) == "completed" else "unknown"
            self.repository.finish(identity, status, result)
        except Exception:
            self.repository.finish(identity, "unknown", "请求结果待核实，请查看委托与成交，勿重复操作。")
        return _public(self.repository.operation(service_id, market, identity))

    def operation(self, service_id: UUID, market: str, identity: UUID):
        return _public(self.repository.operation(service_id, market, identity))
