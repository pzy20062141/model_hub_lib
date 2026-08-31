from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from .adapters.registry import ProviderRegistry
from .contracts.entities import CallerIdentity, ProviderRef
from .contracts.enums import ProviderType
from .contracts.invocation import (
    ModelInvocationRequest,
    ModelListQuery,
    ModelRegistrationRequest,
)
from .contracts.quota import ProviderQuotaPoolInput, ProviderQuotaView
from .contracts.responses import (
    AsyncInvocationResult,
    InvocationResult,
    ModelListResult,
    RegistrationResult,
    StreamEvent,
)
from .control_plane import ModelControlPlaneService
from .infrastructure import InMemoryArtifactStore, InMemoryTaskBackend
from .observability import OpenTelemetryFacade
from .persistence.repository import ModelAccessRepository
from .protocols import ArtifactStore, CredentialCipher, ProviderAdapter, QuotaManager, TaskBackend
from .quota import ConfigurationSourceCache, HostingConfiguration, ManagedQuotaManager
from .runtime import ModelRuntimeService
from .security import FernetCredentialCipher, URLSecurityPolicy


class ModelRepositoryClient:
    """Stable library facade exposed to workflow, agent and application modules."""

    def __init__(
        self,
        *,
        repository: ModelAccessRepository,
        cipher: CredentialCipher,
        providers: ProviderRegistry | None = None,
        url_policy: URLSecurityPolicy | None = None,
        artifact_store: ArtifactStore | None = None,
        task_backend: TaskBackend | None = None,
        quota: QuotaManager | None = None,
        hosting: HostingConfiguration | None = None,
        configuration_cache: ConfigurationSourceCache | None = None,
        observability: OpenTelemetryFacade | None = None,
        max_attempts: int = 3,
    ):
        self.repository = repository
        self.providers = providers or ProviderRegistry()
        self.observability = observability or OpenTelemetryFacade()
        self.quota = quota or ManagedQuotaManager(
            repository,
            hosting=hosting,
            cache=configuration_cache,
        )
        self.control_plane = ModelControlPlaneService(
            repository=repository,
            providers=self.providers,
            cipher=cipher,
            url_policy=url_policy or URLSecurityPolicy(),
            observability=self.observability,
            quota=self.quota,
        )
        self.runtime = ModelRuntimeService(
            repository=repository,
            providers=self.providers,
            cipher=cipher,
            artifact_store=artifact_store or InMemoryArtifactStore(),
            task_backend=task_backend or InMemoryTaskBackend(),
            quota=self.quota,
            observability=self.observability,
            max_attempts=max_attempts,
        )

    @classmethod
    def sqlite(
        cls,
        path: str | Path = ":memory:",
        *,
        encryption_key: str,
        url_policy: URLSecurityPolicy | None = None,
        artifact_store: ArtifactStore | None = None,
        task_backend: TaskBackend | None = None,
        hosting: HostingConfiguration | None = None,
        configuration_cache: ConfigurationSourceCache | None = None,
    ) -> ModelRepositoryClient:
        if str(path) == ":memory:":
            engine = create_engine(
                "sqlite+pysqlite:///:memory:",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            engine = create_engine(
                f"sqlite+pysqlite:///{Path(path).resolve()}",
                connect_args={"check_same_thread": False},
                pool_pre_ping=True,
            )
        repository = ModelAccessRepository(engine, create_schema=True)
        return cls(
            repository=repository,
            cipher=FernetCredentialCipher(encryption_key),
            url_policy=url_policy,
            artifact_store=artifact_store,
            task_backend=task_backend,
            hosting=hosting,
            configuration_cache=configuration_cache,
        )

    def register_adapter(self, adapter: ProviderAdapter, *, replace: bool = False) -> None:
        self.providers.register(adapter, replace=replace)

    async def register_model(
        self,
        request: ModelRegistrationRequest,
        *,
        identity: CallerIdentity,
        idempotency_key: str,
    ) -> RegistrationResult:
        return await self.control_plane.register_model(
            request,
            identity=identity,
            idempotency_key=idempotency_key,
        )

    async def list_models(
        self,
        query: ModelListQuery,
        *,
        identity: CallerIdentity,
    ) -> ModelListResult:
        return await self.control_plane.list_models(query, identity=identity)

    async def invoke(
        self,
        request: ModelInvocationRequest,
        *,
        identity: CallerIdentity,
    ) -> InvocationResult | AsyncInvocationResult | AsyncIterator[StreamEvent]:
        return await self.runtime.invoke(request, identity=identity)

    def configure_quota_pool(
        self,
        config: ProviderQuotaPoolInput,
        *,
        identity: CallerIdentity,
    ) -> ProviderQuotaView:
        if not isinstance(self.quota, ManagedQuotaManager):
            raise RuntimeError("the configured QuotaManager does not expose managed quota controls")
        return self.quota.configure_pool(config, identity=identity)

    def set_provider_preference(
        self,
        *,
        tenant_id: str,
        provider: ProviderRef,
        preferred_provider_type: ProviderType,
        identity: CallerIdentity,
    ) -> None:
        if not isinstance(self.quota, ManagedQuotaManager):
            raise RuntimeError("the configured QuotaManager does not expose provider preferences")
        self.quota.set_preference(
            tenant_id=tenant_id,
            provider=provider,
            preferred_provider_type=preferred_provider_type,
            identity=identity,
        )

    def list_quota_pools(
        self,
        *,
        tenant_id: str,
        provider: ProviderRef,
        identity: CallerIdentity,
    ) -> list[ProviderQuotaView]:
        if not isinstance(self.quota, ManagedQuotaManager):
            raise RuntimeError("the configured QuotaManager does not expose quota pools")
        return self.quota.list_pools(
            tenant_id=tenant_id,
            provider=provider,
            identity=identity,
        )

    async def close(self) -> None:
        for descriptor in self.providers.list_providers():
            adapter = self.providers.get(descriptor.provider)
            close = getattr(adapter, "close", None)
            if close:
                result = close()
                if inspect.isawaitable(result):
                    await result
        self.repository.engine.dispose()
