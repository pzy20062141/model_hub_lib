from __future__ import annotations

import pytest
from pydantic import ValidationError

from model_access.contracts.entities import ProviderRef, RequestMetadata, RuntimeContext
from model_access.contracts.enums import ModelOperation, ModelType, ResponseMode
from model_access.contracts.invocation import ModelInvocationRequest


def test_provider_ref_roundtrip() -> None:
    provider = ProviderRef.parse("langgenius/openai/openai")
    assert provider.plugin_id == "langgenius/openai"
    assert provider.provider_id == "openai"
    assert provider.to_legacy_string() == "langgenius/openai/openai"


def test_unknown_core_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        RuntimeContext(tenant_id="tenant", unexpected="value")  # type: ignore[call-arg]


def test_sensitive_metadata_is_rejected() -> None:
    with pytest.raises(ValidationError, match="sensitive"):
        RequestMetadata(tags={"api_key": "should-not-be-here"})


def test_operation_and_model_type_must_match() -> None:
    with pytest.raises(ValidationError, match="requires model_type embedding"):
        ModelInvocationRequest.model_validate(
            {
                "context": {"tenant_id": "tenant"},
                "model": {
                    "configured_model_id": "cm_1",
                    "model_type": ModelType.TEXT_GENERATION,
                },
                "operation": ModelOperation.EMBEDDINGS,
                "response_mode": ResponseMode.BLOCKING,
                "input": {"texts": ["hello"]},
            }
        )


def test_conversation_context_is_conditional() -> None:
    with pytest.raises(ValidationError, match="session_id and query_id"):
        ModelInvocationRequest.model_validate(
            {
                "context": {"tenant_id": "tenant", "user_id": "user"},
                "model": {
                    "configured_model_id": "cm_1",
                    "model_type": "text_generation",
                },
                "operation": "chat",
                "response_mode": "blocking",
                "input": {
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
                },
                "metadata": {"scene": "conversation"},
            }
        )
