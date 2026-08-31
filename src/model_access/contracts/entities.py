from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator

from .common import NamespacedExtensions, StrictModel, _reject_sensitive_keys
from .enums import (
    AuthType,
    CredentialScope,
    CredentialSourceType,
    DeploymentMode,
    DeploymentProtocol,
    DiscoveryMode,
    ModelCategory,
    ModelOperation,
    ModelStatus,
    ModelType,
    ResponseMode,
)


class ProviderRef(StrictModel):
    plugin_id: str = Field(min_length=1, max_length=160)
    provider_id: str = Field(min_length=1, max_length=80)
    version: str | None = Field(default=None, max_length=40)

    @classmethod
    def parse(cls, value: str) -> ProviderRef:
        if "/" not in value:
            raise ValueError("provider must use plugin_id/provider_id format")
        plugin_id, provider_id = value.rsplit("/", 1)
        return cls(plugin_id=plugin_id, provider_id=provider_id)

    def to_legacy_string(self) -> str:
        return f"{self.plugin_id}/{self.provider_id}"

    @property
    def key(self) -> str:
        return self.to_legacy_string()


class I18nObject(StrictModel):
    zh_Hans: str | None = None
    en_US: str | None = None
    default: str


class ProviderCapabilities(StrictModel):
    supports_streaming: bool = False
    supports_tools: bool = False
    supports_vision: bool = False
    supports_json_schema: bool = False
    supports_system_message: bool = True
    supports_token_counting: bool = False
    supports_polling: bool = False
    supports_multimodal: bool = False


class CredentialFormField(StrictModel):
    name: str
    label: I18nObject
    type: Literal["text", "secret", "select", "boolean"] = "text"
    required: bool = True
    secret: bool = False
    options: list[str] = []


class CredentialFormSchema(StrictModel):
    fields: list[CredentialFormField] = []


class ModelDescriptor(StrictModel):
    provider: ProviderRef
    model: str = Field(min_length=1, max_length=256)
    model_type: ModelType
    label: str | None = None
    status: ModelStatus = ModelStatus.ACTIVE
    features: set[str] = set()
    properties: dict[str, Any] = {}
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    parameter_schema: dict[str, Any] | None = None
    credential_scope: Literal["provider", "model"] = "provider"
    input_modalities: set[Literal["text", "image", "audio", "video"]] = {"text"}
    output_modalities: set[Literal["text", "json", "vector", "image", "audio", "video"]] = {"text"}
    categories: set[ModelCategory] = set()
    operations: set[ModelOperation] = set()
    protocol_versions: set[str] = {"1.1"}

    @model_validator(mode="after")
    def add_derived_categories_and_operations(self) -> ModelDescriptor:
        category_map = {
            ModelType.TEXT_GENERATION: ModelCategory.TEXT_MODEL,
            ModelType.EMBEDDING: ModelCategory.VECTOR_MODEL,
            ModelType.RERANK: ModelCategory.VECTOR_MODEL,
            ModelType.SPEECH_TO_TEXT: ModelCategory.SPEECH_MODEL,
            ModelType.TEXT_TO_SPEECH: ModelCategory.SPEECH_MODEL,
            ModelType.IMAGE_GENERATION: ModelCategory.IMAGE_GENERATION_MODEL,
            ModelType.VIDEO_GENERATION: ModelCategory.VIDEO_GENERATION_MODEL,
            ModelType.MODERATION: ModelCategory.SAFETY_MODEL,
        }
        operation_map = {
            ModelType.TEXT_GENERATION: {ModelOperation.CHAT, ModelOperation.TEXT_COMPLETION},
            ModelType.EMBEDDING: {ModelOperation.EMBEDDINGS},
            ModelType.RERANK: {ModelOperation.RERANK},
            ModelType.SPEECH_TO_TEXT: {ModelOperation.TRANSCRIBE},
            ModelType.TEXT_TO_SPEECH: {ModelOperation.SYNTHESIZE},
            ModelType.IMAGE_GENERATION: {ModelOperation.IMAGE_GENERATE},
            ModelType.VIDEO_GENERATION: {ModelOperation.VIDEO_GENERATE},
            ModelType.MODERATION: {ModelOperation.MODERATE},
        }
        self.categories.add(category_map[self.model_type])
        if self.model_type == ModelType.TEXT_GENERATION and "image" in self.input_modalities:
            self.categories.add(ModelCategory.VISION_MODEL)
        if not self.operations:
            object.__setattr__(self, "operations", operation_map[self.model_type])
        return self


