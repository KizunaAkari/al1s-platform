"""Add optional per-group workflow restrictions."""

from alembic import op
import sqlalchemy as sa

revision = "20261105_0072"
down_revision = "20261103_0071"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bot_drawing_group_modes", sa.Column("restrict_workflows", sa.Boolean(),
        nullable=False, server_default=sa.text("false")))
    op.create_table("bot_drawing_group_presets",
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.String(20), nullable=False),
        sa.Column("preset_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("service_id", "group_id", "preset_id"),
        sa.ForeignKeyConstraint(["service_id", "group_id"],
            ["bot_drawing_group_modes.service_id", "bot_drawing_group_modes.group_id"],
            ondelete="CASCADE", name="fk_drawing_group_preset_group"),
        sa.ForeignKeyConstraint(["service_id", "preset_id"],
            ["bot_drawing_presets.service_id", "bot_drawing_presets.id"],
            ondelete="CASCADE", name="fk_drawing_group_preset_owner"))
    op.create_index("ix_drawing_group_presets_preset", "bot_drawing_group_presets", ["service_id", "preset_id"])


def downgrade():
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM bot_drawing_group_modes WHERE restrict_workflows) "
                           "OR EXISTS (SELECT 1 FROM bot_drawing_group_presets)")):
        raise RuntimeError("drawing_group_restrictions_must_be_resolved_before_downgrade")
    op.drop_index("ix_drawing_group_presets_preset", table_name="bot_drawing_group_presets")
    op.drop_table("bot_drawing_group_presets")
    op.drop_column("bot_drawing_group_modes", "restrict_workflows")
