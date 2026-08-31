from __future__ import annotations

from enum import StrEnum


class ModelType(StrEnum):
    TEXT_GENERATION = "text_generation"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    SPEECH_TO_TEXT = "speech_to_text"
    TEXT_TO_SPEECH = "text_to_speech"
    IMAGE_GENERATION = "image_generation"
    VIDEO_GENERATION = "video_generation"
    MODERATION = "moderation"


class ModelOperation(StrEnum):
    CHAT = "chat"
    TEXT_COMPLETION = "text_completion"
    EMBEDDINGS = "embeddings"
    RERANK = "rerank"
    TRANSCRIBE = "transcribe"
    SYNTHESIZE = "synthesize"
    IMAGE_GENERATE = "image_generate"
    VIDEO_GENERATE = "video_generate"
    MODERATE = "moderate"


class ModelStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    INVALID_CREDENTIAL = "INVALID_CREDENTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    WARMING_UP = "WARMING_UP"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    NO_CONFIGURE = "NO_CONFIGURE"


class ProviderType(StrEnum):
    SYSTEM = "SYSTEM"
    CUSTOM = "CUSTOM"


class ProviderQuotaType(StrEnum):
    TRIAL = "TRIAL"
    FREE = "FREE"
    PAID = "PAID"


class QuotaUnit(StrEnum):
    TIMES = "TIMES"
    TOKENS = "TOKENS"


class QuotaReservationStatus(StrEnum):
    RESERVED = "RESERVED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


class QuotaPeriodType(StrEnum):
    DAY = "DAY"
    MONTH = "MONTH"


class QuotaOverrideMode(StrEnum):
    INHERIT = "INHERIT"
    LIMITED = "LIMITED"
    UNLIMITED = "UNLIMITED"


class QuotaPolicySource(StrEnum):
    USER = "USER"
    ROLE = "ROLE"
    TENANT_DEFAULT = "TENANT_DEFAULT"
    PLATFORM_DEFAULT = "PLATFORM_DEFAULT"


class UserQuotaStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SOFT_LIMIT = "SOFT_LIMIT"
    EXCEEDED = "EXCEEDED"
    DISABLED = "DISABLED"
    UNLIMITED = "UNLIMITED"


class CredentialScope(StrEnum):
    USER = "USER"
    TENANT = "TENANT"
    SYSTEM = "SYSTEM"


class CredentialSourceType(StrEnum):
    USER = "user"
    TENANT = "tenant"
    SYSTEM = "system"
    SELF_HOSTED = "self_hosted"


class CredentialStatus(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"
    DISABLED = "DISABLED"


class ModelCategory(StrEnum):
    TEXT_MODEL = "TEXT_MODEL"
    VECTOR_MODEL = "VECTOR_MODEL"
    VISION_MODEL = "VISION_MODEL"
    SPEECH_MODEL = "SPEECH_MODEL"
    IMAGE_GENERATION_MODEL = "IMAGE_GENERATION_MODEL"
    VIDEO_GENERATION_MODEL = "VIDEO_GENERATION_MODEL"
    SAFETY_MODEL = "SAFETY_MODEL"


class ResponseMode(StrEnum):
    BLOCKING = "blocking"
    STREAMING = "streaming"
    ASYNC = "async"


class InvocationStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DeploymentMode(StrEnum):
    LOCAL_ENDPOINT = "LOCAL_ENDPOINT"
    PRIVATE_CLOUD = "PRIVATE_CLOUD"
    KUBERNETES_SERVICE = "KUBERNETES_SERVICE"


class DeploymentProtocol(StrEnum):
    OPENAI_COMPATIBLE = "OPENAI_COMPATIBLE"
    NATIVE_HTTP = "NATIVE_HTTP"
    GRPC = "GRPC"


class AuthType(StrEnum):
    NONE = "NONE"
    API_KEY = "API_KEY"
    BEARER = "BEARER"
    MTLS = "MTLS"


class DiscoveryMode(StrEnum):
    MANUAL = "MANUAL"
    ENDPOINT = "ENDPOINT"
    MANIFEST = "MANIFEST"


class ErrorCode(StrEnum):
    PROVIDER_NOT_FOUND = "PROVIDER_NOT_FOUND"
    PROVIDER_DISABLED = "PROVIDER_DISABLED"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    MODEL_DISABLED = "MODEL_DISABLED"
    MODEL_UNSUPPORTED = "MODEL_UNSUPPORTED"
    MODEL_TYPE_MISMATCH = "MODEL_TYPE_MISMATCH"
    CREDENTIAL_REQUIRED = "CREDENTIAL_REQUIRED"
    CREDENTIAL_INVALID = "CREDENTIAL_INVALID"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_BAD_RESPONSE = "PROVIDER_BAD_RESPONSE"
    REQUEST_INVALID = "REQUEST_INVALID"
    CONTEXT_INVALID = "CONTEXT_INVALID"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    PROTOCOL_VERSION_UNSUPPORTED = "PROTOCOL_VERSION_UNSUPPORTED"
    UNAUTHORIZED = "UNAUTHORIZED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
