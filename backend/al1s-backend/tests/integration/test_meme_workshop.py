import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, delete, event, select, update
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.blob_references import no_blob_references
from al1s.adapters.postgres.bot_models import BotServiceRow
from al1s.adapters.postgres.meme_models import (
    MemeAliasRow,
    MemeAssetRow,
    MemeMutationRow,
    MemeSendGateRow,
    MemeTemplateRow,
    MemeVersionRow,
)
from al1s.adapters.postgres.meme_repository import MemeRepository
from al1s.adapters.postgres.models import BlobObjectRow, GcJobRow
from al1s.meme_workshop.assets import MemeAssets
from al1s.meme_workshop.contracts import (
    AvatarSlot,
    CommandSource,
    ConnectionSettings,
    Layout,
    Scope,
    TemplateWrite,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def repository() -> Iterator[tuple[MemeRepository, Engine]]:
    url = os.environ.get("AL1S_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated PostgreSQL required")
    engine = create_engine(url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    repository = MemeRepository(sessions)
    repository.ensure_builtins()
    with sessions.begin() as session:
        session.execute(
            update(MemeSendGateRow).values(
                available_at=datetime.now(UTC) - timedelta(days=1),
                owner_receipt_id=None,
            )
        )
    yield repository, engine
    engine.dispose()


def template(keyword: str) -> TemplateWrite:
    return TemplateWrite(name=keyword, keyword=keyword, layout=Layout())


def test_save_replay_conflict_and_alias_unique_are_atomic(repository) -> None:
    catalog, _ = repository
    identity, operation = uuid4(), str(uuid4())
    data = template(f"test-{identity.hex[:16]}")
    saved = catalog.save(identity, data, expected_version=0, operation_id=operation)
    assert saved.row_version == 1
    assert catalog.save(identity, data, expected_version=0, operation_id=operation) == saved
    with pytest.raises(ValueError, match="idempotency"):
        catalog.save(identity, template("changed"), expected_version=0, operation_id=operation)
    with pytest.raises(ValueError, match="conflict"):
        catalog.save(identity, data, expected_version=0, operation_id=str(uuid4()))
    with pytest.raises(ValueError, match="keyword"):
        catalog.save(uuid4(), data, expected_version=0, operation_id=str(uuid4()))
    catalog.delete(identity, expected_version=1, operation_id=str(uuid4()))
    assert catalog.find_keyword(data.keyword) is None


def test_configuration_and_original_message_allow_only_one_send(repository) -> None:
    catalog, _engine = repository
    bot_id = uuid4()
    with catalog.sessions.begin() as session:
        session.add(BotServiceRow(id=bot_id, name=str(bot_id), kind="onebot_gateway"))
    catalog.save_settings(
        bot_id,
        ConnectionSettings(
            enabled=True,
            scopes=[Scope(kind="qq_group", target_id="12345")],
        ),
        expected_version=0,
        operation_id=str(uuid4()),
    )
    source = CommandSource(
        message_id=str(uuid4()), scope_kind="qq_group", target_id="12345", sender_id="45678"
    )
    assert catalog.allowed(bot_id, source)
    assert not catalog.allowed(bot_id, source.model_copy(update={"target_id": "99999"}))
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: catalog.claim(bot_id, source), range(4)))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    receipt = winners[0]
    assert catalog.start_send(bot_id, receipt)
    assert not catalog.start_send(bot_id, receipt)
    catalog.settle(bot_id, receipt, "unknown")
    assert catalog.claim(bot_id, source) is None


