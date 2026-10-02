from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from al1s.adapters.postgres.maa_script_version_repository import (
    PostgresMaaScriptVersionRepository,
)
from al1s.api.maa_catalog_scripts import list_script_versions
from al1s.maa.catalog_service import MaaCatalogQueryService


def version(revision: int) -> SimpleNamespace:
    return SimpleNamespace(
        script_version_id=uuid4(),
        revision=revision,
        schema_version=2,
        manifest_hash="0" * 64,
        created_at=datetime.now(UTC),
    )


def request_for(service: Mock) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(maa_catalog=service)))


def test_script_versions_endpoint_defaults_to_ascending_and_preserves_cursor() -> None:
    service = Mock()
    script_id = uuid4()
    service.list_script_versions.return_value = [version(4)]

    result = list_script_versions(script_id, request_for(service), after_revision=2, limit=1)

    service.list_script_versions.assert_called_once_with(
        script_id=script_id,
        after_revision=2,
        limit=1,
        newest_first=False,
    )
    assert result.next_after_revision == 4


def test_script_versions_endpoint_propagates_newest_first() -> None:
    service = Mock()
    script_id = uuid4()
    service.list_script_versions.return_value = [version(4)]

    list_script_versions(
        script_id,
        request_for(service),
        after_revision=8,
        limit=1,
        newest_first=True,
    )

    service.list_script_versions.assert_called_once_with(
        script_id=script_id,
        after_revision=8,
        limit=1,
        newest_first=True,
    )


def test_catalog_service_omits_new_repository_flag_for_default_calls() -> None:
    script_id = uuid4()
    uow = SimpleNamespace(scripts=Mock(), script_versions=Mock())
    uow.scripts.get_active.return_value = object()
    uow.script_versions.list_for_script.return_value = [version(3)]

    result = MaaCatalogQueryService(lambda: nullcontext(uow)).list_script_versions(
        script_id=script_id,
        after_revision=1,
        limit=7,
    )

    assert result == uow.script_versions.list_for_script.return_value
    uow.scripts.get_active.assert_called_once_with(script_id)
    uow.script_versions.list_for_script.assert_called_once_with(
        script_id,
        after_revision=1,
        limit=7,
    )


def test_catalog_service_propagates_newest_first_to_repository() -> None:
    script_id = uuid4()
    uow = SimpleNamespace(scripts=Mock(), script_versions=Mock())
    uow.scripts.get_active.return_value = object()
    uow.script_versions.list_for_script.return_value = [version(3)]

    MaaCatalogQueryService(lambda: nullcontext(uow)).list_script_versions(
        script_id=script_id,
        after_revision=8,
        limit=7,
        newest_first=True,
    )

    uow.scripts.get_active.assert_called_once_with(script_id)
    uow.script_versions.list_for_script.assert_called_once_with(
        script_id,
        after_revision=8,
        limit=7,
        newest_first=True,
    )


def test_repository_uses_ascending_keyset_query_by_default() -> None:
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = []
    script_id, after_revision = uuid4(), 7

    result = PostgresMaaScriptVersionRepository(session).list_for_script(
        script_id,
        after_revision=after_revision,
        limit=37,
    )

    assert result == []
    statement = session.execute.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "maa_script_versions.script_id =" in sql
    assert "maa_script_versions.revision >" in sql
    assert "ORDER BY maa_script_versions.revision" in sql
    assert "DESC" not in sql
    assert "LIMIT" in sql
    assert script_id in compiled.params.values()
    assert after_revision in compiled.params.values()
    assert 37 in compiled.params.values()


def test_repository_uses_descending_keyset_query_when_newest_first() -> None:
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = []
    script_id, after_revision = uuid4(), 7

    result = PostgresMaaScriptVersionRepository(session).list_for_script(
        script_id,
        after_revision=after_revision,
        limit=37,
        newest_first=True,
    )

    assert result == []
    statement = session.execute.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "maa_script_versions.script_id =" in sql
    assert "maa_script_versions.revision <" in sql
    assert "ORDER BY maa_script_versions.revision DESC" in sql
    assert "LIMIT" in sql
    assert script_id in compiled.params.values()
    assert after_revision in compiled.params.values()
    assert 37 in compiled.params.values()
