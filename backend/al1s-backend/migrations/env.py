from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from al1s.adapters.postgres import artifact_models as artifact_models
from al1s.adapters.postgres import bot_models as bot_models
from al1s.adapters.postgres import delivery_models as delivery_models
from al1s.adapters.postgres import discord_forward_models as discord_forward_models
from al1s.adapters.postgres import editor_models as editor_models
from al1s.adapters.postgres import execution_models as execution_models
from al1s.adapters.postgres import lexicon_models as lexicon_models
from al1s.adapters.postgres import lineup_models as lineup_models
from al1s.adapters.postgres import lineup_workspace_models as lineup_workspace_models
from al1s.adapters.postgres import maa_models as maa_models
from al1s.adapters.postgres import maintenance_models as maintenance_models
from al1s.adapters.postgres import models as postgres_models  # noqa: F401
from al1s.adapters.postgres import mqtt_models as mqtt_models
from al1s.adapters.postgres import notification_models as notification_models
from al1s.adapters.postgres import release_models as release_models
from al1s.adapters.postgres import review_models as review_models
from al1s.adapters.postgres import scheduling_models as scheduling_models
from al1s.adapters.postgres.base import Base
from al1s.app.config import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
