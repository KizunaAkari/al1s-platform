"""Persist short-lived terminal MQTT sessions.

Revision ID: 20260901_0017
Revises: 20260901_0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0017"
down_revision: str | None = "20260901_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "terminal_mqtt_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("terminal_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.String(length=100), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("role_name", sa.String(length=100), nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("password_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("configured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'revoked', 'expired')",
            name="ck_terminal_mqtt_sessions_status",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at", name="ck_terminal_mqtt_sessions_expiry"
        ),
        sa.CheckConstraint("row_version > 0", name="ck_terminal_mqtt_sessions_version"),
        sa.CheckConstraint(
            "(status = 'pending' AND configured_at IS NULL AND revoked_at IS NULL) OR "
            "(status = 'active' AND configured_at IS NOT NULL AND revoked_at IS NULL) OR "
            "(status IN ('revoked', 'expired') AND revoked_at IS NOT NULL)",
            name="ck_terminal_mqtt_sessions_lifecycle",
        ),
        sa.ForeignKeyConstraint(["terminal_id"], ["terminals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id"),
        sa.UniqueConstraint("role_name"),
        sa.UniqueConstraint("username"),
    )
    op.create_index(
        "ix_terminal_mqtt_sessions_terminal_status",
        "terminal_mqtt_sessions",
        ["terminal_id", "status", "expires_at", "id"],
    )
    op.create_index(
        "ix_terminal_mqtt_sessions_expiry",
        "terminal_mqtt_sessions",
        ["status", "expires_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_terminal_mqtt_sessions_expiry", table_name="terminal_mqtt_sessions")
    op.drop_index(
        "ix_terminal_mqtt_sessions_terminal_status", table_name="terminal_mqtt_sessions"
    )
    op.drop_table("terminal_mqtt_sessions")
