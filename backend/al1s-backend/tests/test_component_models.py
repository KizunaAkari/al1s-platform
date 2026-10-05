from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from al1s.adapters.postgres.component_models import ComponentOperationRow, ComponentReleaseRow


def test_component_data_has_typed_keys_constraints_and_foreign_keys():
    release = ComponentReleaseRow.__table__
    operation = ComponentOperationRow.__table__
    assert {"component", "version", "architecture", "state", "blob_id"} <= set(release.c.keys())
    assert {
        "release_id",
        "scope",
        "terminal_id",
        "idempotency_key",
        "state",
        "expected_source",
    } <= set(operation.c.keys())
    assert any(index.name == "ix_component_release_page" for index in release.indexes)
    assert any(index.name == "uq_component_operation_active_scope" for index in operation.indexes)
    ddl = str(CreateTable(operation).compile(dialect=postgresql.dialect()))
    assert "FOREIGN KEY" in ddl and "terminal_id" in ddl
