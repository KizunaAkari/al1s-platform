from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from al1s.adapters.postgres.freqtrade_models import FreqtradeSettingsRow, FreqtradeWordRow
from al1s.adapters.postgres.freqtrade_repository import FreqtradeRepository
from al1s.adapters.postgres.meme_base import MemeRepositoryBase, fingerprint
from al1s.adapters.postgres.meme_models import MemeAliasRow
from al1s.adapters.postgres.quant_models import QuantCommandRow, QuantScopeRow, QuantSettingsRow
from al1s.adapters.postgres.quant_settings import QuantSettingsRecord, QuantSettingsRepository
from al1s.adapters.postgres.trading_reply_models import TradingReplyTemplateRow
from al1s.freqtrade.commands import validate_words
from al1s.freqtrade.contracts import FreqtradeRecord
from al1s.quant.commands import validate_mappings
from al1s.trading_replies.catalog import words
from al1s.trading_replies.render import validate_templates


class TradingReplyRepository(MemeRepositoryBase):
    @staticmethod
    def read(session, service_id, engine):
        QuantSettingsRepository._service(session, service_id)
        if engine == "quant":
            return QuantSettingsRepository._read(session, service_id)
        if engine == "freqtrade":
            return FreqtradeRepository.read(session, service_id)
        raise ValueError("reply_engine_invalid")

    def get(self, service_id, engine):
        with self.sessions() as session:
            return self.read(session, service_id, engine)

    @staticmethod
    def _initialize(session, service_id, engine, previous):
        model = QuantSettingsRow if engine == "quant" else FreqtradeSettingsRow
        row = session.get(model, service_id, with_for_update=True)
        fresh = row is None
        if fresh:
            values = previous.settings.model_dump()
            if engine == "quant":
                groups = values.pop("groups")
                values["quote_max_age_seconds"] = values.pop("max_quote_age_seconds")
                values["mapping_version"] = 1
                session.add_all([QuantScopeRow(service_id=service_id, group_id=g) for g in groups])
            row = model(service_id=service_id, row_version=0, **values)
            session.add(row)
        return row, fresh

    def save(
        self, service_id, engine, action, keyword, templates, *, expected_version, operation_id
    ):
        templates = validate_templates(engine, action, templates)
        key = self.mutation_key("trading_command", service_id, operation_id)
        digest = fingerprint(
            dict(
                engine=engine,
                action=action,
                keyword=keyword,
                templates=templates,
                expected=expected_version,
            )
        )
        with self.sessions.begin() as session:
            self.lock(session)
            QuantSettingsRepository._service(session, service_id)
            replay = self.replay(session, key, digest)
            if replay is not None:
                model = QuantSettingsRecord if engine == "quant" else FreqtradeRecord
                return model.model_validate(replay)
            previous = self.read(session, service_id, engine)
            if previous.row_version != expected_version:
                raise ValueError("reply_settings_conflict")
            mappings = {**words(engine), **previous.commands, action: keyword}
            other = FreqtradeWordRow if engine == "quant" else QuantCommandRow
            candidates = (validate_mappings if engine == "quant" else validate_words)(
                mappings, set()
            )
            aliases = set(
                session.scalars(
                    select(MemeAliasRow.normalized_alias).where(
                        MemeAliasRow.normalized_alias.in_(list(candidates.values()))
                    )
                ).all()
            )
            aliases.update(
                session.scalars(
                    select(other.normalized_keyword).where(other.service_id == service_id)
                ).all()
            )
            normalized = (validate_mappings if engine == "quant" else validate_words)(
                mappings, aliases
            )
            row, fresh = self._initialize(session, service_id, engine, previous)
            row.row_version += 1
            if engine == "quant" and not fresh and previous.commands.get(action) != keyword:
                row.mapping_version += 1
            session.flush()
            word_model = QuantCommandRow if engine == "quant" else FreqtradeWordRow
            targets = (
                mappings
                if fresh
                else {
                    action: keyword,
                    **{
                        name: value
                        for name, value in mappings.items()
                        if name not in previous.commands
                    },
                }
            )
            statement = insert(word_model).values(
                [
                    dict(
                        service_id=service_id,
                        action=target,
                        keyword=value,
                        normalized_keyword=normalized[target],
                    )
                    for target, value in targets.items()
                ]
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[word_model.service_id, word_model.action],
                    set_={
                        "keyword": statement.excluded.keyword,
                        "normalized_keyword": statement.excluded.normalized_keyword,
                    },
                )
            )
            session.execute(
                delete(TradingReplyTemplateRow).where(
                    TradingReplyTemplateRow.service_id == service_id,
                    TradingReplyTemplateRow.engine == engine,
                    TradingReplyTemplateRow.action == action,
                )
            )
            if templates:
                session.execute(
                    insert(TradingReplyTemplateRow),
                    [
                        dict(
                            service_id=service_id,
                            engine=engine,
                            action=action,
                            variant=variant,
                            template=body,
                        )
                        for variant, body in templates.items()
                    ],
                )
            session.flush()
            result = self.read(session, service_id, engine)
            self.add_mutation(
                session, key, digest, result.model_dump(mode="json"), bot_service_id=service_id
            )
            return result
