from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

PROTOCOL_VERSION = "1.1"
SUPPORTED_PROTOCOL_MAJOR = 1
EXTENSION_NAMESPACE_PATTERN = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$")
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|secret|token|private[_-]?key)",
    re.IGNORECASE,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class NamespacedExtensions(StrictModel):
    values: dict[str, dict[str, Any]] = {}

    @field_validator("values")
    @classmethod
    def validate_extensions(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        for namespace, payload in value.items():
            if not EXTENSION_NAMESPACE_PATTERN.fullmatch(namespace):
                raise ValueError(f"invalid extension namespace: {namespace}")
            _reject_sensitive_keys(payload)
        if len(str(value).encode("utf-8")) > 8192:
            raise ValueError("extensions must not exceed 8 KiB")
        return value


def _reject_sensitive_keys(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if SENSITIVE_KEY_PATTERN.search(str(key)):
                raise ValueError(f"sensitive field is not allowed in metadata: {child_path}")
            _reject_sensitive_keys(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_keys(child, f"{path}[{index}]")


def ensure_protocol_version(version: str) -> str:
    try:
        major = int(version.split(".", 1)[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("protocol_version must use major.minor format") from exc
    if major != SUPPORTED_PROTOCOL_MAJOR:
        raise ValueError(f"unsupported protocol version: {version}")
    return version