class ProviderDescriptor(StrictModel):
    provider: ProviderRef
    display_name: I18nObject
    icon_small: I18nObject | None = None
    icon_small_dark: I18nObject | None = None
    supported_model_types: list[ModelType]
    capabilities: ProviderCapabilities = ProviderCapabilities()
    provider_credential_schema: CredentialFormSchema | None = None
    model_credential_schema: CredentialFormSchema | None = None
    models: list[ModelDescriptor] | None = None
    dynamic_model_discovery: bool = False
    protocol_version: str = "1.1"
    enabled: bool = True


class CredentialSet(StrictModel):
    provider: ProviderRef
    model: str | None = None
    model_type: ModelType | None = None
    values: dict[str, Any] = Field(repr=False)
    credential_id: str | None = None
    source: CredentialSourceType
    version: str | None = None

    def __repr_args__(self):  # type: ignore[no-untyped-def]
        for name, value in super().__repr_args__():
            if name == "values":
                yield name, "***"
            else:
                yield name, value


class CredentialValidationResult(StrictModel):
    valid: bool
    error_code: str | None = None
    message: str | None = None
    normalized_credentials: dict[str, Any] | None = Field(default=None, repr=False)
    expires_at: datetime | None = None


class ExecutionContext(StrictModel):
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    task_id: str | None = None


class RuntimeContext(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=160)
    query_id: str | None = Field(default=None, max_length=160)
    parent_query_id: str | None = Field(default=None, max_length=160)
    app_id: str | None = Field(default=None, max_length=128)
    execution: ExecutionContext | None = None
    invocation_id: str | None = Field(default=None, max_length=160)
    request_id: str | None = Field(default=None, max_length=160)
    traceparent: str | None = Field(default=None, max_length=256)
    tracestate: str | None = Field(default=None, max_length=512)
    extensions: dict[str, dict[str, Any]] = {}

    @field_validator("extensions")
    @classmethod
    def validate_extensions(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return NamespacedExtensions(values=value).values


class RequestMetadata(StrictModel):
    scene: str | None = Field(default=None, max_length=80)
    node_id: str | None = Field(default=None, max_length=160)
    response_mode: ResponseMode = ResponseMode.BLOCKING
    idempotency_key: str | None = Field(default=None, max_length=256)
    tags: dict[str, str] = {}

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32:
            raise ValueError("metadata tags must contain at most 32 entries")
        _reject_sensitive_keys(value)
        if any(len(k) > 64 or len(v) > 256 for k, v in value.items()):
            raise ValueError("metadata tag is too large")
        return value


class SelfHostedEndpoint(StrictModel):
    endpoint_id: str = Field(min_length=1, max_length=128)
    base_url: str
    weight: int = Field(default=100, ge=1, le=10000)
    zone: str | None = Field(default=None, max_length=80)
    enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def validate_base_url_shape(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must contain http(s) scheme and host")
        if parsed.username or parsed.password:
            raise ValueError("base_url must not contain user info")
        return value.rstrip("/")


class SelfHostedHealthCheck(StrictModel):
    path: str = "/health"
    method: Literal["GET", "POST", "grpc_health"] = "GET"
    interval_seconds: int = Field(default=15, ge=1, le=3600)
    timeout_ms: int = Field(default=2000, ge=100, le=60000)
    unhealthy_threshold: int = Field(default=3, ge=1, le=100)
    healthy_threshold: int = Field(default=2, ge=1, le=100)


class SelfHostedDeployment(StrictModel):
    deployment_mode: DeploymentMode
    protocol: DeploymentProtocol
    endpoints: list[SelfHostedEndpoint] = Field(min_length=1)
    model_name: str = Field(min_length=1, max_length=256)
    auth_type: AuthType = AuthType.NONE
    credential_id: str | None = None
    tls_verify: bool = True
    ca_cert_ref: str | None = None
    discovery_mode: DiscoveryMode = DiscoveryMode.MANUAL
    health_check: SelfHostedHealthCheck = SelfHostedHealthCheck()
    connect_timeout_ms: int = Field(default=3000, ge=100, le=60000)
    request_timeout_ms: int = Field(default=120000, ge=1000, le=3600000)
    max_concurrency: int | None = Field(default=None, ge=1)
    deployment_version: int = Field(default=1, ge=1)
    cluster_id: str | None = Field(default=None, max_length=128)
    extensions: dict[str, dict[str, Any]] = {}


class CredentialInput(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    base_url: str
    api_key: SecretStr = Field(repr=False)
    scope: CredentialScope

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must contain http(s) scheme and host")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("base_url must not contain user info or fragment")
        return value.rstrip("/")


class CallerIdentity(StrictModel):
    tenant_id: str
    user_id: str | None = None
    roles: set[str] = set()
    is_service: bool = False

    @property
    def is_admin(self) -> bool:
        return bool(self.roles & {"tenant_admin", "model_admin", "system_admin"})
