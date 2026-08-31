from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .contracts.enums import ErrorCode, ModelType

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|authorization|password|secret|token)\s*[:=]\s*[^\s,;}]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~-]+"),
]


def sanitize_message(message: str) -> str:
    sanitized = message
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized[:2000]


@dataclass(slots=True)
class ModelAccessException(Exception):
    code: ErrorCode
    message: str
    retryable: bool = False
    provider: str | None = None
    model: str | None = None
    model_type: ModelType | None = None
    field: str | None = None
    provider_error_code: str | None = None
    extensions: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.message = sanitize_message(self.message)
        Exception.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }
        details = {
            "provider": self.provider,
            "model": self.model,
            "model_type": self.model_type.value if self.model_type else None,
            "field": self.field,
            "provider_error_code": self.provider_error_code,
        }
        result["details"] = [{key: value for key, value in details.items() if value is not None}]
        if self.extensions:
            result["extensions"] = self.extensions
        return result


def request_validation_error(exc: ValidationError | ValueError) -> ModelAccessException:
    field = None
    if isinstance(exc, ValidationError) and exc.errors():
        field = ".".join(str(part) for part in exc.errors()[0].get("loc", ())) or None
    return ModelAccessException(
        code=ErrorCode.REQUEST_INVALID,
        message=sanitize_message(str(exc)),
        field=field,
    )


HTTP_STATUS_BY_ERROR: dict[ErrorCode, int] = {
    ErrorCode.REQUEST_INVALID: 400,
    ErrorCode.CONTEXT_INVALID: 400,
    ErrorCode.MODEL_TYPE_MISMATCH: 400,
    ErrorCode.PROTOCOL_VERSION_UNSUPPORTED: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.PROVIDER_NOT_FOUND: 404,
    ErrorCode.MODEL_NOT_FOUND: 404,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.MODEL_DISABLED: 409,
    ErrorCode.CREDENTIAL_REQUIRED: 422,
    ErrorCode.CREDENTIAL_INVALID: 422,
    ErrorCode.MODEL_UNSUPPORTED: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.QUOTA_EXCEEDED: 429,
    ErrorCode.PROVIDER_BAD_RESPONSE: 502,
    ErrorCode.PROVIDER_UNAVAILABLE: 503,
    ErrorCode.PROVIDER_TIMEOUT: 504,
}
