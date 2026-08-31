from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .contracts.entities import (
    CallerIdentity,
    CredentialSet,
    CredentialValidationResult,
    ModelDescriptor,
    ProviderDescriptor,
    ProviderRef,
    RuntimeContext,
)
from .contracts.invocation import AdapterInvocation
from .contracts.quota import UserQuotaAllocation, UserQuotaSummary
from .contracts.responses import (
    AdapterAsyncTask,
    AdapterChunk,
    AdapterResponse,
    ArtifactRef,
    Usage,
)

AdapterInvocationResult = AdapterResponse | AdapterAsyncTask | AsyncIterator[AdapterChunk]


@runtime_checkable
class ProviderAdapter(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    async def validate_credentials(
        self,
        *,
        context: RuntimeContext,
        provider: ProviderRef,
        credentials: CredentialSet,
    ) -> CredentialValidationResult: ...

    async def discover_models(
        self,
        *,
        context: RuntimeContext,
        provider: ProviderRef,
        credentials: CredentialSet,
        deployment: dict[str, Any] | None = None,
    ) -> Sequence[ModelDescriptor]: ...

    async def invoke(self, invocation: AdapterInvocation) -> AdapterInvocationResult: ...


class CredentialCipher(Protocol):
    def encrypt(self, values: dict[str, Any]) -> str: ...

    def decrypt(self, ciphertext: str) -> dict[str, Any]: ...


class SecretResolver(Protocol):
    def resolve(self, reference: str) -> str: ...


@dataclass(frozen=True, slots=True)
class ResolvedFile:
    filename: str
    media_type: str
    data: bytes


class FileResolver(Protocol):
    async def resolve(
        self, file_id: str, identity: CallerIdentity | None = None
    ) -> ResolvedFile: ...


class ArtifactStore(Protocol):
    async def put_bytes(
        self,
        *,
        data: bytes,
        media_type: str,
        filename: str | None,
        tenant_id: str,
        user_id: str | None,
    ) -> ArtifactRef: ...

    async def register_uri(
        self,
        *,
        uri: str,
        media_type: str,
        tenant_id: str,
        user_id: str | None,
    ) -> ArtifactRef: ...


class TaskBackend(Protocol):
    async def register_external_task(
        self,
        *,
        invocation_id: str,
        provider_task_id: str,
        result_type: str,
        tenant_id: str,
        user_id: str | None,
        trace_context: dict[str, str],
        provider_payload: dict[str, Any],
    ) -> str: ...


class QuotaManager(Protocol):
    async def reserve(
        self,
        *,
        invocation_id: str,
        tenant_id: str,
        configured_model_id: str,
        operation: str,
    ) -> None: ...

    async def settle(self, *, invocation_id: str, usage: Usage | None, succeeded: bool) -> None: ...


@runtime_checkable
class UserQuotaAwareManager(QuotaManager, Protocol):
    async def acquire_user_quota(
        self,
        *,
        invocation_id: str,
        tenant_id: str,
        user_id: str,
        roles: set[str],
        configured_model_id: str,
        operation: str,
        estimated_usage: Usage,
    ) -> UserQuotaAllocation: ...

    def get_summary(
        self,
        *,
        tenant_id: str,
        user_id: str,
        roles: set[str],
        identity: CallerIdentity,
    ) -> UserQuotaSummary: ...


class Observability(Protocol):
    def start_span(
        self, name: str, attributes: dict[str, Any], *, kind: str = "internal"
    ) -> Any: ...

    def current_trace_id(self) -> str | None: ...

    def record_invocation(
        self,
        *,
        provider: str,
        model_type: str,
        status: str,
        duration_seconds: float,
        usage: Usage | None,
    ) -> None: ...
