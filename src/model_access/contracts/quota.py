from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import StrictModel
from .entities import ProviderRef
from .enums import ModelStatus, ProviderQuotaType, ProviderType, QuotaUnit


class HostingQuotaDefinition(StrictModel):
    """Platform-side quota specification used to lazily initialize tenant pools."""

    provider: ProviderRef
    quota_type: ProviderQuotaType = ProviderQuotaType.TRIAL
    quota_unit: QuotaUnit = QuotaUnit.TIMES
    quota_limit: int = Field(ge=-1)
    restrict_models: set[str] = set()
    enabled: bool = True


class ProviderQuotaPoolInput(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    provider: ProviderRef
    quota_type: ProviderQuotaType
    quota_unit: QuotaUnit
    quota_limit: int = Field(ge=-1)
    quota_used: int | None = Field(default=None, ge=0)
    restrict_models: set[str] = set()
    is_valid: bool = True
    quota_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_usage(self) -> ProviderQuotaPoolInput:
        if (
            self.quota_used is not None
            and self.quota_limit != -1
            and self.quota_used > self.quota_limit
        ):
            raise ValueError("quota_used must not exceed quota_limit")
        return self


class ProviderQuotaView(StrictModel):
    quota_id: str
    quota_type: ProviderQuotaType
    quota_unit: QuotaUnit
    quota_limit: int
    quota_used: int
    quota_reserved: int
    quota_remaining: int | None
    is_valid: bool
    restrict_models: set[str] = set()


class ProviderConfiguration(StrictModel):
    tenant_id: str
    user_id: str | None = None
    provider: ProviderRef
    model: str
    model_type: str
    preferred_provider_type: ProviderType
    using_provider_type: ProviderType | None = None
    status: ModelStatus
    selected_configured_model_id: str | None = None
    selected_quota_id: str | None = None
    selected_quota_type: ProviderQuotaType | None = None
    quota_unit: QuotaUnit | None = None
    quota_remaining: int | None = None
    eligible_quota_ids: list[str] = []
    fallback_reason: Literal["SYSTEM_QUOTA_EXHAUSTED"] | None = None
    source_version: int = 0


class QuotaAllocation(StrictModel):
    invocation_id: str
    configured_model_id: str
    preferred_provider_type: ProviderType
    using_provider_type: ProviderType
    reservation_id: str
    quota_id: str | None = None
    quota_type: ProviderQuotaType | None = None
    quota_unit: QuotaUnit | None = None
    reserved_units: int = 0
    fallback_reason: str | None = None
