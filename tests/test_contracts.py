from __future__ import annotations

import logging

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


def test_omitted_model_selector_uses_operation_default_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="model_access.contracts.invocation"):
        request = ModelInvocationRequest.model_validate(
            {
                "context": {
                    "tenant_id": "tenant",
                    "user_id": "user",
                    "request_id": "request_omitted",
                },
                "operation": "chat",
                "response_mode": "blocking",
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "private-prompt-content"}],
                        }
                    ]
                },
            }
        )
    assert request.model is not None
    assert request.model.model_type == ModelType.TEXT_GENERATION
    assert request.model.uses_default is True
    record = caplog.records[-1]
    assert record.model_access_event == "tenant_default_model_fallback"  # type: ignore[attr-defined]
    assert record.model_fallback_reason == "model_omitted"  # type: ignore[attr-defined]
    assert record.operation == "chat"  # type: ignore[attr-defined]
    assert record.tenant_id == "tenant"  # type: ignore[attr-defined]
    assert record.request_id == "request_omitted"  # type: ignore[attr-defined]
    assert "private-prompt-content" not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="model_access.contracts.invocation"):
        empty_selector = ModelInvocationRequest.model_validate(
            {
                "context": {"tenant_id": "tenant", "user_id": "user"},
                "model": {},
                "operation": "embeddings",
                "input": {"texts": ["hello"]},
            }
        )
    assert empty_selector.model is not None
    assert empty_selector.model.model_type == ModelType.EMBEDDING
    assert empty_selector.model.uses_default is True
    assert caplog.records[-1].model_fallback_reason == "model_empty_object"  # type: ignore[attr-defined]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="model_access.contracts.invocation"):
        null_selector = ModelInvocationRequest.model_validate(
            {
                "context": {"tenant_id": "tenant", "user_id": "user"},
                "model": None,
                "operation": "chat",
                "input": {
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
                },
            }
        )
    assert null_selector.model is not None
    assert null_selector.model.uses_default is True
    assert caplog.records[-1].model_fallback_reason == "model_null"  # type: ignore[attr-defined]


def test_explicit_model_selector_does_not_log_default_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="model_access.contracts.invocation"):
        ModelInvocationRequest.model_validate(
            {
                "context": {"tenant_id": "tenant", "user_id": "user"},
                "model": {
                    "configured_model_id": "cm_1",
                    "model_type": "text_generation",
                },
                "operation": "chat",
                "input": {
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
                },
            }
        )
    assert not [
        record
        for record in caplog.records
        if getattr(record, "model_access_event", None) == "tenant_default_model_fallback"
    ]


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
