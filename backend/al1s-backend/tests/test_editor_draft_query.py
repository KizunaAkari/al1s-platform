"""Structural checks for the bounded, server-side editor draft query."""

from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from al1s.adapters.postgres.maa_repositories import PostgresMaaScriptRepository


def test_editor_drafts_use_one_parameterized_query_with_stable_cursor() -> None:
    session = MagicMock()
    session.scalars.return_value = []
    device_id, after_id = uuid4(), uuid4()

    result = PostgresMaaScriptRepository(session).list_editor_drafts(
        device_id=device_id, after_id=after_id, limit=37,
    )

    assert result == []
    session.scalars.assert_called_once()
    statement = session.scalars.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "JOIN maa_application_devices" in sql
    assert "JOIN maa_applications" in sql
    assert "maa_application_devices.device_id =" in sql
    assert "maa_scripts.current_version_id IS NULL" in sql
    assert "maa_scripts.candidate_version_id IS NULL" in sql
    assert "maa_scripts.id >" in sql
    assert "ORDER BY maa_scripts.id" in sql
    assert device_id in compiled.params.values()
    assert after_id in compiled.params.values()
    assert 37 in compiled.params.values()
