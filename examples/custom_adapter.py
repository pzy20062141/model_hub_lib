from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from model_access.contracts.entities import (
    CredentialSet,
    CredentialValidationResult,
    ModelDescriptor,
    ProviderDescriptor,
    ProviderRef,
    RuntimeContext,
)
from model_access.contracts.invocation import AdapterInvocation
from model_access.contracts.responses import AdapterAsyncTask, AdapterChunk, AdapterResponse


class CustomProviderAdapter:
    """Native HTTP/gRPC provider adapter skeleton."""

    def __init__(self, descriptor: ProviderDescriptor, models: Sequence[ModelDescriptor]):
        self._descriptor = descriptor
        self._models = list(models)

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
        # Perform the smallest authenticated request. Never log credentials.values.
        return CredentialValidationResult(valid=True)

    async def discover_models(
        self,
        *,
        context: RuntimeContext,
        provider: ProviderRef,
        credentials: CredentialSet,
        deployment: dict[str, Any] | None = None,
    ) -> Sequence[ModelDescriptor]:
        return self._models

    async def invoke(
        self,
        invocation: AdapterInvocation,
    ) -> AdapterResponse | AdapterAsyncTask | AsyncIterator[AdapterChunk]:
        # Convert the standard operation/input to native HTTP or gRPC, then
        # normalize provider output/errors to the standard objects.
        raise NotImplementedError

