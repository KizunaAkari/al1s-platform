from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
    Index, Integer, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from al1s.adapters.postgres.base import Base


class BotDrawingSettingsRow(Base):
    __tablename__ = "bot_drawing_settings"
    __table_args__ = (
        ForeignKeyConstraint(["service_id", "default_text_preset_id"],
            ["bot_drawing_presets.service_id", "bot_drawing_presets.id"],
            name="fk_drawing_default_text", ondelete="RESTRICT"),
        ForeignKeyConstraint(["service_id", "default_edit_preset_id"],
            ["bot_drawing_presets.service_id", "bot_drawing_presets.id"],
            name="fk_drawing_default_edit", ondelete="RESTRICT"),
        ForeignKeyConstraint(["service_id", "default_online_preset_id"],
            ["bot_drawing_presets.service_id", "bot_drawing_presets.id"],
            name="fk_drawing_default_online", ondelete="RESTRICT", use_alter=True),
        ForeignKeyConstraint(["service_id", "active_prompt_profile_id"],
            ["bot_drawing_prompt_profiles.service_id", "bot_drawing_prompt_profiles.id"],
            name="fk_drawing_active_prompt_profile", ondelete="RESTRICT", use_alter=True),
        CheckConstraint("next_workflow_number > 0 AND row_version > 0 AND applied_version >= 0 "
                        "AND media_bytes >= 0 AND prompt_selection_version > 0",
                        name="ck_drawing_settings_version"),
        CheckConstraint("resource_applied_version >= 0", name="ck_drawing_resource_version"),
    )
    service_id: Mapped[UUID] = mapped_column(ForeignKey("bot_services.id", ondelete="RESTRICT"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    allow_private: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    event_ws_url: Mapped[str | None] = mapped_column(String(1024))
    comfy_base_url: Mapped[str] = mapped_column(String(1024), nullable=False,
                                                  server_default="http://host.docker.internal:8188")
    keep_drawing_warm: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    default_text_preset_id: Mapped[UUID | None] = mapped_column()
    default_edit_preset_id: Mapped[UUID | None] = mapped_column()
    default_online_preset_id: Mapped[UUID | None] = mapped_column()
    active_prompt_profile_id: Mapped[UUID | None] = mapped_column()
    prompt_selection_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    next_workflow_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    media_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    resource_secret_id: Mapped[UUID | None] = mapped_column(ForeignKey("encrypted_secrets.id", ondelete="RESTRICT"))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    applied_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    resource_applied_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class BotDrawingScopeRow(Base):
    __tablename__ = "bot_drawing_scopes"
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("bot_drawing_settings.service_id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(20), primary_key=True)


class BotDrawingPromptProfileRow(Base):
    __tablename__ = "bot_drawing_prompt_profiles"
    __table_args__ = (
        UniqueConstraint("service_id", "id", name="uq_drawing_prompt_profile_owner"),
        CheckConstraint("current_revision > 0 AND row_version > 0", name="ck_drawing_prompt_profile_version"),
        Index("uq_drawing_prompt_profile_name", "service_id", "normalized_name", unique=True,
              postgresql_where=text("deleted_at IS NULL")),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("bot_drawing_settings.service_id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(128), nullable=False)
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                   server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                   server_default=text("CURRENT_TIMESTAMP"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BotDrawingPromptRevisionRow(Base):
    __tablename__ = "bot_drawing_prompt_profile_revisions"
    __table_args__ = (
        ForeignKeyConstraint(["service_id", "profile_id"],
            ["bot_drawing_prompt_profiles.service_id", "bot_drawing_prompt_profiles.id"],
            name="fk_drawing_prompt_revision_owner", ondelete="RESTRICT"),
        UniqueConstraint("service_id", "profile_id", "revision", name="uq_drawing_prompt_revision_owner"),
        CheckConstraint("revision > 0 AND network_mode IN ('direct','proxy')", name="ck_drawing_prompt_revision"),
    )
    profile_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[UUID] = mapped_column(nullable=False)
    adapter_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str] = mapped_column(String(1024), nullable=False, server_default="")
    network_mode: Mapped[str] = mapped_column(String(8), nullable=False, server_default="direct")
    secret_id: Mapped[UUID | None] = mapped_column(ForeignKey("encrypted_secrets.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                   server_default=text("CURRENT_TIMESTAMP"))


class BotDrawingPresetRow(Base):
    __tablename__ = "bot_drawing_presets"
    __table_args__ = (
        UniqueConstraint("service_id", "id", name="uq_drawing_preset_owner"),
        UniqueConstraint("service_id", "number", name="uq_drawing_preset_number"),
        UniqueConstraint("service_id", "normalized_name", name="uq_drawing_preset_name"),
        CheckConstraint("(engine = 'comfy' AND kind IN ('txt2img','image_edit')) OR "
                        "(engine = 'openai' AND kind = 'both')", name="ck_drawing_preset_kind"),
        CheckConstraint("number > 0 AND current_revision > 0 AND row_version > 0", name="ck_drawing_preset_version"),
        Index("ix_drawing_preset_service_number", "service_id", "number", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    service_id: Mapped[UUID] = mapped_column(ForeignKey("bot_services.id", ondelete="RESTRICT"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    engine: Mapped[str] = mapped_column(String(16), nullable=False, server_default="comfy")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class BotDrawingRevisionRow(Base):
    __tablename__ = "bot_drawing_preset_revisions"
    __table_args__ = (
        ForeignKeyConstraint(["service_id", "preset_id"], ["bot_drawing_presets.service_id", "bot_drawing_presets.id"],
                             ondelete="RESTRICT", name="fk_drawing_revision_owner"),
        UniqueConstraint("service_id", "preset_id", "revision", name="uq_drawing_revision_owner"),
        CheckConstraint("revision > 0", name="ck_drawing_revision_number"),
        CheckConstraint("validation_state IN ('unchecked','missing','needs_preview','ready')",
                        name="ck_drawing_revision_validation"),
        CheckConstraint("measured_peak_bytes IS NULL OR measured_peak_bytes >= 0", name="ck_drawing_revision_peak"),
        CheckConstraint("(dependency_manifest IS NULL) = (dependency_signature IS NULL)", name="ck_drawing_revision_dependency"),
        CheckConstraint("online_network_mode IS NULL OR online_network_mode IN ('direct','proxy')",
                        name="ck_drawing_online_network"),
        CheckConstraint("(online_model_id IS NULL AND online_base_url IS NULL AND online_network_mode IS NULL "
                        "AND online_size IS NULL AND online_quality IS NULL AND online_prompt_model IS NULL "
                        "AND online_secret_id IS NULL) OR (online_model_id IS NOT NULL AND online_model_id IN "
                        "('gpt-image-2.6-flare','gpt-image-2.6-sunburst') AND online_base_url IS NOT NULL "
                        "AND online_network_mode IS NOT NULL AND online_network_mode IN ('direct','proxy') "
                        "AND online_size IS NOT NULL AND online_size IN "
                        "('auto','1024x1024','1024x1536','1536x1024') AND online_quality IS NOT NULL AND online_quality IN "
                        "('auto','low','medium','high','xhigh','max') AND online_prompt_model IS NOT NULL)",
                        name="ck_drawing_online_config"),
    )
    preset_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[UUID] = mapped_column(nullable=False)
    source_graph: Mapped[dict | None] = mapped_column(JSONB)
    execution_template: Mapped[dict | None] = mapped_column(JSONB)
    source_bindings: Mapped[dict | None] = mapped_column(JSONB)
    bindings: Mapped[dict | None] = mapped_column(JSONB)
    manifest: Mapped[list] = mapped_column(JSONB, nullable=False)
    graph_hash: Mapped[str | None] = mapped_column(String(64))
    execution_template_hash: Mapped[str | None] = mapped_column(String(64))
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(80), nullable=False)
    dependency_manifest: Mapped[dict | None] = mapped_column(JSONB)
    dependency_signature: Mapped[str | None] = mapped_column(String(64))
    validation_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="unchecked")
    validation_code: Mapped[str | None] = mapped_column(String(64))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    measured_peak_bytes: Mapped[int | None] = mapped_column(BigInteger)
    online_model_id: Mapped[str | None] = mapped_column(String(200))
    online_base_url: Mapped[str | None] = mapped_column(String(1024))
    online_network_mode: Mapped[str | None] = mapped_column(String(8))
    online_size: Mapped[str | None] = mapped_column(String(16))
    online_quality: Mapped[str | None] = mapped_column(String(8))
    online_prompt_model: Mapped[bool | None] = mapped_column(Boolean)
    online_secret_id: Mapped[UUID | None] = mapped_column(ForeignKey("encrypted_secrets.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class BotDrawingLoraRouteRow(Base):
    __tablename__ = "bot_drawing_lora_routes"
    __table_args__ = (
        ForeignKeyConstraint(["service_id", "preset_id", "revision"],
            ["bot_drawing_preset_revisions.service_id", "bot_drawing_preset_revisions.preset_id",
             "bot_drawing_preset_revisions.revision"], ondelete="RESTRICT", name="fk_drawing_lora_revision"),
        Index("ix_drawing_lora_lookup", "service_id", "normalized_key", "preset_id", "revision"),
    )
    preset_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    normalized_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    service_id: Mapped[UUID] = mapped_column(nullable=False)
    actual_lora_name: Mapped[str] = mapped_column(String(512), nullable=False)


class BotDrawingGroupModeRow(Base):
    __tablename__ = "bot_drawing_group_modes"
    __table_args__ = (
        CheckConstraint("mode IN ('green','r18') AND row_version > 0", name="ck_drawing_group_mode"),
    )
    service_id: Mapped[UUID] = mapped_column(ForeignKey("bot_services.id", ondelete="RESTRICT"), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, server_default="green")
    restrict_workflows: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class BotDrawingGroupPresetRow(Base):
    __tablename__ = "bot_drawing_group_presets"
    __table_args__ = (
        ForeignKeyConstraint(["service_id", "group_id"],
            ["bot_drawing_group_modes.service_id", "bot_drawing_group_modes.group_id"],
            ondelete="CASCADE", name="fk_drawing_group_preset_group"),
        ForeignKeyConstraint(["service_id", "preset_id"],
            ["bot_drawing_presets.service_id", "bot_drawing_presets.id"],
            ondelete="CASCADE", name="fk_drawing_group_preset_owner"),
        Index("ix_drawing_group_presets_preset", "service_id", "preset_id"),
    )
    service_id: Mapped[UUID] = mapped_column(primary_key=True)
    group_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    preset_id: Mapped[UUID] = mapped_column(primary_key=True)


class BotDrawingSelectionRow(Base):
    __tablename__ = "bot_drawing_selections"
    __table_args__ = (
        ForeignKeyConstraint(["service_id", "preset_id"], ["bot_drawing_presets.service_id", "bot_drawing_presets.id"],
                             ondelete="RESTRICT", name="fk_drawing_selection_preset"),
        CheckConstraint("scope_kind IN ('qq_group','qq_private')", name="ck_drawing_selection_scope"),
        Index("ix_drawing_selection_preset", "service_id", "preset_id"),
    )
    service_id: Mapped[UUID] = mapped_column(primary_key=True)
    scope_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    sender_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    preset_id: Mapped[UUID] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class BotDrawingTaskRow(Base):
    __tablename__ = "bot_drawing_tasks"
    __table_args__ = (
        ForeignKeyConstraint(["service_id", "preset_id", "preset_revision"],
            ["bot_drawing_preset_revisions.service_id", "bot_drawing_preset_revisions.preset_id",
             "bot_drawing_preset_revisions.revision"], ondelete="RESTRICT", name="fk_drawing_task_revision"),
        ForeignKeyConstraint(["service_id", "prompt_profile_id", "prompt_profile_revision"],
            ["bot_drawing_prompt_profile_revisions.service_id", "bot_drawing_prompt_profile_revisions.profile_id",
             "bot_drawing_prompt_profile_revisions.revision"], ondelete="RESTRICT", name="fk_drawing_task_prompt_revision"),
        CheckConstraint("operation IN ('draw','tag') AND origin IN ('qq','admin_preview')", name="ck_drawing_task_kind"),
        CheckConstraint("(operation = 'draw' AND preset_id IS NOT NULL AND preset_revision IS NOT NULL AND dependency_signature IS NOT NULL) OR "
                        "(operation = 'tag' AND preset_id IS NULL AND preset_revision IS NULL AND dependency_signature IS NULL)", name="ck_drawing_task_preset"),
        CheckConstraint("(origin = 'qq' AND scope_kind IS NOT NULL AND scope_kind IN ('qq_group','qq_private') AND target_id IS NOT NULL "
                        "AND sender_id IS NOT NULL AND source_message_id IS NOT NULL AND bot_self_id IS NOT NULL) OR "
                        "(origin = 'admin_preview' AND scope_kind IS NULL AND target_id IS NULL AND sender_id IS NULL "
                        "AND source_message_id IS NULL AND bot_self_id IS NULL)", name="ck_drawing_task_identity"),
        CheckConstraint("generation_status IN ('queued','submitting','running','succeeded','failed',"
                        "'expired','cancelled','cancelling','reconciling')", name="ck_drawing_task_status"),
        CheckConstraint("resource_registration_state IN ('pending','confirmed','unknown')", name="ck_drawing_task_registration"),
        CheckConstraint("audit_status IN ('pending','allow','block','unknown','not_required')", name="ck_drawing_task_audit"),
        CheckConstraint("input_audit_status IN ('pending','allow','block','unknown','not_required')", name="ck_drawing_task_input_audit"),
        CheckConstraint("send_status IN ('none','ready','sending','sent','failed','unknown')", name="ck_drawing_task_send"),
        CheckConstraint("notice_status IN ('none','ready','sending','sent','failed','unknown')", name="ck_drawing_task_notice"),
        CheckConstraint("cleanup_status IN ('none','pending','done','deferred','failed')", name="ck_drawing_task_cleanup"),
        CheckConstraint("prompt_status IN ('waiting','working','ready','failed')", name="ck_drawing_task_prompt_status"),
        CheckConstraint("online_request_state IS NULL OR online_request_state IN "
                        "('not_started','submitting','sent','received','unknown','failed')",
                        name="ck_drawing_task_online_request"),
        CheckConstraint("(NOT online_image_received OR online_request_sent) AND "
                        "(online_request_state IS NOT NULL OR (NOT online_request_sent AND NOT online_image_received))",
                        name="ck_drawing_task_online_facts"),
        CheckConstraint("(prompt_profile_id IS NULL) = (prompt_profile_revision IS NULL)",
                        name="ck_drawing_task_prompt_revision"),
        CheckConstraint("deletion_state IN ('visible','deleting','deleted')", name="ck_drawing_task_deletion"),
        CheckConstraint("row_version > 0", name="ck_drawing_task_version"),
        UniqueConstraint("prompt_id", name="uq_drawing_task_prompt"),
        Index("uq_drawing_source", "service_id", "bot_self_id", "scope_kind", "target_id", "source_message_id",
              unique=True, postgresql_where=text("origin = 'qq'")),
        Index("ix_drawing_task_service_time", "service_id", "created_at", "id"),
        Index("ix_drawing_task_history", "service_id", "deletion_state", "created_at", "id"),
        Index("ix_drawing_task_deleting", "id", postgresql_where=text("deletion_state = 'deleting'")),
        Index("ix_drawing_task_endpoint_status", "endpoint_key", "generation_status", "next_check_at", "id"),
        Index("ix_drawing_task_queue", "endpoint_key", "created_at", "id",
              postgresql_where=text("generation_status = 'queued'")),
        Index("ix_drawing_task_cleanup", "cleanup_status", "next_check_at", "id"),
        Index("ix_drawing_task_media_expiry", "media_expires_at", "id"),
        Index("ix_drawing_task_retention", "created_at", "id",
              postgresql_where=text("cleanup_status = 'done' AND input_bytes IS NULL AND output_bytes IS NULL")),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    service_id: Mapped[UUID] = mapped_column(ForeignKey("bot_services.id", ondelete="RESTRICT"), nullable=False)
    operation: Mapped[str] = mapped_column(String(8), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_kind: Mapped[str | None] = mapped_column(String(16))
    target_id: Mapped[str | None] = mapped_column(String(20))
    sender_id: Mapped[str | None] = mapped_column(String(20))
    source_message_id: Mapped[str | None] = mapped_column(String(64))
    bot_self_id: Mapped[str | None] = mapped_column(String(20))
    preset_id: Mapped[UUID | None] = mapped_column()
    preset_revision: Mapped[int | None] = mapped_column(Integer)
    dependency_signature: Mapped[str | None] = mapped_column(String(64))
    ai_version: Mapped[int | None] = mapped_column(Integer)
    drawing_version: Mapped[int | None] = mapped_column(Integer)
    chat_profile_id: Mapped[UUID | None] = mapped_column()
    intent_mode: Mapped[str | None] = mapped_column(String(16))
    wish_text: Mapped[str | None] = mapped_column(Text)
    prompt_workflow_name: Mapped[str | None] = mapped_column(String(32))
    prompt_aliases: Mapped[list | None] = mapped_column(JSONB)
    prompt_profile_id: Mapped[UUID | None] = mapped_column()
    prompt_profile_revision: Mapped[int | None] = mapped_column(Integer)
    prompt_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ready")
    online_request_state: Mapped[str | None] = mapped_column(String(16))
    online_request_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    online_image_received: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    processing_stage: Mapped[str] = mapped_column(String(32), nullable=False, server_default="queued")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    deletion_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="visible")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    positive: Mapped[str | None] = mapped_column(Text)
    negative: Mapped[str | None] = mapped_column(Text)
    requested_lora_keys: Mapped[list | None] = mapped_column(JSONB)
    ignored_lora_keys: Mapped[list | None] = mapped_column(JSONB)
    route_reason: Mapped[str | None] = mapped_column(String(24))
    seed: Mapped[int | None] = mapped_column(BigInteger)
    reference_message_id: Mapped[str | None] = mapped_column(String(64))
    input_reference: Mapped[str | None] = mapped_column(String(512))
    input_sha256: Mapped[str | None] = mapped_column(String(64))
    input_bytes: Mapped[int | None] = mapped_column(BigInteger)
    endpoint_key: Mapped[str] = mapped_column(String(128), nullable=False)
    comfy_endpoint: Mapped[str | None] = mapped_column(String(1024))
    prompt_id: Mapped[UUID] = mapped_column(nullable=False)
    boot_id: Mapped[str | None] = mapped_column(String(64))
    execution_hash: Mapped[str | None] = mapped_column(String(64))
    resource_registration_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prompt_transport_started: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    generation_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    send_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="none")
    notice_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="none")
    cleanup_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="none")
    audit_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    input_audit_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    audit_mode: Mapped[str | None] = mapped_column(String(8))
    audit_model_revision: Mapped[str | None] = mapped_column(String(64))
    audit_policy_version: Mapped[str | None] = mapped_column(String(64))
    audit_sha256: Mapped[str | None] = mapped_column(String(64))
    input_audit_sha256: Mapped[str | None] = mapped_column(String(64))
    input_audit_model_revision: Mapped[str | None] = mapped_column(String(64))
    input_audit_policy_version: Mapped[str | None] = mapped_column(String(64))
    output_reference: Mapped[str | None] = mapped_column(String(512))
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    output_bytes: Mapped[int | None] = mapped_column(BigInteger)
    reply_kind: Mapped[str | None] = mapped_column(String(16))
    tag_result: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    online_moderation_details: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    notice_transport_started: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    result_transport_started: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    notice_receipt: Mapped[str | None] = mapped_column(String(64))
    result_receipt: Mapped[str | None] = mapped_column(String(64))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    media_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
