"""Independent editor sessions; no formal attempts or task data are modified."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260908_0021"
down_revision: str | None = "20260905_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "interactive_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "terminal_id",
            sa.Uuid(),
            sa.ForeignKey("terminals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "device_id",
            sa.Uuid(),
            sa.ForeignKey("target_devices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("create_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("ciphertext", sa.Text()),
        sa.Column("key_id", sa.String(16)),
        sa.Column("terminal_instance_id", sa.Uuid()),
        sa.Column("error_code", sa.String(64)),
        sa.CheckConstraint(
            "status IN ('pending','active','closing','closed','failed','expired')",
            name="ck_editor_status",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_editor_version"),
        sa.CheckConstraint("create_deadline > created_at", name="ck_editor_deadline"),
        sa.CheckConstraint("(ciphertext IS NULL) = (key_id IS NULL)", name="ck_editor_secret_pair"),
        sa.CheckConstraint(
            "status NOT IN ('closed','failed','expired') OR ciphertext IS NULL",
            name="ck_editor_final_no_secret",
        ),
        sa.CheckConstraint(
            "status <> 'active' OR (ciphertext IS NOT NULL AND terminal_instance_id IS NOT NULL)",
            name="ck_editor_active_secret",
        ),
    )
    op.create_index(
        "uq_editor_active_device",
        "interactive_sessions",
        ["device_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','active','closing')"),
    )
    op.create_index(
        "ix_editor_terminal_pending",
        "interactive_sessions",
        ["terminal_id", "status", "created_at", "id"],
    )


def downgrade() -> None:
    # Explicit operator rollback only; this removes editor-session history.
    op.drop_table("interactive_sessions")
