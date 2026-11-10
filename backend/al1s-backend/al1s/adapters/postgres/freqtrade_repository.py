from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, union_all, update
from sqlalchemy.dialects.postgresql import insert

from al1s.adapters.postgres.bot_models import BotConfigVersionRow, BotServiceRow
from al1s.adapters.postgres.freqtrade_models import (
    FreqtradeFuturesProfileRow,
    FreqtradeProfileRow,
    FreqtradeRequestRow,
    FreqtradeSettingsRow,
    FreqtradeWordRow,
)
from al1s.adapters.postgres.meme_base import MemeRepositoryBase, fingerprint
from al1s.adapters.postgres.meme_models import MemeAliasRow
from al1s.adapters.postgres.meme_settings import QqConnection
from al1s.adapters.postgres.notification_models import EncryptedSecretRow
from al1s.adapters.postgres.quant_models import QuantCommandRow, QuantReplyRow
from al1s.adapters.postgres.quant_settings import QuantSettingsRepository
from al1s.adapters.postgres.trading_reply_models import load_templates
from al1s.freqtrade.commands import validate_words
from al1s.freqtrade.contracts import (
    CAPITAL_COMMANDS,
    DEFAULT_COMMANDS,
    WRITE_ACTIONS,
    FreqtradeCommand,
    FreqtradeRecord,
    FreqtradeSettings,
    Market,
)
from al1s.freqtrade.exit_notices import close_time, exit_request_id, final_exit, notice_text, utc


def profile_model(market: Market):
    return {"spot": FreqtradeProfileRow, "futures": FreqtradeFuturesProfileRow}[market]


def _exit_reply_values(profile, request, settings, trade, now):
    return dict(
        id=uuid4(),
        service_id=request.service_id,
        scope_kind=request.scope_kind,
        target_id=request.target_id,
        sender_id=request.sender_id,
        message_id=f"ft-exit-{profile.id.hex}-{trade['trade_id']}",
        event_time=int(trade["close_timestamp"]),
        request_sha256=fingerprint(
            {
                "profile": str(profile.id),
                "trade": trade["trade_id"],
                "closed": trade["close_timestamp"],
            }
        ),
        action="f_exit",
        mapping_version=settings.row_version,
        content=notice_text(trade),
        image=None,
        mention_id=request.sender_id if request.scope_kind == "qq_group" else None,
        quote_message=False,
        expires_at=close_time(trade) + timedelta(minutes=15),
        interval_seconds=settings.reply_interval_seconds,
        status="queued",
        created_at=now,
        updated_at=now,
    )


