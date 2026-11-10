"""Allow the same QQ user to hold more than one queued drawing."""

import sqlalchemy as sa
from alembic import op

revision = "20261105_0074"
down_revision = "20261105_0073"
branch_labels = None
depends_on = None

_ACTIVE_DRAW = ("operation = 'draw' AND origin = 'qq' AND "
                "generation_status IN ('queued','submitting','running','cancelling','reconciling')")


def upgrade():
    op.drop_index('uq_drawing_active_user', table_name='bot_drawing_tasks')


def downgrade():
    conflict = op.get_bind().scalar(sa.text(
        'SELECT EXISTS (SELECT 1 FROM bot_drawing_tasks WHERE ' + _ACTIVE_DRAW +
        ' GROUP BY service_id, sender_id HAVING count(*) > 1)'))
    if conflict:
        raise RuntimeError('drawing_same_user_queue_must_be_drained_before_downgrade')
    op.create_index('uq_drawing_active_user', 'bot_drawing_tasks', ['service_id', 'sender_id'],
                    unique=True, postgresql_where=sa.text(_ACTIVE_DRAW))
