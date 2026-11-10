"""Add QQ drawing presets, choices, and durable tasks."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261103_0071"
down_revision = "20261101_0070"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('bot_ai_vision_jobs', sa.Column('boot_id', sa.String(length=64), nullable=True))
    op.add_column('bot_ai_vision_jobs', sa.Column('execution_hash', sa.String(length=64), nullable=True))
    op.add_column('bot_ai_vision_jobs', sa.Column('registration_state', sa.String(length=16), nullable=True))
    op.add_column('bot_ai_vision_jobs', sa.Column('transport_started', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.create_check_constraint('ck_ai_vision_resource_state', 'bot_ai_vision_jobs',
        "(boot_id IS NULL AND execution_hash IS NULL AND registration_state IS NULL AND NOT transport_started) OR "
        "(boot_id IS NOT NULL AND execution_hash IS NOT NULL AND registration_state IS NOT NULL "
        "AND registration_state IN ('pending','confirmed','unknown','rejected') "
        "AND (NOT transport_started OR registration_state <> 'pending') "
        "AND (registration_state <> 'rejected' OR transport_started))")
    op.create_table('bot_drawing_presets',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('service_id', sa.Uuid(), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=32), nullable=False),
    sa.Column('normalized_name', sa.String(length=128), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('current_revision', sa.Integer(), nullable=False),
    sa.Column('row_version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint("kind IN ('txt2img','image_edit')", name='ck_drawing_preset_kind'),
    sa.CheckConstraint('number > 0 AND current_revision > 0 AND row_version > 0', name='ck_drawing_preset_version'),
    sa.ForeignKeyConstraint(['service_id'], ['bot_services.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('service_id', 'id', name='uq_drawing_preset_owner'),
    sa.UniqueConstraint('service_id', 'normalized_name', name='uq_drawing_preset_name'),
    sa.UniqueConstraint('service_id', 'number', name='uq_drawing_preset_number')
    )

    op.create_index('ix_drawing_preset_service_number', 'bot_drawing_presets', ['service_id', 'number', 'id'], unique=False)

    op.create_table('bot_drawing_settings',
    sa.Column('service_id', sa.Uuid(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('keep_drawing_warm', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('default_text_preset_id', sa.Uuid(), nullable=True),
    sa.Column('default_edit_preset_id', sa.Uuid(), nullable=True),
    sa.Column('next_workflow_number', sa.Integer(), server_default='1', nullable=False),
    sa.Column('media_bytes', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('resource_secret_id', sa.Uuid(), nullable=True),
    sa.Column('row_version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('applied_version', sa.Integer(), server_default='0', nullable=False),
    sa.CheckConstraint('next_workflow_number > 0 AND row_version > 0 AND applied_version >= 0 AND media_bytes >= 0', name='ck_drawing_settings_version'),
    sa.ForeignKeyConstraint(['resource_secret_id'], ['encrypted_secrets.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['service_id', 'default_edit_preset_id'], ['bot_drawing_presets.service_id', 'bot_drawing_presets.id'], name='fk_drawing_default_edit', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['service_id', 'default_text_preset_id'], ['bot_drawing_presets.service_id', 'bot_drawing_presets.id'], name='fk_drawing_default_text', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['service_id'], ['bot_services.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('service_id')
    )

    op.create_table('bot_drawing_preset_revisions',
    sa.Column('preset_id', sa.Uuid(), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('service_id', sa.Uuid(), nullable=False),
    sa.Column('source_graph', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('execution_template', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('source_bindings', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('bindings', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('manifest', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('graph_hash', sa.String(length=64), nullable=False),
    sa.Column('execution_template_hash', sa.String(length=64), nullable=False),
    sa.Column('snapshot_hash', sa.String(length=64), nullable=False),
    sa.Column('adapter_version', sa.String(length=80), nullable=False),
    sa.Column('dependency_manifest', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('dependency_signature', sa.String(length=64), nullable=True),
    sa.Column('validation_state', sa.String(length=16), server_default='unchecked', nullable=False),
    sa.Column('validation_code', sa.String(length=64), nullable=True),
    sa.Column('validated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('measured_peak_bytes', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint("validation_state IN ('unchecked','missing','needs_preview','ready')", name='ck_drawing_revision_validation'),
    sa.CheckConstraint('measured_peak_bytes IS NULL OR measured_peak_bytes >= 0', name='ck_drawing_revision_peak'),
    sa.CheckConstraint('(dependency_manifest IS NULL) = (dependency_signature IS NULL)', name='ck_drawing_revision_dependency'),
    sa.CheckConstraint('revision > 0', name='ck_drawing_revision_number'),
    sa.ForeignKeyConstraint(['service_id', 'preset_id'], ['bot_drawing_presets.service_id', 'bot_drawing_presets.id'], name='fk_drawing_revision_owner', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('preset_id', 'revision'),
    sa.UniqueConstraint('service_id', 'preset_id', 'revision', name='uq_drawing_revision_owner')
    )

    op.create_table('bot_drawing_lora_routes',
    sa.Column('preset_id', sa.Uuid(), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('normalized_key', sa.String(length=128), nullable=False),
    sa.Column('service_id', sa.Uuid(), nullable=False),
    sa.Column('actual_lora_name', sa.String(length=512), nullable=False),
    sa.ForeignKeyConstraint(['service_id', 'preset_id', 'revision'], ['bot_drawing_preset_revisions.service_id', 'bot_drawing_preset_revisions.preset_id', 'bot_drawing_preset_revisions.revision'], name='fk_drawing_lora_revision', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('preset_id', 'revision', 'normalized_key')
    )

    op.create_index('ix_drawing_lora_lookup', 'bot_drawing_lora_routes', ['service_id', 'normalized_key', 'preset_id', 'revision'], unique=False)

    op.create_table('bot_drawing_group_modes',
    sa.Column('service_id', sa.Uuid(), nullable=False),
    sa.Column('group_id', sa.String(length=20), nullable=False),
    sa.Column('mode', sa.String(length=8), server_default='green', nullable=False),
    sa.Column('row_version', sa.Integer(), server_default='1', nullable=False),
    sa.CheckConstraint("mode IN ('green','r18') AND row_version > 0", name='ck_drawing_group_mode'),
    sa.ForeignKeyConstraint(['service_id'], ['bot_services.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('service_id', 'group_id')
    )

    op.create_table('bot_drawing_selections',
    sa.Column('service_id', sa.Uuid(), nullable=False),
    sa.Column('scope_kind', sa.String(length=16), nullable=False),
    sa.Column('target_id', sa.String(length=20), nullable=False),
    sa.Column('sender_id', sa.String(length=20), nullable=False),
    sa.Column('preset_id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint("scope_kind IN ('qq_group','qq_private')", name='ck_drawing_selection_scope'),
    sa.ForeignKeyConstraint(['service_id', 'preset_id'], ['bot_drawing_presets.service_id', 'bot_drawing_presets.id'], name='fk_drawing_selection_preset', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('service_id', 'scope_kind', 'target_id', 'sender_id')
    )

    op.create_index('ix_drawing_selection_preset', 'bot_drawing_selections', ['service_id', 'preset_id'], unique=False)

    op.create_table('bot_drawing_tasks',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('service_id', sa.Uuid(), nullable=False),
    sa.Column('operation', sa.String(length=8), nullable=False),
    sa.Column('origin', sa.String(length=16), nullable=False),
    sa.Column('scope_kind', sa.String(length=16), nullable=True),
    sa.Column('target_id', sa.String(length=20), nullable=True),
    sa.Column('sender_id', sa.String(length=20), nullable=True),
    sa.Column('source_message_id', sa.String(length=64), nullable=True),
    sa.Column('bot_self_id', sa.String(length=20), nullable=True),
    sa.Column('preset_id', sa.Uuid(), nullable=True),
    sa.Column('preset_revision', sa.Integer(), nullable=True),
    sa.Column('dependency_signature', sa.String(length=64), nullable=True),
    sa.Column('ai_version', sa.Integer(), nullable=True),
    sa.Column('drawing_version', sa.Integer(), nullable=True),
    sa.Column('chat_profile_id', sa.Uuid(), nullable=True),
    sa.Column('intent_mode', sa.String(length=16), nullable=True),
    sa.Column('positive', sa.Text(), nullable=True),
    sa.Column('negative', sa.Text(), nullable=True),
    sa.Column('requested_lora_keys', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('ignored_lora_keys', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('route_reason', sa.String(length=24), nullable=True),
    sa.Column('seed', sa.BigInteger(), nullable=True),
    sa.Column('reference_message_id', sa.String(length=64), nullable=True),
    sa.Column('input_reference', sa.String(length=512), nullable=True),
    sa.Column('input_sha256', sa.String(length=64), nullable=True),
    sa.Column('input_bytes', sa.BigInteger(), nullable=True),
    sa.Column('endpoint_key', sa.String(length=128), nullable=False),
    sa.Column('prompt_id', sa.Uuid(), nullable=False),
    sa.Column('boot_id', sa.String(length=64), nullable=True),
    sa.Column('execution_hash', sa.String(length=64), nullable=True),
    sa.Column('resource_registration_state', sa.String(length=16), server_default='pending', nullable=False),
    sa.Column('registered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('prompt_transport_started', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('generation_status', sa.String(length=16), server_default='queued', nullable=False),
    sa.Column('send_status', sa.String(length=16), server_default='none', nullable=False),
    sa.Column('notice_status', sa.String(length=16), server_default='none', nullable=False),
    sa.Column('cleanup_status', sa.String(length=16), server_default='none', nullable=False),
    sa.Column('audit_status', sa.String(length=16), server_default='pending', nullable=False),
    sa.Column('input_audit_status', sa.String(length=16), server_default='pending', nullable=False),
    sa.Column('audit_mode', sa.String(length=8), nullable=True),
    sa.Column('audit_model_revision', sa.String(length=64), nullable=True),
    sa.Column('audit_policy_version', sa.String(length=64), nullable=True),
    sa.Column('audit_sha256', sa.String(length=64), nullable=True),
    sa.Column('input_audit_sha256', sa.String(length=64), nullable=True),
    sa.Column('input_audit_model_revision', sa.String(length=64), nullable=True),
    sa.Column('input_audit_policy_version', sa.String(length=64), nullable=True),
    sa.Column('output_reference', sa.String(length=512), nullable=True),
    sa.Column('output_sha256', sa.String(length=64), nullable=True),
    sa.Column('output_bytes', sa.BigInteger(), nullable=True),
    sa.Column('reply_kind', sa.String(length=16), nullable=True),
    sa.Column('tag_result', sa.Text(), nullable=True),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('notice_transport_started', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('result_transport_started', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('notice_receipt', sa.String(length=64), nullable=True),
    sa.Column('result_receipt', sa.String(length=64), nullable=True),
    sa.Column('row_version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_check_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('media_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(operation = 'draw' AND preset_id IS NOT NULL AND preset_revision IS NOT NULL AND dependency_signature IS NOT NULL) OR (operation = 'tag' AND preset_id IS NULL AND preset_revision IS NULL AND dependency_signature IS NULL)", name='ck_drawing_task_preset'),
    sa.CheckConstraint("(origin = 'qq' AND scope_kind IS NOT NULL AND scope_kind IN ('qq_group','qq_private') AND target_id IS NOT NULL AND sender_id IS NOT NULL AND source_message_id IS NOT NULL AND bot_self_id IS NOT NULL) OR (origin = 'admin_preview' AND scope_kind IS NULL AND target_id IS NULL AND sender_id IS NULL AND source_message_id IS NULL AND bot_self_id IS NULL)", name='ck_drawing_task_identity'),
    sa.CheckConstraint("audit_status IN ('pending','allow','block','unknown','not_required')", name='ck_drawing_task_audit'),
    sa.CheckConstraint("input_audit_status IN ('pending','allow','block','unknown','not_required')", name='ck_drawing_task_input_audit'),
    sa.CheckConstraint("cleanup_status IN ('none','pending','done','deferred','failed')", name='ck_drawing_task_cleanup'),
    sa.CheckConstraint("generation_status IN ('queued','submitting','running','succeeded','failed','expired','cancelled','cancelling','reconciling')", name='ck_drawing_task_status'),
    sa.CheckConstraint("operation IN ('draw','tag') AND origin IN ('qq','admin_preview')", name='ck_drawing_task_kind'),
    sa.CheckConstraint("resource_registration_state IN ('pending','confirmed','unknown')", name='ck_drawing_task_registration'),
    sa.CheckConstraint("send_status IN ('none','ready','sending','sent','failed','unknown')", name='ck_drawing_task_send'),
    sa.CheckConstraint("notice_status IN ('none','ready','sending','sent','failed','unknown')", name='ck_drawing_task_notice'),
    sa.CheckConstraint('row_version > 0', name='ck_drawing_task_version'),
    sa.ForeignKeyConstraint(['service_id', 'preset_id', 'preset_revision'], ['bot_drawing_preset_revisions.service_id', 'bot_drawing_preset_revisions.preset_id', 'bot_drawing_preset_revisions.revision'], name='fk_drawing_task_revision', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['service_id'], ['bot_services.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('prompt_id', name='uq_drawing_task_prompt')
    )

    op.create_index('ix_drawing_task_cleanup', 'bot_drawing_tasks', ['cleanup_status', 'next_check_at', 'id'], unique=False)

    op.create_index('ix_drawing_task_endpoint_status', 'bot_drawing_tasks', ['endpoint_key', 'generation_status', 'next_check_at', 'id'], unique=False)

    op.create_index('ix_drawing_task_queue', 'bot_drawing_tasks', ['endpoint_key', 'created_at', 'id'], unique=False,
                    postgresql_where=sa.text("generation_status = 'queued'"))

    op.create_index('ix_drawing_task_media_expiry', 'bot_drawing_tasks', ['media_expires_at', 'id'], unique=False)

    op.create_index('ix_drawing_task_retention', 'bot_drawing_tasks', ['created_at', 'id'], unique=False,
                    postgresql_where=sa.text("cleanup_status = 'done' AND input_bytes IS NULL AND output_bytes IS NULL"))

    op.create_index('ix_drawing_task_service_time', 'bot_drawing_tasks', ['service_id', 'created_at', 'id'], unique=False)

    op.create_index('uq_drawing_active_user', 'bot_drawing_tasks', ['service_id', 'sender_id'], unique=True, postgresql_where=sa.text("operation = 'draw' AND origin = 'qq' AND generation_status IN ('queued','submitting','running','cancelling','reconciling')"))

    op.create_index('uq_drawing_source', 'bot_drawing_tasks', ['service_id', 'bot_self_id', 'scope_kind', 'target_id', 'source_message_id'], unique=True, postgresql_where=sa.text("origin = 'qq'"))


def downgrade():
    raise RuntimeError("bot_drawing_downgrade_requires_preserving_tasks_and_presets")
