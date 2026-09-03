from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from ..contracts.entities import (
    CredentialSet,
    CredentialValidationResult,
    ModelDescriptor,
    ProviderDescriptor,
    ProviderRef,
    RuntimeContext,
)
from ..contracts.enums import ModelOperation, ResponseMode
from ..contracts.invocation import AdapterInvocation, EmbeddingInput, RerankInput
from ..contracts.responses import AdapterAsyncTask, AdapterChunk, AdapterResponse, Usage


class MockProviderAdapter:
    """Deterministic adapter for local development and contract tests."""

    def __init__(self, descriptor: ProviderDescriptor, models: Sequence[ModelDescriptor]):
        self._descriptor = descriptor.model_copy(update={"models": list(models)})
        self._models = list(models)
        self.invocations: list[AdapterInvocation] = []

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def validate_credentials(
        self,
        *,
        context: RuntimeContext,
        provider: ProviderRef,
        credentials: CredentialSet,
    ) -> CredentialValidationResult:
        del context, provider
        valid = credentials.values.get("api_key") != "invalid"
        return CredentialValidationResult(
            valid=valid,
            error_code=None if valid else "CREDENTIAL_INVALID",
            message=None if valid else "invalid mock credential",
        )

    async def discover_models(
        self,
        *,
        context: RuntimeContext,
        provider: ProviderRef,
        credentials: CredentialSet,
        deployment: dict[str, Any] | None = None,
    ) -> Sequence[ModelDescriptor]:
        del context, provider, credentials, deployment
        return self._models

    async def invoke(
        self,
        invocation: AdapterInvocation,
    ) -> AdapterResponse | AdapterAsyncTask | AsyncIterator[AdapterChunk]:
        self.invocations.append(invocation)
        if invocation.response_mode == ResponseMode.STREAMING:
            return self._stream()
        if invocation.response_mode == ResponseMode.ASYNC:
            return AdapterAsyncTask(
                provider_task_id="provider_task_mock",
                result_type="video"
                if invocation.operation == ModelOperation.VIDEO_GENERATE
                else "artifact",
                estimated_wait_seconds=1,
            )
        if invocation.operation == ModelOperation.EMBEDDINGS:
            value = invocation.input
            assert isinstance(value, EmbeddingInput)
            output = {
                "type": "vectors",
                "vectors": [[float(len(text)), 1.0] for text in value.texts],
            }
        elif invocation.operation == ModelOperation.RERANK:
            value = invocation.input
            assert isinstance(value, RerankInput)
            output = {
                "type": "ranked_documents",
                "ranked_documents": [
                    {"index": index, "score": 1.0 / (index + 1), "document": document}
                    for index, document in enumerate(value.documents[: value.top_n])
                ],
            }
        else:
            output = {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "mock response"}],
                "finish_reason": "stop",
            }
        return AdapterResponse(
            output=output,
            usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5, usage_source="provider"),
            provider_request_id="mock_request",
            response_model=invocation.model,
            finish_reason=output.get("finish_reason"),
        )

    async def _stream(self) -> AsyncIterator[AdapterChunk]:
        yield AdapterChunk(index=0, delta={"type": "text", "text": "mock "})
        yield AdapterChunk(index=1, delta={"type": "text", "text": "response"})
        yield AdapterChunk(
            index=2,
            usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5, usage_source="provider"),
            finish_reason="stop",
        )
