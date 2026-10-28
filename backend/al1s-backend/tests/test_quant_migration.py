import importlib
from io import StringIO

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_quant_migration_adds_only_new_tables_and_preserves_upgrade_chain():
    migration = importlib.import_module("migrations.versions.20261027_0058_qq_quant")
    assert migration.down_revision == "20261013_0057"
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        migration.upgrade()
    sql = output.getvalue()
    for table in (
        "quant_accounts",
        "quant_positions",
        "quant_orders",
        "quant_commands",
        "quant_bars",
    ):
        assert f"CREATE TABLE {table}" in sql
    assert "CREATE UNIQUE INDEX uq_quant_order_active" in sql
    assert "DROP TABLE" not in sql and "ALTER TABLE bot_services" not in sql


def test_quant_provider_migration_preserves_existing_primary_and_order_state():
    migration = importlib.import_module("migrations.versions.20261028_0059_quant_sources")
    assert migration.down_revision == "20261027_0058"
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        migration.upgrade()
    sql = output.getvalue()
    assert "provider_order" in sql and "execution_provider" in sql
    assert "CREATE TABLE quant_provider_credentials" in sql
    assert "jsonb_build_array(provider)" in sql
    assert "DROP TABLE" not in sql
