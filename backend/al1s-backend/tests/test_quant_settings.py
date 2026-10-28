from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from al1s.adapters.postgres.bot_models import BotConfigVersionRow, BotServiceRow
from al1s.adapters.postgres.meme_models import MemeAliasRow, MemeSettingsRow
from al1s.adapters.postgres.quant_models import QuantCommandRow, QuantScopeRow, QuantSettingsRow
from al1s.adapters.postgres.quant_settings import QuantSettingsRepository
from al1s.adapters.postgres.notification_models import EncryptedSecretRow
from al1s.quant.commands import DEFAULT_COMMANDS
from al1s.quant.contracts import QuantSettings


@compiles(JSONB, "sqlite")
def sqlite_jsonb(_type, _compiler, **_kw):
    return "JSON"


@pytest.fixture
def configured():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for table in (
        BotServiceRow.__table__,
        BotConfigVersionRow.__table__,
        EncryptedSecretRow.__table__,
        MemeAliasRow.__table__,
        MemeSettingsRow.__table__,
        QuantSettingsRow.__table__,
        QuantScopeRow.__table__,
        QuantCommandRow.__table__,
    ):
        table.create(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE meme_mutations (key VARCHAR(64) PRIMARY KEY, "
                "request_sha256 VARCHAR(64) NOT NULL, template_id CHAR(32), "
                "bot_service_id CHAR(32), version_id CHAR(32), result JSON NOT NULL, "
                "expires_at DATETIME NOT NULL)"
            )
        )
    sessions = sessionmaker(engine, expire_on_commit=False)
    service_id = uuid4()
    with sessions.begin() as session:
        session.add(
            BotServiceRow(
                id=service_id, kind="onebot_gateway", name="QQ", enabled=True, row_version=1
            )
        )
    repository = QuantSettingsRepository(sessions)
    repository.lock = lambda _session: None
    return repository, service_id


def test_settings_have_cas_and_idempotent_replay(configured):
    repo, service = configured
    saved = repo.save(
        service,
        QuantSettings(enabled=True, event_ws_url="ws://llbot:3001", groups=["12345"]),
        DEFAULT_COMMANDS,
        expected_version=0,
        operation_id="request-1",
    )
    assert saved.row_version == 1 and saved.mapping_version == 1
    replay = repo.save(
        service, saved.settings, DEFAULT_COMMANDS, expected_version=0, operation_id="request-1"
    )
    assert replay == saved
    with pytest.raises(ValueError, match="idempotency_conflict"):
        repo.save(
            service, QuantSettings(), DEFAULT_COMMANDS, expected_version=0, operation_id="request-1"
        )
    with pytest.raises(ValueError, match="settings_conflict"):
        repo.save(
            service, saved.settings, DEFAULT_COMMANDS, expected_version=0, operation_id="different"
        )
    updated = repo.save(
        service,
        saved.settings,
        {**DEFAULT_COMMANDS, "buy": "买入"},
        expected_version=1,
        operation_id="request-2",
    )
    assert updated.mapping_version == 2 and updated.commands["buy"] == "买入"


def test_meme_keyword_conflict_cannot_be_saved(configured):
    repo, service = configured
    with repo.sessions.begin() as session:
        session.add(MemeAliasRow(normalized_alias="摸摸", template_id=uuid4(), is_primary=True))
    with pytest.raises(ValueError, match="quant_keyword_conflict"):
        repo.save(
            service,
            QuantSettings(),
            {**DEFAULT_COMMANDS, "buy": "摸摸"},
            expected_version=0,
            operation_id="collision",
        )


def test_quant_only_connection_is_discovered_and_saved_alias_is_checked(configured):
    repo, service = configured
    config_id = uuid4()
    with repo.sessions.begin() as session:
        session.add(BotConfigVersionRow(id=config_id, service_id=service, version_no=1,
                                        settings={"ONEBOT_BASE_URL": "http://llbot:3000"},
                                        config_hash="a" * 64))
        session.get(BotServiceRow, service).applied_config_version_id = config_id
        session.add(MemeAliasRow(normalized_alias="摸摸", template_id=uuid4(), is_primary=True))
    repo.save(service, QuantSettings(enabled=True, event_ws_url="ws://llbot:3001"), DEFAULT_COMMANDS,
              expected_version=0, operation_id="start")
    connections = repo.qq_connections()
    assert len(connections) == 1
    assert connections[0].service_id == service and connections[0].event_url == "ws://llbot:3001"
    assert repo.keyword_conflicts("摸摸") and not repo.keyword_conflicts("模拟买入")
    snapshots = repo.snapshots([service])
    assert snapshots[service].commands == DEFAULT_COMMANDS and snapshots[service].settings.enabled


def test_new_quant_settings_reuse_existing_qq_event_endpoint(configured):
    repo, service = configured
    with repo.sessions.begin() as session:
        session.add(MemeSettingsRow(bot_service_id=service, enabled=False, private_enabled=False,
                                    qq_event_ws_url="ws://llbot:3001", cooldown_seconds=5, row_version=1))
    assert repo.get(service).settings.event_ws_url == "ws://llbot:3001"


def test_provider_order_survives_save_and_read(configured):
    repo, service = configured
    order = ["tencent", "sina", "eastmoney", "gugudata", "tushare"]
    settings = QuantSettings(provider="tencent", provider_order=order)
    repo.save(service, settings, DEFAULT_COMMANDS, expected_version=0, operation_id="priority")
    assert repo.get(service).settings.provider_order == order