def test_catalog_pagination_performs_a_fixed_number_of_queries(repository) -> None:
    catalog, engine = repository
    queries: list[str] = []

    def count(connection, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        rows = catalog.list_templates(limit=50)
        assert len(rows) >= 3
        assert len(queries) <= 3
        assert all(row.layout.schema_version == 1 for row in rows)
    finally:
        event.remove(engine, "before_cursor_execute", count)


def test_three_hundred_templates_keep_reads_paginated_and_batched(repository) -> None:
    catalog, engine = repository
    marker = uuid4().hex[:8]
    now = datetime.now(UTC)
    identities = [uuid4() for _ in range(300)]
    with catalog.sessions.begin() as session:
        session.add_all(
            MemeTemplateRow(
                id=identity,
                name=f"bulk-{index}-{marker}",
                engine_kind="composite",
                enabled=True,
                random_enabled=False,
                builtin=False,
                current_version_id=None,
                row_version=1,
                created_at=now,
                updated_at=now,
            )
            for index, identity in enumerate(identities)
        )
        session.flush()
        session.add_all(
            MemeVersionRow(
                id=identity,
                template_id=identity,
                version_no=1,
                schema_version=1,
                min_avatar_count=0,
                min_image_count=0,
                min_text_count=0,
                layout=Layout().model_dump(mode="json"),
                created_at=now,
            )
            for identity in identities
        )
        session.flush()
        session.add_all(
            MemeAliasRow(
                template_id=identity, normalized_alias=f"b{index}-{marker}", is_primary=True
            )
            for index, identity in enumerate(identities)
        )
        session.execute(
            update(MemeTemplateRow)
            .where(MemeTemplateRow.id.in_(identities))
            .values(current_version_id=MemeTemplateRow.id)
        )
    statements = []

    def count(*args):
        statements.append(args[2])

    event.listen(engine, "before_cursor_execute", count)
    try:
        first = catalog.list_templates(limit=50)
        assert len(first) == 50 and len(statements) == 3
        statements.clear()
        second = catalog.list_templates(after_id=first[-1].id, limit=50)
        assert len(second) == 50 and len(statements) == 3
        assert not set(row.id for row in first) & set(row.id for row in second)
    finally:
        event.remove(engine, "before_cursor_execute", count)
        with catalog.sessions.begin() as session:
            session.execute(
                update(MemeTemplateRow)
                .where(MemeTemplateRow.id.in_(identities))
                .values(current_version_id=None)
            )
            session.execute(delete(MemeAliasRow).where(MemeAliasRow.template_id.in_(identities)))
            session.execute(
                delete(MemeVersionRow).where(MemeVersionRow.template_id.in_(identities))
            )
            session.execute(delete(MemeTemplateRow).where(MemeTemplateRow.id.in_(identities)))


def test_cleanup_is_bounded_and_does_not_delete_live_templates(repository) -> None:
    catalog, _ = repository
    catalog.cleanup(datetime.now(UTC), limit=10)
    assert catalog.find_keyword("摸摸") is not None


def test_deleted_template_metadata_is_kept_for_replay_then_cleaned_after_references_expire(
    repository,
):
    catalog, _ = repository
    identity = uuid4()
    data = template(identity.hex[:16])
    catalog.save(identity, data, expected_version=0, operation_id=str(uuid4()))
    catalog.delete(identity, expected_version=1, operation_id=str(uuid4()))
    now = datetime.now(UTC)
    catalog.cleanup(now, limit=10)
    with catalog.sessions() as session:
        assert session.get(MemeTemplateRow, identity) is not None
    old = datetime(2000, 1, 1, tzinfo=UTC)
    with catalog.sessions.begin() as session:
        session.execute(
            update(MemeMutationRow)
            .where(MemeMutationRow.template_id == identity)
            .values(expires_at=old)
        )
        session.execute(
            update(MemeVersionRow)
            .where(MemeVersionRow.template_id == identity)
            .values(created_at=old)
        )
        session.execute(
            update(MemeTemplateRow).where(MemeTemplateRow.id == identity).values(deleted_at=old)
        )
    catalog.cleanup(now, limit=10)
    with catalog.sessions() as session:
        assert session.get(MemeTemplateRow, identity) is None


def test_disabling_a_bound_template_or_connection_prevents_late_send_without_taking_quota(
    repository,
):
    catalog, _ = repository
    bot_id, template_id = uuid4(), uuid4()
    with catalog.sessions.begin() as session:
        session.add(BotServiceRow(id=bot_id, name=str(bot_id), kind="onebot_gateway"))
    enabled = ConnectionSettings(enabled=True, private_enabled=True)
    catalog.save_settings(bot_id, enabled, expected_version=0, operation_id=str(uuid4()))
    data = template(template_id.hex[:16])
    saved = catalog.save(template_id, data, expected_version=0, operation_id=str(uuid4()))
    source = CommandSource(
        message_id=str(uuid4()), scope_kind="qq_private", target_id="200", sender_id="200"
    )
    receipt = catalog.claim(bot_id, source)
    assert receipt and catalog.bind_template(bot_id, receipt, saved.version_id)
    catalog.save(
        template_id,
        data.model_copy(update={"enabled": False}),
        expected_version=1,
        operation_id=str(uuid4()),
    )
    assert not catalog.start_send(bot_id, receipt)
    receipt = catalog.claim(bot_id, source.model_copy(update={"message_id": str(uuid4())}))
    assert receipt
    catalog.save_settings(
        bot_id,
        enabled.model_copy(update={"enabled": False}),
        expected_version=1,
        operation_id=str(uuid4()),
    )
    assert not catalog.start_send(bot_id, receipt)
    with catalog.sessions() as session:
        gate = session.get(MemeSendGateRow, "qq")
        assert gate and gate.available_at < datetime.now(UTC)


def test_random_candidates_filter_enabled_participation_and_typed_input_requirements(repository):
    catalog, _ = repository
    variants = [
        dict(layout=Layout(engine="petpet", width=128, height=128)),
        dict(layout=Layout(engine="caption")),
        dict(enabled=False),
        dict(random_enabled=False),
        dict(layout=Layout(avatars=[AvatarSlot(), AvatarSlot()])),
    ]
    ids = [uuid4() for _ in variants]
    records = [
        catalog.save(
            identity,
            TemplateWrite(name="随机过滤", keyword=identity.hex[:16], **values),
            expected_version=0,
            operation_id=str(uuid4()),
        )
        for identity, values in zip(ids, variants, strict=True)
    ]
    try:
        supplied = catalog.random_candidates(1, 0, 0)
        assert {row.id for row in supplied} & set(ids) == {ids[0]}
        supplied = catalog.random_candidates(1, 1, 1)
        assert {row.id for row in supplied} & set(ids) == set(ids[:2])
        assert all(
            all(
                required <= actual
                for required, actual in zip(
                    row.layout.requirements(),
                    (1, 1, 1),
                    strict=True,
                )
            )
            for row in supplied
        )
    finally:
        for row in records:
            catalog.delete(row.id, expected_version=1, operation_id=str(uuid4()))


def test_image_deduplication_and_shared_reference_survive_one_template_deletion(repository) -> None:
    from io import BytesIO
    from types import SimpleNamespace

    from PIL import Image

    catalog, _ = repository

    class Store:
        def __init__(self):
            self.data = {}

        def put(self, key, body, kind):
            self.data[key] = (body, kind)

        def head(self, key):
            return SimpleNamespace(size_bytes=len(self.data[key][0]), media_type=self.data[key][1])

        def get_range(self, key, start, end):
            return self.data[key][0][start : end + 1]

    store = Store()
    assets = MemeAssets(catalog, store)
    output = BytesIO()
    Image.new("RGBA", (32, 32), "#" + uuid4().hex[:6]).save(output, "PNG")
    first = assets.upload(output.getvalue())
    second = assets.upload(output.getvalue())
    assert first.blob_id == second.blob_id and len(store.data) == 1
    records = []
    for _ in range(2):
        identity = uuid4()
        records.append(
            catalog.save(
                identity,
                TemplateWrite(
                    name="共享素材", keyword=identity.hex[:16], background_blob_id=first.blob_id
                ),
                expected_version=0,
                operation_id=str(uuid4()),
            )
        )
    catalog.delete(records[0].id, expected_version=1, operation_id=str(uuid4()))
    future = datetime.now(UTC) + timedelta(days=2)
    catalog.cleanup(future)
    catalog.cleanup_assets(future)
    with catalog.sessions() as session:
        assert session.get(MemeAssetRow, first.blob_id) is not None
        assert (
            session.scalar(
                select(BlobObjectRow.id).where(
                    BlobObjectRow.id == first.blob_id, *no_blob_references()
                )
            )
            is None
        )
    assert assets.read(first.blob_id) == output.getvalue()


def test_global_send_limit_cannot_be_bypassed_by_a_different_bot_user_or_group(repository) -> None:
    catalog, _ = repository
    bot_ids = [uuid4(), uuid4()]
    for bot_id in bot_ids:
        with catalog.sessions.begin() as session:
            session.add(BotServiceRow(id=bot_id, name=str(bot_id), kind="onebot_gateway"))
        catalog.save_settings(
            bot_id,
            ConnectionSettings(enabled=True, scopes=[Scope(kind="qq_group", target_id="300")]),
            expected_version=0,
            operation_id=str(uuid4()),
        )
    sources = [
        CommandSource(
            message_id=str(uuid4()),
            scope_kind="qq_group",
            target_id="300",
            sender_id=str(200 + index),
        )
        for index in range(2)
    ]
    receipts = [
        catalog.claim(bot_id, source) for bot_id, source in zip(bot_ids, sources, strict=True)
    ]
    assert all(receipts)
    assert catalog.start_send(bot_ids[0], receipts[0])
    catalog.settle(bot_ids[0], receipts[0], "sent")
    assert not catalog.start_send(bot_ids[1], receipts[1])
    restarted = MemeRepository(catalog.sessions)
    assert (
        restarted.claim(bot_ids[1], sources[1].model_copy(update={"message_id": str(uuid4())}))
        is None
    )
    with catalog.sessions() as session:
        gate = session.get(MemeSendGateRow, "qq")
        assert gate is not None
        assert (gate.available_at - datetime.now(UTC)).total_seconds() >= 29


def test_discord_ten_second_global_interval_is_independent_of_qq(repository) -> None:
    catalog, _ = repository
    bot_id = uuid4()
    with catalog.sessions.begin() as session:
        session.add(BotServiceRow(id=bot_id, name=str(bot_id), kind="discord_bridge"))
    catalog.save_settings(
        bot_id,
        ConnectionSettings(enabled=True, private_enabled=True),
        expected_version=0,
        operation_id=str(uuid4()),
    )
    source = CommandSource(
        message_id=str(uuid4()), scope_kind="discord_private", target_id="100", sender_id="200"
    )
    receipt = catalog.claim(bot_id, source)
    assert receipt and catalog.start_send(bot_id, receipt)
    catalog.settle(bot_id, receipt, "sent")
    with catalog.sessions() as session:
        gate = session.get(MemeSendGateRow, "discord")
        assert gate and (gate.available_at - datetime.now(UTC)).total_seconds() >= 9
        qq = session.get(MemeSendGateRow, "qq")
        assert qq and qq.available_at < datetime.now(UTC)


def test_concurrent_different_messages_share_one_send_gate_and_error_replies_consume_it(repository):
    catalog, _ = repository
    bot_id = uuid4()
    with catalog.sessions.begin() as session:
        session.add(BotServiceRow(id=bot_id, name=str(bot_id), kind="onebot_gateway"))
    catalog.save_settings(
        bot_id,
        ConnectionSettings(enabled=True, private_enabled=True),
        expected_version=0,
        operation_id=str(uuid4()),
    )
    receipts = [
        catalog.claim(
            bot_id,
            CommandSource(
                message_id=str(uuid4()),
                scope_kind="qq_private",
                target_id=str(100 + index),
                sender_id=str(100 + index),
            ),
        )
        for index in range(8)
    ]
    assert all(receipts)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(
            pool.map(
                lambda receipt: catalog.start_send(bot_id, receipt, error_code="meme_bad_input"),
                receipts,
            )
        )
    assert sum(outcomes) == 1
    receipt = receipts[outcomes.index(True)]
    catalog.settle(bot_id, receipt, "unknown", "meme_bad_input")
    assert (
        catalog.claim(
            bot_id,
            CommandSource(
                message_id=str(uuid4()),
                scope_kind="qq_private",
                target_id="200",
                sender_id="200",
            ),
        )
        is None
    )


def test_expired_staging_reschedules_cleanup_after_a_previous_protected_job(repository) -> None:
    catalog, _ = repository
    blob_id, old_job = uuid4(), uuid4()
    now = datetime.now(UTC)
    import hashlib

    with catalog.sessions.begin() as session:
        session.add(
            BlobObjectRow(
                id=blob_id,
                sha256=hashlib.sha256(str(blob_id).encode()).hexdigest(),
                size_bytes=10,
                media_type="image/png",
                object_key=f"memes/{blob_id}",
                status="ready",
                row_version=1,
                ready_at=now,
            )
        )
        session.flush()
        session.add(
            MemeAssetRow(blob_id=blob_id, width=32, height=32, expires_at=now - timedelta(days=1))
        )
        session.add(
            GcJobRow(
                id=old_job,
                blob_id=blob_id,
                status="dead_letter",
                attempt_count=10,
                available_at=now,
                row_version=1,
            )
        )
    catalog.cleanup_assets(now)
    with catalog.sessions() as session:
        assert session.get(MemeAssetRow, blob_id) is None
        assert (
            session.scalar(
                select(GcJobRow.id).where(GcJobRow.blob_id == blob_id, GcJobRow.status == "pending")
            )
            is not None
        )


def test_templates_cannot_reference_images_owned_only_by_another_module(repository) -> None:
    catalog, _ = repository
    blob_id, identity = uuid4(), uuid4()
    import hashlib

    with catalog.sessions.begin() as session:
        session.add(
            BlobObjectRow(
                id=blob_id,
                sha256=hashlib.sha256(str(blob_id).encode()).hexdigest(),
                size_bytes=100,
                media_type="image/png",
                object_key=f"maa/{blob_id}.png",
                status="ready",
                row_version=1,
                ready_at=datetime.now(UTC),
            )
        )
    with pytest.raises(ValueError, match="asset"):
        catalog.save(
            identity,
            TemplateWrite(
                name="跨模块拒绝",
                keyword=f"owner-{identity.hex[:12]}",
                background_blob_id=blob_id,
            ),
            expected_version=0,
            operation_id=str(uuid4()),
        )
