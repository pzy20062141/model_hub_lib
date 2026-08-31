from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, model_validator

from .common import StrictModel
from .enums import (
    QuotaOverrideMode,
    QuotaPeriodType,
    QuotaPolicySource,
    UserQuotaStatus,
)


class ModelCreditRateInput(StrictModel):
    """Tenant-owned conversion rule from provider usage to internal credits."""

    tenant_id: str = Field(min_length=1, max_length=128)
    configured_model_id: str = Field(min_length=1, max_length=64)
    per_request_credits: Decimal = Field(default=Decimal("1"), ge=0)
    input_credits_per_1k: Decimal = Field(default=Decimal("0"), ge=0)
    output_credits_per_1k: Decimal = Field(default=Decimal("0"), ge=0)
    billable_unit_credits: Decimal = Field(default=Decimal("0"), ge=0)


class ModelCreditRateView(ModelCreditRateInput):
    version: int
    updated_at: datetime


class UserQuotaTemplateInput(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    period_type: QuotaPeriodType = QuotaPeriodType.MONTH
    credit_limit: Decimal | None = Field(default=None, ge=0)
    soft_limit_percent: int = Field(default=80, ge=1, le=99)
    is_default: bool = False
    enabled: bool = True
    template_id: str | None = Field(default=None, max_length=64)


class UserQuotaTemplateView(UserQuotaTemplateInput):
    template_id: str
    version: int
    updated_at: datetime


class RoleQuotaBindingInput(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    role_code: str = Field(min_length=1, max_length=128)
    template_id: str = Field(min_length=1, max_length=64)
    priority: int = Field(default=0, ge=-10000, le=10000)


class UserQuotaAssignmentInput(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    template_id: str | None = Field(default=None, max_length=64)
    override_mode: QuotaOverrideMode = QuotaOverrideMode.INHERIT
    credit_limit: Decimal | None = Field(default=None, ge=0)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_override(self) -> UserQuotaAssignmentInput:
        if self.override_mode == QuotaOverrideMode.LIMITED and self.credit_limit is None:
            raise ValueError("credit_limit is required when override_mode is LIMITED")
        if self.override_mode != QuotaOverrideMode.LIMITED and self.credit_limit is not None:
            raise ValueError("credit_limit is only valid when override_mode is LIMITED")
        return self


class UserQuotaSummary(StrictModel):
    tenant_id: str
    user_id: str
    status: UserQuotaStatus
    source_type: QuotaPolicySource
    source_id: str | None = None
    period_type: QuotaPeriodType
    period_start: datetime
    period_end: datetime
    credit_limit: Decimal | None = None
    credits_used: Decimal
    credits_reserved: Decimal
    credits_remaining: Decimal | None = None
    soft_limit_percent: int


class UserCostItem(StrictModel):
    invocation_id: str
    tenant_id: str
    user_id: str
    configured_model_id: str
    operation: str
    credits: Decimal
    usage: dict | None = None
    created_at: datetime


class UserCostReport(StrictModel):
    items: list[UserCostItem]
    total_credits: Decimal


class UserQuotaAllocation(StrictModel):
    invocation_id: str
    configured_model_id: str
    user_id: str
    reserved_credits: Decimal
    summary: UserQuotaSummary
