from __future__ import annotations

import logging
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from .common import PROTOCOL_VERSION, NamespacedExtensions, StrictModel, ensure_protocol_version
from .entities import ProviderRef, RequestMetadata, RuntimeContext, SelfHostedDeployment
from .enums import (
    ModelCategory,
    ModelOperation,
    ModelStatus,
    ModelType,
    ResponseMode,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL_FALLBACK_EVENT = "tenant_default_model_fallback"
MODEL_OMITTED = "model_omitted"
MODEL_NULL = "model_null"
MODEL_EMPTY_OBJECT = "model_empty_object"


def _field(container: Any, name: str) -> Any:
    if isinstance(container, dict):
        return container.get(name)
    return getattr(container, name, None)


def _safe_log_value(value: Any, *, max_length: int = 256) -> str | None:
    """Return a bounded, single-line scalar suitable for diagnostic logs."""
    if value is None:
        return None
    if isinstance(value, Enum):
        value = value.value
    if not isinstance(value, (str, int, float, bool)):
        return None
    return str(value).replace("\r", "\\r").replace("\n", "\\n")[:max_length]


def _missing_model_reason(data: dict[str, Any]) -> str | None:
    if "model" not in data:
        return MODEL_OMITTED
    if data["model"] is None:
        return MODEL_NULL
    if data["model"] == {}:
        return MODEL_EMPTY_OBJECT
    return None


def _log_default_model_fallback(
    data: dict[str, Any], operation: ModelOperation, reason: str
) -> None:
    context = data.get("context")
    metadata = data.get("metadata")
    fields = {
        "model_access_event": DEFAULT_MODEL_FALLBACK_EVENT,
        "model_fallback_reason": reason,
        "operation": operation.value,
        "tenant_id": _safe_log_value(_field(context, "tenant_id")),
        "user_id": _safe_log_value(_field(context, "user_id")),
        "app_id": _safe_log_value(_field(context, "app_id")),
        "session_id": _safe_log_value(_field(context, "session_id")),
        "query_id": _safe_log_value(_field(context, "query_id")),
        "request_id": _safe_log_value(_field(context, "request_id")),
        "invocation_id": _safe_log_value(_field(context, "invocation_id")),
        "invocation_scene": _safe_log_value(_field(metadata, "scene")),
    }
    logger.warning(
        "tenant default model fallback triggered: reason=%s operation=%s "
        "tenant_id=%s user_id=%s app_id=%s session_id=%s query_id=%s "
        "request_id=%s invocation_id=%s scene=%s",
        fields["model_fallback_reason"],
        fields["operation"],
        fields["tenant_id"],
        fields["user_id"],
        fields["app_id"],
        fields["session_id"],
        fields["query_id"],
        fields["request_id"],
        fields["invocation_id"],
        fields["invocation_scene"],
        extra=fields,
    )


class TextContentPart(StrictModel):
    type: Literal["text"]
    text: str


class ImageContentPart(StrictModel):
    type: Literal["image"]
    file_id: str | None = None
    uri: str | None = None
    detail: Literal["auto", "low", "high"] | None = None

    @model_validator(mode="after")
    def exactly_one_source(self) -> ImageContentPart:
        if bool(self.file_id) == bool(self.uri):
            raise ValueError("image content requires exactly one of file_id or uri")
        return self


class AudioContentPart(StrictModel):
    type: Literal["audio"]
    file_id: str
    format: str | None = None


ContentPart = Annotated[
    TextContentPart | ImageContentPart | AudioContentPart,
    Field(discriminator="type"),
]


class PromptMessage(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: list[ContentPart]
    name: str | None = None
    tool_call_id: str | None = None


class ToolDefinition(StrictModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any]


class ChatInput(StrictModel):
    messages: list[PromptMessage] = Field(min_length=1, max_length=1000)
    tools: list[ToolDefinition] | None = None
    tool_choice: str | dict[str, Any] | None = None
    stop: list[str] | None = None


class CompletionInput(StrictModel):
    prompt: str
    stop: list[str] | None = None


class EmbeddingInput(StrictModel):
    texts: list[str] = Field(min_length=1, max_length=2048)
    input_type: Literal["document", "query"] = "document"
    dimensions: int | None = Field(default=None, ge=1)


class RerankInput(StrictModel):
    query: str
    documents: list[str] = Field(min_length=1, max_length=4096)
    top_n: int | None = Field(default=None, ge=1)
    score_threshold: float | None = None


class TranscriptionInput(StrictModel):
    file_id: str
    language: str | None = None
    timestamps: bool = False


class SynthesisInput(StrictModel):
    text: str
    voice: str = ""
    format: str = "mp3"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)


class ImageGenerationInput(StrictModel):
    prompt: str
    negative_prompt: str | None = None
    size: str | None = None
    count: int = Field(default=1, ge=1, le=10)
    reference_file_ids: list[str] = []


class VideoGenerationInput(StrictModel):
    prompt: str
    image_file_id: str | None = None
    reference_file_ids: list[str] = []
    duration: int | None = Field(default=None, ge=1, le=3600)
    resolution: str | None = None


class ModerationInput(StrictModel):
    input: str | list[str]
    policy: str | None = None


InvocationInput = (
    ChatInput
    | CompletionInput
    | EmbeddingInput
    | RerankInput
    | TranscriptionInput
    | SynthesisInput
    | ImageGenerationInput
    | VideoGenerationInput
    | ModerationInput
)

INPUT_BY_OPERATION: dict[ModelOperation, type[StrictModel]] = {
    ModelOperation.CHAT: ChatInput,
    ModelOperation.TEXT_COMPLETION: CompletionInput,
    ModelOperation.EMBEDDINGS: EmbeddingInput,
    ModelOperation.RERANK: RerankInput,
    ModelOperation.TRANSCRIBE: TranscriptionInput,
    ModelOperation.SYNTHESIZE: SynthesisInput,
    ModelOperation.IMAGE_GENERATE: ImageGenerationInput,
    ModelOperation.VIDEO_GENERATE: VideoGenerationInput,
    ModelOperation.MODERATE: ModerationInput,
}

MODEL_TYPE_BY_OPERATION: dict[ModelOperation, ModelType] = {
    ModelOperation.CHAT: ModelType.TEXT_GENERATION,
    ModelOperation.TEXT_COMPLETION: ModelType.TEXT_GENERATION,
    ModelOperation.EMBEDDINGS: ModelType.EMBEDDING,
    ModelOperation.RERANK: ModelType.RERANK,
    ModelOperation.TRANSCRIBE: ModelType.SPEECH_TO_TEXT,
    ModelOperation.SYNTHESIZE: ModelType.TEXT_TO_SPEECH,
    ModelOperation.IMAGE_GENERATE: ModelType.IMAGE_GENERATION,
    ModelOperation.VIDEO_GENERATE: ModelType.VIDEO_GENERATION,
    ModelOperation.MODERATE: ModelType.MODERATION,
}

ALLOWED_RESPONSE_MODES: dict[ModelOperation, set[ResponseMode]] = {
    ModelOperation.CHAT: {ResponseMode.BLOCKING, ResponseMode.STREAMING},
    ModelOperation.TEXT_COMPLETION: {ResponseMode.BLOCKING, ResponseMode.STREAMING},
    ModelOperation.EMBEDDINGS: {ResponseMode.BLOCKING},
    ModelOperation.RERANK: {ResponseMode.BLOCKING},
    ModelOperation.TRANSCRIBE: {ResponseMode.BLOCKING, ResponseMode.ASYNC},
    ModelOperation.SYNTHESIZE: {ResponseMode.BLOCKING, ResponseMode.STREAMING, ResponseMode.ASYNC},
    ModelOperation.IMAGE_GENERATE: {ResponseMode.BLOCKING, ResponseMode.ASYNC},
    ModelOperation.VIDEO_GENERATE: {ResponseMode.ASYNC},
    ModelOperation.MODERATE: {ResponseMode.BLOCKING},
}


class ModelRegistrationOptions(StrictModel):
    validate_credentials: bool = True
    discover_models: bool = True
    enable_discovered_models: bool = True


class ManualModelRegistration(StrictModel):
    model: str
    model_type: ModelType
    label: str | None = None
    input_modalities: set[Literal["text", "image", "audio", "video"]] = {"text"}
    output_modalities: set[Literal["text", "json", "vector", "image", "audio", "video"]] = {"text"}
    operations: set[ModelOperation] = set()
    features: set[str] = set()
    categories: set[ModelCategory] = set()
    context_window: int | None = None
    max_output_tokens: int | None = None


class ModelRegistrationRequest(StrictModel):
    tenant_id: str
    user_id: str
    provider: ProviderRef
    credential: CredentialInput
    deployment: SelfHostedDeployment | None = None
    model: ManualModelRegistration | None = None
    options: ModelRegistrationOptions = ModelRegistrationOptions()

    @model_validator(mode="after")
    def validate_manual_self_hosted(self) -> ModelRegistrationRequest:
        if self.deployment and self.deployment.discovery_mode.value == "MANUAL" and not self.model:
            raise ValueError("manual self-hosted registration requires model")
        return self


from .entities import CredentialInput  # noqa: E402  (resolves pydantic forward reference)

ModelRegistrationRequest.model_rebuild()


class ModelListQuery(StrictModel):
    tenant_id: str
    user_id: str
    category: ModelCategory | None = None
    model_type: ModelType | None = None
    provider_id: str | None = None
    status: ModelStatus | None = ModelStatus.ACTIVE
    page_size: int = Field(default=50, ge=1, le=200)
    page_token: str | None = None


class TenantDefaultModelUpdateRequest(StrictModel):
    tenant_id: str
    configured_model_id: str | None = Field(default=None, min_length=1, max_length=64)


class ModelSelector(StrictModel):
    configured_model_id: str | None = None
    provider: ProviderRef | None = None
    model: str | None = None
    model_type: ModelType
    use_default: bool = False

    @model_validator(mode="after")
    def validate_selector(self) -> ModelSelector:
        if bool(self.provider) != bool(self.model):
            raise ValueError("provider and model must be supplied together")
        explicit = bool(self.provider and self.model)
        selected_modes = sum([bool(self.configured_model_id), explicit, self.use_default])
        if selected_modes > 1:
            raise ValueError(
                "provide at most one of configured_model_id, provider + model, or use_default"
            )
        return self

    @property
    def uses_default(self) -> bool:
        return self.use_default or not (self.configured_model_id or (self.provider and self.model))


class ModelInvocationRequest(StrictModel):
    protocol_version: str = PROTOCOL_VERSION
    context: RuntimeContext
    model: ModelSelector | None = None
    operation: ModelOperation
    response_mode: ResponseMode = ResponseMode.BLOCKING
    input: InvocationInput
    parameters: dict[str, Any] = {}
    provider_options: dict[str, dict[str, Any]] = {}
    metadata: RequestMetadata = RequestMetadata()
    extensions: dict[str, dict[str, Any]] = {}

    @model_validator(mode="before")
    @classmethod
    def parse_operation_input(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        operation = ModelOperation(data.get("operation"))
        missing_model_reason = _missing_model_reason(data)
        if missing_model_reason:
            _log_default_model_fallback(data, operation, missing_model_reason)
        input_model = INPUT_BY_OPERATION[operation]
        data = dict(data)
        data["input"] = input_model.model_validate(data.get("input", {}))
        if missing_model_reason:
            data["model"] = {"model_type": MODEL_TYPE_BY_OPERATION[operation]}
        return data

    @field_validator("protocol_version")
    @classmethod
    def validate_protocol_version(cls, value: str) -> str:
        return ensure_protocol_version(value)

    @model_validator(mode="after")
    def validate_discriminated_contract(self) -> ModelInvocationRequest:
        expected_input = INPUT_BY_OPERATION[self.operation]
        if not isinstance(self.input, expected_input):
            raise ValueError(f"operation {self.operation} requires {expected_input.__name__}")
        expected_model_type = MODEL_TYPE_BY_OPERATION[self.operation]
        if self.model is None:
            object.__setattr__(self, "model", ModelSelector(model_type=expected_model_type))
        assert self.model is not None
        if self.model.model_type != expected_model_type:
            raise ValueError(
                f"operation {self.operation} requires model_type {expected_model_type}, "
                f"got {self.model.model_type}"
            )
        if self.response_mode not in ALLOWED_RESPONSE_MODES[self.operation]:
            raise ValueError(
                f"response_mode {self.response_mode} is not valid for {self.operation}"
            )
        if self.metadata.scene == "conversation" and not (
            self.context.session_id and self.context.query_id
        ):
            raise ValueError("conversation scene requires session_id and query_id")
        NamespacedExtensions(values=self.extensions)
        return self


class AdapterInvocation(StrictModel):
    context: RuntimeContext
    provider: ProviderRef
    model: str
    model_type: ModelType
    operation: ModelOperation
    response_mode: ResponseMode
    input: InvocationInput
    parameters: dict[str, Any] = {}
    provider_options: dict[str, Any] = {}
    credential_values: dict[str, Any] = Field(repr=False)
    configured_model_id: str
    credential_id: str
    deployment: SelfHostedDeployment | None = None
    endpoint_id: str | None = None
