from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ProviderCredentialRecord(Base):
    __tablename__ = "provider_credential"
    __table_args__ = (
        Index("ix_credential_tenant_owner", "tenant_id", "owner_user_id"),
        Index("ix_credential_provider", "tenant_id", "provider_id"),
    )

    credential_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(128))
    plugin_id: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    encrypted_values: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_masked: Mapped[str] = mapped_column(String(128), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    deployment: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ConfiguredModelRecord(Base):
    __tablename__ = "configured_model"
    __table_args__ = (
        UniqueConstraint("credential_id", "model", "model_type", name="uq_credential_model_type"),
        Index("ix_model_tenant_owner", "tenant_id", "owner_user_id"),
        Index("ix_model_catalog_filter", "tenant_id", "provider_id", "model_type", "status"),
    )

    configured_model_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(128))
    credential_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("provider_credential.credential_id"), nullable=False
    )
    plugin_id: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    label: Mapped[str | None] = mapped_column(String(256))
    model_type: Mapped[str] = mapped_column(String(40), nullable=False)
    categories: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    input_modalities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    output_modalities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    features: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    operations: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    parameter_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    context_window: Mapped[int | None] = mapped_column(Integer)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    protocol_versions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class TenantDefaultModelRecord(Base):
    __tablename__ = "tenant_default_model"
    __table_args__ = (Index("ix_tenant_default_model_configured", "configured_model_id"),)

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_type: Mapped[str] = mapped_column(String(40), primary_key=True)
    configured_model_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("configured_model.configured_model_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ModelInvocationUsageRecord(Base):
    __tablename__ = "model_invocation_usage"

    invocation_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), index=True)
    session_id: Mapped[str | None] = mapped_column(String(160), index=True)
    query_id: Mapped[str | None] = mapped_column(String(160), index=True)
    app_id: Mapped[str | None] = mapped_column(String(128), index=True)
    configured_model_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(40), nullable=False)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    cost: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ModelCreditRateRecord(Base):
    __tablename__ = "model_credit_rate"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    configured_model_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("configured_model.configured_model_id", ondelete="CASCADE"),
        primary_key=True,
    )
    per_request_credits: Mapped[Any] = mapped_column(Numeric(20, 6), nullable=False)
    input_credits_per_1k: Mapped[Any] = mapped_column(Numeric(20, 6), nullable=False)
    output_credits_per_1k: Mapped[Any] = mapped_column(Numeric(20, 6), nullable=False)
    billable_unit_credits: Mapped[Any] = mapped_column(Numeric(20, 6), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class UserQuotaTemplateRecord(Base):
    __tablename__ = "user_quota_template"
    __table_args__ = (
        Index("ix_user_quota_template_default", "tenant_id", "is_default", "enabled"),
    )

    template_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    period_type: Mapped[str] = mapped_column(String(16), nullable=False)
    credit_limit: Mapped[Any | None] = mapped_column(Numeric(20, 6))
    soft_limit_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class UserQuotaRoleBindingRecord(Base):
    __tablename__ = "user_quota_role_binding"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    role_code: Mapped[str] = mapped_column(String(128), primary_key=True)
    template_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("user_quota_template.template_id", ondelete="CASCADE"),
        nullable=False,
    )
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class UserQuotaAssignmentRecord(Base):
    __tablename__ = "user_quota_assignment"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    template_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("user_quota_template.template_id", ondelete="SET NULL")
    )
    override_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    credit_limit: Mapped[Any | None] = mapped_column(Numeric(20, 6))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class UserQuotaPeriodRecord(Base):
    __tablename__ = "user_quota_period"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "period_start", "period_end", name="uq_user_quota_period"
        ),
        Index("ix_user_quota_period_lookup", "tenant_id", "user_id", "period_end"),
    )

    period_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    period_type: Mapped[str] = mapped_column(String(16), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    credit_limit: Mapped[Any | None] = mapped_column(Numeric(20, 6))
    credits_used: Mapped[Any] = mapped_column(Numeric(20, 6), default=0, nullable=False)
    credits_reserved: Mapped[Any] = mapped_column(Numeric(20, 6), default=0, nullable=False)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(128))
    soft_limit_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class UserQuotaReservationRecord(Base):
    __tablename__ = "user_quota_reservation"

    invocation_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    period_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("user_quota_period.period_id"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    configured_model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(40), nullable=False)
    estimated_credits: Mapped[Any] = mapped_column(Numeric(20, 6), nullable=False)
    actual_credits: Mapped[Any | None] = mapped_column(Numeric(20, 6))
    estimated_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    rate_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserCostLedgerRecord(Base):
    __tablename__ = "user_cost_ledger"
    __table_args__ = (Index("ix_user_cost_query", "tenant_id", "user_id", "created_at"),)

    invocation_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    configured_model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(40), nullable=False)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    credits: Mapped[Any] = mapped_column(Numeric(20, 6), nullable=False)
    rate_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class UserQuotaAuditRecord(Base):
    __tablename__ = "user_quota_audit"

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    operator_user_id: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(256), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ModelRegistrationAuditRecord(Base):
    __tablename__ = "model_registration_audit"

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    operator_user_id: Mapped[str | None] = mapped_column(String(128), index=True)
    credential_id: Mapped[str | None] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(160))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class IdempotencyRecord(Base):
    __tablename__ = "model_access_idempotency"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "idempotency_key", name="uq_idempotency_scope"),
    )

    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class InvocationBindingRecord(Base):
    __tablename__ = "model_invocation_binding"

    invocation_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(160))
    query_id: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ProviderPreferenceRecord(Base):
    __tablename__ = "provider_preference"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    preferred_provider_type: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ProviderQuotaRecord(Base):
    __tablename__ = "provider_quota"
    __table_args__ = (
        Index(
            "ix_provider_quota_selection",
            "tenant_id",
            "plugin_id",
            "provider_id",
            "is_valid",
            "quota_type",
        ),
    )

    quota_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plugin_id: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(80), nullable=False)
    quota_type: Mapped[str] = mapped_column(String(16), nullable=False)
    quota_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    quota_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quota_used: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    quota_reserved: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    restrict_models: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class QuotaReservationRecord(Base):
    __tablename__ = "quota_reservation"

    invocation_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    quota_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("provider_quota.quota_id"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    configured_model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(16), nullable=False)
    quota_type: Mapped[str | None] = mapped_column(String(16))
    quota_unit: Mapped[str | None] = mapped_column(String(16))
    reserved_units: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    actual_units: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConfigurationSourceVersionRecord(Base):
    __tablename__ = "configuration_source_version"

    source_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ModelAccessOutboxRecord(Base):
    __tablename__ = "model_access_outbox"
    __table_args__ = (Index("ix_model_access_outbox_unpublished", "published_at", "created_at"),)

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(512), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