class FreqtradeRepository(MemeRepositoryBase):
    @staticmethod
    def read(session, service_id: UUID) -> FreqtradeRecord:
        row = session.get(FreqtradeSettingsRow, service_id)
        if row is None:
            return FreqtradeRecord()
        words = session.execute(
            select(FreqtradeWordRow.action, FreqtradeWordRow.keyword).where(
                FreqtradeWordRow.service_id == service_id
            )
        ).all()
        fields = {name: getattr(row, name) for name in FreqtradeSettings.model_fields}
        return FreqtradeRecord(
            settings=FreqtradeSettings(**fields), commands=dict(words), row_version=row.row_version,
            reply_templates=load_templates(session, "freqtrade", [service_id]).get(service_id, {})
        )

    def get(self, service_id: UUID) -> FreqtradeRecord:
        with self.sessions() as session:
            QuantSettingsRepository._service(session, service_id)
            return self.read(session, service_id)

    def save(
        self,
        service_id: UUID,
        settings: FreqtradeSettings,
        commands: dict[str, str] | None,
        *,
        expected_version: int,
        operation_id: str,
    ) -> FreqtradeRecord:
        key = self.mutation_key("freqtrade_settings", service_id, operation_id)
        digest = fingerprint(
            {
                "settings": settings.model_dump(mode="json"),
                "commands": commands,
                "expected": expected_version,
            }
        )
        with self.sessions.begin() as session:
            self.lock(session)
            replay = self.replay(session, key, digest)
            if replay is not None:
                return FreqtradeRecord.model_validate(replay)
            QuantSettingsRepository._service(session, service_id)
            previous = self.read(session, service_id)
            if previous.row_version != expected_version:
                raise ValueError("freqtrade_settings_conflict")
            commands = {**previous.commands, **(commands or {})}
            if settings.futures_enabled:
                defaults = (
                    DEFAULT_COMMANDS
                    if set(CAPITAL_COMMANDS) <= set(commands)
                    else {
                        key: value
                        for key, value in DEFAULT_COMMANDS.items()
                        if key not in CAPITAL_COMMANDS
                    }
                )
                commands = {**defaults, **commands}
            candidate_words = validate_words(commands, set())
            reserved = set(
                session.scalars(
                    select(MemeAliasRow.normalized_alias).where(
                        MemeAliasRow.normalized_alias.in_(list(candidate_words.values()))
                    )
                ).all()
            )
            reserved.update(
                session.scalars(
                    select(QuantCommandRow.normalized_keyword).where(
                        QuantCommandRow.service_id == service_id
                    )
                ).all()
            )
            normalized = validate_words(commands, reserved)
            row = session.get(FreqtradeSettingsRow, service_id, with_for_update=True)
            if row is None:
                row = FreqtradeSettingsRow(service_id=service_id)
                session.add(row)
            for name in FreqtradeSettings.model_fields:
                setattr(row, name, getattr(settings, name))
            row.row_version = previous.row_version + 1
            session.flush()
            session.execute(
                delete(FreqtradeWordRow).where(FreqtradeWordRow.service_id == service_id)
            )
            session.add_all(
                [
                    FreqtradeWordRow(
                        service_id=service_id,
                        action=action,
                        keyword=word,
                        normalized_keyword=normalized[action],
                    )
                    for action, word in commands.items()
                ]
            )
            session.flush()
            result = self.read(session, service_id)
            self.add_mutation(
                session, key, digest, result.model_dump(mode="json"), bot_service_id=service_id
            )
            return result

    def snapshots(self, service_ids: list[UUID]) -> dict[UUID, FreqtradeRecord]:
        if not service_ids:
            return {}
        if len(service_ids) > 100:
            raise ValueError("freqtrade_connections_limit")
        with self.sessions() as session:
            rows = session.scalars(
                select(FreqtradeSettingsRow).where(FreqtradeSettingsRow.service_id.in_(service_ids))
            ).all()
            words = (
                session.execute(
                    select(FreqtradeWordRow).where(FreqtradeWordRow.service_id.in_(service_ids))
                )
                .scalars()
                .all()
            )
            mappings = {row.service_id: {} for row in rows}
            templates = load_templates(session, "freqtrade", service_ids)
            for word in words:
                mappings[word.service_id][word.action] = word.keyword
            return {
                row.service_id: FreqtradeRecord(
                    row_version=row.row_version,
                    settings=FreqtradeSettings(
                        **{n: getattr(row, n) for n in FreqtradeSettings.model_fields}
                    ),
                    commands=mappings[row.service_id],
                    reply_templates=templates.get(row.service_id, {}),
                )
                for row in rows
            }

    def enabled_services(self) -> list[UUID]:
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(FreqtradeSettingsRow.service_id)
                    .where(FreqtradeSettingsRow.enabled)
                    .limit(100)
                ).all()
            )

    def qq_connections(self) -> list[QqConnection]:
        with self.sessions() as session:
            rows = session.execute(
                select(
                    FreqtradeSettingsRow,
                    BotConfigVersionRow,
                    EncryptedSecretRow.ciphertext,
                    EncryptedSecretRow.key_id,
                )
                .join(BotServiceRow, BotServiceRow.id == FreqtradeSettingsRow.service_id)
                .join(
                    BotConfigVersionRow,
                    BotConfigVersionRow.id == BotServiceRow.applied_config_version_id,
                )
                .outerjoin(
                    EncryptedSecretRow, EncryptedSecretRow.id == BotConfigVersionRow.secret_id
                )
                .where(
                    FreqtradeSettingsRow.enabled,
                    FreqtradeSettingsRow.event_ws_url.is_not(None),
                    BotServiceRow.enabled,
                    BotServiceRow.deleted_at.is_(None),
                    BotServiceRow.kind == "onebot_gateway",
                    BotConfigVersionRow.service_id == BotServiceRow.id,
                )
                .order_by(BotServiceRow.id)
                .limit(100)
            ).all()
            return [
                QqConnection(
                    settings.service_id,
                    version.id,
                    settings.row_version,
                    settings.event_ws_url,
                    version.settings["ONEBOT_BASE_URL"],
                    5,
                    ciphertext,
                    key_id,
                )
                for settings, version, ciphertext, key_id in rows
                if isinstance(version.settings.get("ONEBOT_BASE_URL"), str)
            ]

    def keyword_conflicts(self, service_id: UUID, word: str) -> bool:
        with self.sessions() as session:
            return (
                session.get(MemeAliasRow, word) is not None
                or session.scalar(
                    select(QuantCommandRow.action)
                    .where(
                        QuantCommandRow.service_id == service_id,
                        QuantCommandRow.normalized_keyword == word,
                    )
                    .limit(1)
                )
                is not None
            )

    def profile(self, qq_id: str, market: Market = "spot"):
        table = profile_model(market)
        with self.sessions() as session:
            return session.scalar(select(table).where(table.qq_id == qq_id))

    def open(
        self, qq_id: str, service_id: UUID, settings: FreqtradeSettings, market: Market = "spot"
    ):
        table = profile_model(market)
        with self.sessions.begin() as session:
            identity = session.scalar(
                insert(table)
                .values(
                    id=uuid4(),
                    qq_id=qq_id,
                    service_id=service_id,
                    initial_cash=settings.futures_initial_cash
                    if market == "futures"
                    else settings.initial_cash,
                    state="ready",
                    native_active=False,
                    created_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(index_elements=[table.qq_id])
                .returning(table.id)
            )
            row = session.scalar(select(table).where(table.qq_id == qq_id))
            return row, identity is not None

    def active_profiles(self, limit: int = 50) -> list[FreqtradeProfileRow]:
        with self.sessions() as session:
            rows = [
                row
                for table in (FreqtradeProfileRow, FreqtradeFuturesProfileRow)
                for row in session.scalars(
                    select(table).where(table.native_active).order_by(table.created_at).limit(limit)
                ).all()
            ]
            return sorted(rows, key=lambda row: row.created_at)[:limit]

    def runtime_settings(self, identities: list[UUID]) -> dict:
        if not identities:
            return {}
        profiles = union_all(
            select(FreqtradeProfileRow.id, FreqtradeProfileRow.service_id),
            select(FreqtradeFuturesProfileRow.id, FreqtradeFuturesProfileRow.service_id),
        ).subquery()
        with self.sessions() as session:
            rows = session.execute(
                select(profiles.c.id, FreqtradeSettingsRow)
                .join(
                    FreqtradeSettingsRow,
                    FreqtradeSettingsRow.service_id == profiles.c.service_id,
                )
                .where(profiles.c.id.in_(identities))
                .limit(50)
            ).all()
            return {
                identity: FreqtradeSettings(
                    **{n: getattr(row, n) for n in FreqtradeSettings.model_fields}
                )
                for identity, row in rows
            }

    def set_profile(self, identity: UUID, *, market: Market = "spot", **fields) -> None:
        if not set(fields) <= {"native_active", "state"}:
            raise ValueError("freqtrade_profile_update_invalid")
        with self.sessions.begin() as session:
            table = profile_model(market)
            session.execute(update(table).where(table.id == identity).values(**fields))

    def total_accounts(self) -> int:
        with self.sessions() as session:
            return session.scalar(
                select(
                    select(func.count()).select_from(FreqtradeProfileRow).scalar_subquery()
                    + select(func.count()).select_from(FreqtradeFuturesProfileRow).scalar_subquery()
                )
            )

    def mark_inactive(self, identities: list[UUID]) -> None:
        if identities:
            with self.sessions.begin() as session:
                for table in (FreqtradeProfileRow, FreqtradeFuturesProfileRow):
                    session.execute(
                        update(table).where(table.id.in_(identities)).values(native_active=False)
                    )

    def enqueue_exits(self, profile, trades: list[dict], *, checkpoint=None) -> None:
        if len(trades) > 20:
            raise ValueError("freqtrade_exit_batch_invalid")
        now = datetime.now(UTC)
        candidates = {
            exit_request_id(trade): trade
            for trade in trades
            if final_exit(trade)
            and trade.get("exit_reason") in {"liquidation", "stop_loss"}
            and close_time(trade) >= utc(profile.notices_since)
            and close_time(trade) + timedelta(minutes=15) > now
        }
        candidates.pop(None, None)
        with self.sessions.begin() as session:
            contexts = (
                session.execute(
                    select(FreqtradeRequestRow, FreqtradeSettingsRow)
                    .join(
                        FreqtradeSettingsRow,
                        FreqtradeSettingsRow.service_id == FreqtradeRequestRow.service_id,
                    )
                    .join(BotServiceRow, BotServiceRow.id == FreqtradeRequestRow.service_id)
                    .where(
                        FreqtradeRequestRow.id.in_(list(candidates)),
                        FreqtradeRequestRow.sender_id == profile.qq_id,
                        FreqtradeRequestRow.market == "futures",
                        FreqtradeRequestRow.scope_kind.in_(["qq_group", "qq_private"]),
                        FreqtradeRequestRow.action.in_(["long", "short"]),
                        BotServiceRow.enabled,
                        BotServiceRow.deleted_at.is_(None),
                    )
                ).all()
                if candidates
                else []
            )
            values = []
            for request, settings in contexts:
                if not settings.enabled or not settings.futures_enabled:
                    continue
                if (
                    request.scope_kind == "qq_group" and request.target_id not in settings.groups
                ) or (request.scope_kind == "qq_private" and not settings.private_enabled):
                    continue
                trade = candidates[request.id]
                values.append(_exit_reply_values(profile, request, settings, trade, now))
            if values:
                session.execute(insert(QuantReplyRow).values(values).on_conflict_do_nothing())
            if checkpoint is not None:
                session.execute(
                    update(FreqtradeFuturesProfileRow)
                    .where(
                        FreqtradeFuturesProfileRow.id == profile.id,
                        FreqtradeFuturesProfileRow.exit_scan_at < checkpoint,
                    )
                    .values(exit_scan_at=checkpoint)
                )

    def claim(self, service_id: UUID, command: FreqtradeCommand):
        source = command.source
        values = dict(
            service_id=service_id,
            scope_kind=source.scope_kind,
            target_id=source.target_id,
            sender_id=source.sender_id,
            message_id=source.message_id,
            event_time=command.event_timestamp,
        )
        digest = fingerprint(
            {
                "action": command.action,
                "args": command.arguments,
                "version": command.settings_version,
            }
        )
        with self.sessions.begin() as session:
            inserted = session.scalar(
                insert(FreqtradeRequestRow)
                .values(
                    **values,
                    id=uuid4(),
                    action=command.action,
                    market=command.market,
                    request_sha256=digest,
                    status="processing",
                    created_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(constraint="uq_freqtrade_event")
                .returning(FreqtradeRequestRow.id)
            )
            row = session.scalar(select(FreqtradeRequestRow).filter_by(**values))
            if row.request_sha256 != digest:
                raise ValueError("freqtrade_event_conflict")
            return row, inserted is not None

    def complete(self, identity: UUID, content: str, *, unknown: bool = False) -> None:
        with self.sessions.begin() as session:
            session.execute(
                update(FreqtradeRequestRow)
                .where(FreqtradeRequestRow.id == identity)
                .values(status="unknown" if unknown else "completed", result=content)
            )

    def prepare_native(self, identity: UUID, trade: dict | None) -> None:
        with self.sessions.begin() as session:
            session.execute(
                update(FreqtradeRequestRow)
                .where(FreqtradeRequestRow.id == identity)
                .values(
                    native_trade_id=trade["trade_id"] if trade else None,
                    native_order_count=len(trade.get("orders", [])) if trade else 0,
                )
            )

    def prepare_capital(self, identity: UUID, amount, trade_id: int | None) -> None:
        with self.sessions.begin() as session:
            session.execute(
                update(FreqtradeRequestRow)
                .where(FreqtradeRequestRow.id == identity)
                .values(capital_amount=amount, native_trade_id=trade_id)
            )

    def unresolved(self, qq_id: str, market: Market = "spot") -> list[FreqtradeRequestRow]:
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(FreqtradeRequestRow)
                    .where(
                        FreqtradeRequestRow.sender_id == qq_id,
                        FreqtradeRequestRow.market == market,
                        FreqtradeRequestRow.status.in_(["processing", "unknown"]),
                        FreqtradeRequestRow.action.in_(list(WRITE_ACTIONS)),
                    )
                    .order_by(FreqtradeRequestRow.created_at.desc())
                    .limit(20)
                ).all()
            )

    def blocking_request(self, qq_id: str, request_id: UUID, market: Market = "futures") -> bool:
        with self.sessions() as session:
            return (
                session.scalar(
                    select(FreqtradeRequestRow.id)
                    .where(
                        FreqtradeRequestRow.sender_id == qq_id,
                        FreqtradeRequestRow.market == market,
                        FreqtradeRequestRow.id != request_id,
                        FreqtradeRequestRow.status.in_(["processing", "unknown"]),
                        FreqtradeRequestRow.action.in_(list(WRITE_ACTIONS)),
                    )
                    .limit(1)
                )
                is not None
            )

    def finish_recovery(
        self,
        profile_id: UUID,
        request_ids: list[UUID],
        *,
        all_recovered: bool,
        market: Market = "spot",
    ):
        with self.sessions.begin() as session:
            if request_ids:
                session.execute(
                    update(FreqtradeRequestRow)
                    .where(
                        FreqtradeRequestRow.id.in_(request_ids),
                        FreqtradeRequestRow.market == market,
                    )
                    .values(status="completed", result="已核对原生操作凭据，未重复执行。")
                )
            if all_recovered:
                table = profile_model(market)
                session.execute(update(table).where(table.id == profile_id).values(state="ready"))

    def recover(self) -> None:
        with self.sessions.begin() as session:
            for market in ("spot", "futures"):
                table = profile_model(market)
                users = select(FreqtradeRequestRow.sender_id).where(
                    FreqtradeRequestRow.status.in_(["processing", "unknown"]),
                    FreqtradeRequestRow.market == market,
                    FreqtradeRequestRow.action.in_(list(WRITE_ACTIONS)),
                )
                session.execute(update(table).where(table.qq_id.in_(users)).values(state="unknown"))
            session.execute(
                update(FreqtradeRequestRow)
                .where(FreqtradeRequestRow.status == "processing")
                .values(
                    status="unknown",
                    result="请求执行中断，结果待核实，请查询对应市场的成交记录，勿重复下单。",
                )
            )
