from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field

from .common import PROTOCOL_VERSION, StrictModel
from .entities import ProviderRef
from .enums import (
    CredentialScope,
    InvocationStatus,
    ModelCategory,
    ModelStatus,
    ModelType,
    ProviderType,
    UserQuotaStatus,
)
from .quota import UserQuotaSummary


def utc_now() -> datetime:
    return datetime.now(UTC)


class Usage(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)
    billable_units: float | None = Field(default=None, ge=0)
    billable_unit_type: Literal[
        "tokens",
        "provider_units",
        "input_images",
        "output_images",
        "characters",
        "seconds",
    ] | None = None
    usage_source: Literal["provider", "counted", "estimated", "unknown"] = "unknown"

    @classmethod
    def from_provider(cls, payload: dict[str, Any] | None) -> Usage | None:
        if not payload:
            return None
        input_tokens = payload.get("prompt_tokens", payload.get("input_tokens"))
        output_tokens = payload.get("completion_tokens", payload.get("output_tokens"))
        total_tokens = payload.get("total_tokens")
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = int(input_tokens) + int(output_tokens)
        provider_billable_units = payload.get("billable_units")
        billable_units = (
            provider_billable_units if provider_billable_units is not None else total_tokens
        )
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cache_read_input_tokens=payload.get("cache_read_input_tokens"),
            billable_units=billable_units,
            billable_unit_type=(
                "provider_units" if provider_billable_units is not None else "tokens"
            )
            if billable_units is not None
            else None,
            usage_source="provider",
        )


class ArtifactRef(StrictModel):
    artifact_id: str
    media_type: str
    uri: str
    expires_at: datetime | None = None


class AdapterArtifact(StrictModel):
    media_type: str
    data: bytes | None = Field(default=None, repr=False)
    uri: str | None = None
    filename: str | None = None


class AdapterResponse(StrictModel):
    output: dict[str, Any]
    usage: Usage | None = None
    provider_request_id: str | None = None
    response_model: str | None = None
    finish_reason: str | None = None
    artifacts: list[AdapterArtifact] = []


class AdapterChunk(StrictModel):
    index: int = 0
    delta: dict[str, Any] | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    provider_request_id: str | None = None
    response_model: str | None = None


class AdapterAsyncTask(StrictModel):
    provider_task_id: str
    result_type: str
    estimated_wait_seconds: int | None = None
    provider_payload: dict[str, Any] = {}


class RegistrationResult(StrictModel):
    registration_id: str
    credential_id: str
    credential_name: str
    provider_id: str
    base_url: str
    api_key_masked: str
    scope: CredentialScope
    validation_status: str
    discovered_model_count: int
    configured_model_ids: list[str]
    created_at: datetime = Field(default_factory=utc_now)


class ConfiguredModelRegistrationResult(StrictModel):
    configured_model_id: str
    credential_id: str
    provider_id: str
    model: str
    model_type: ModelType
    created_at: datetime = Field(default_factory=utc_now)


class CredentialSummary(StrictModel):
    credential_id: str
    name: str
    scope: CredentialScope
    api_key_masked: str


class ProviderSummary(StrictModel):
    plugin_id: str
    provider_id: str
    display_name: str


class ConfiguredModelItem(StrictModel):
    configured_model_id: str
    provider: ProviderSummary
    model: str
    label: str | None = None
    model_type: ModelType
    categories: set[ModelCategory]
    input_modalities: set[str]
    output_modalities: set[str]
    features: set[str]
    operations: set[str]
    context_window: int | None = None
    max_output_tokens: int | None = None
    credential: CredentialSummary
    status: ModelStatus
    model_enabled: bool = True
    provider_enabled: bool = True
    provider_type: ProviderType = ProviderType.CUSTOM
    user_quota_status: UserQuotaStatus | None = None
    user_quota_remaining: Decimal | None = None
    is_default: bool = False


class ModelListResult(StrictModel):
    items: list[ConfiguredModelItem]
    next_page_token: str | None = None
    default_models: dict[ModelType, str | None] = Field(default_factory=dict)
    user_quota: UserQuotaSummary | None = None


class TenantDefaultModelsResult(StrictModel):
    tenant_id: str
    defaults: dict[ModelType, str | None]


class ConfiguredModelAvailabilityResult(StrictModel):
    tenant_id: str
    configured_model_id: str
    enabled: bool
    provider_enabled: bool
    status: ModelStatus


class ProviderAvailabilityResult(StrictModel):
    tenant_id: str
    provider: ProviderRef
    enabled: bool


class InvocationResult(StrictModel):
    invocation_id: str
    session_id: str | None = None
    query_id: str | None = None
    status: InvocationStatus = InvocationStatus.SUCCEEDED
    output: dict[str, Any]
    artifacts: list[ArtifactRef] = []
    usage: Usage | None = None
    provider_request_id: str | None = None
    response_model: str | None = None
    finish_reason: str | None = None
    latency_ms: int


class AsyncInvocationResult(StrictModel):
    invocation_id: str
    session_id: str | None = None
    query_id: str | None = None
    status: InvocationStatus = InvocationStatus.ACCEPTED
    task_id: str
    result_type: str
    estimated_wait_seconds: int | None = None


class StreamEvent(StrictModel):
    event: Literal[
        "response.created",
        "output.delta",
        "usage",
        "response.completed",
        "error",
    ]
    data: dict[str, Any]


class ResponseEnvelope(StrictModel):
    protocol_version: str = PROTOCOL_VERSION
    request_id: str
    trace_id: str | None = None
    data: Any | None = None
    error: dict[str, Any] | None = None
