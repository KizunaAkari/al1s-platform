import asyncio
import base64
from uuid import UUID

from al1s.freqtrade.commands import dispatch
from al1s.freqtrade.contracts import CAPITAL_COMMANDS, WRITE_ACTIONS


class FreqtradeQqAdapter:
    def __init__(self, repository, client, replies):
        self.repository, self.client, self.replies = repository, client, replies
        self.records = {}

    def applied(self, service_id: UUID) -> int:
        record = self.records.get(service_id)
        return record.row_version if record else 0

    def matches_connection(self, service_id: UUID, event_url: str | None) -> bool:
        record = self.records.get(service_id)
        return bool(
            record and record.settings.enabled and record.settings.event_ws_url == event_url
        )

    async def refresh(self):
        rows = await asyncio.to_thread(self.repository.qq_connections)
        records = await asyncio.to_thread(
            self.repository.snapshots, [row.service_id for row in rows]
        )
        accepted = []
        current = {}
        for row in rows:
            record = records.get(row.service_id)
            if (
                record is not None
                and await asyncio.to_thread(self.client.activate, row.service_id, record)
                == record.row_version
            ):
                current[row.service_id] = record
                accepted.append(row)
        self.records = current
        return accepted

    async def parse(self, row, event):
        record = self.records.get(row.service_id)
        if record is None or record.settings.event_ws_url != row.event_url:
            return None
        command = dispatch(record, event)
        if command is not None and not await asyncio.to_thread(
            self.repository.keyword_conflicts, row.service_id, command.normalized_keyword
        ):
            return command
        return None

    async def execute(self, row, command):
        if command.action in {"chart", "f_chart"}:
            try:
                result = await asyncio.to_thread(self.client.chart, row.service_id, command)
                image = (
                    base64.b64decode(result["image"], validate=True) if "image" in result else None
                )
                content = result["content"]
            except (OSError, ValueError, KeyError):
                image, content = None, "走势图暂不可用，请稍后重试。"
            await asyncio.to_thread(
                self.replies.enqueue,
                row.service_id,
                command,
                content,
                command.settings.reply_interval_seconds,
                image=image,
            )
            return
        try:
            content = await asyncio.to_thread(self.client.execute, row.service_id, command)
        except (OSError, ValueError):
            word = command.commands.get(
                "f_orders" if command.market == "futures" else "orders", "成交记录"
            )
            if command.action in CAPITAL_COMMANDS:
                content = f"资金操作结果未确认，请查询{word}，勿重复执行。"
            else:
                content = (
                    f"买卖请求结果未确认，请查询{word}，勿重复下单。"
                    if command.action in WRITE_ACTIONS
                    else "Freqtrade暂不可用，请稍后重试。"
                )
        await asyncio.to_thread(
            self.replies.enqueue,
            row.service_id,
            command,
            content,
            command.settings.reply_interval_seconds,
        )
