from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from .adapters.registry import ProviderRegistry
from .contracts.entities import CallerIdentity
from .contracts.enums import ModelType
from .contracts.invocation import (
    ExistingCredentialModelRegistrationRequest,
    ModelInvocationRequest,
    ModelListQuery,
    ModelRegistrationRequest,
    TenantDefaultModelUpdateRequest,
)
from .contracts.quota import (
    ModelCreditRateInput,
    ModelCreditRateView,
    RoleQuotaBindingInput,
    UserCostReport,
    UserCostSummary,
    UserQuotaAssignmentInput,
    UserQuotaSummary,
    UserQuotaTemplateInput,
    UserQuotaTemplateView,
)
from .contracts.responses import (
    AsyncInvocationResult,
    ConfiguredModelRegistrationResult,
    InvocationResult,
    ModelListResult,
    RegistrationResult,
    StreamEvent,
    TenantDefaultModelsResult,
)
from .control_plane import ModelControlPlaneService
from .infrastructure import InMemoryArtifactStore, InMemoryTaskBackend
from .observability import OpenTelemetryFacade
from .persistence.repository import ModelAccessRepository
from .protocols import ArtifactStore, CredentialCipher, ProviderAdapter, QuotaManager, TaskBackend
from .runtime import ModelRuntimeService
from .security import FernetCredentialCipher, URLSecurityPolicy
from .user_quota import UserQuotaManager


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
        observability: OpenTelemetryFacade | None = None,
        max_attempts: int = 3,
    ):
        self.repository = repository
        self.providers = providers or ProviderRegistry()
        self.observability = observability or OpenTelemetryFacade()
        self.quota = quota or UserQuotaManager(repository)
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

    async def register_model_with_credential(
        self,
        request: ExistingCredentialModelRegistrationRequest,
        *,
        identity: CallerIdentity,
    ) -> ConfiguredModelRegistrationResult:
        return await self.control_plane.register_model_with_credential(
            request,
            identity=identity,
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

    async def set_default_model(
        self,
        request: TenantDefaultModelUpdateRequest,
        *,
        model_type: ModelType,
        identity: CallerIdentity,
    ) -> TenantDefaultModelsResult:
        return await self.control_plane.set_default_model(
            request,
            model_type=model_type,
            identity=identity,
        )

    async def get_default_models(
        self,
        *,
        tenant_id: str,
        identity: CallerIdentity,
    ) -> TenantDefaultModelsResult:
        return await self.control_plane.get_default_models(
            tenant_id=tenant_id,
            identity=identity,
        )

    def _user_quota(self) -> UserQuotaManager:
        if not isinstance(self.quota, UserQuotaManager):
            raise RuntimeError("the configured QuotaManager does not expose user quota controls")
        return self.quota

    def configure_model_credit_rate(
        self, config: ModelCreditRateInput, *, identity: CallerIdentity
    ) -> ModelCreditRateView:
        return self._user_quota().configure_model_rate(config, identity=identity)

    def configure_user_quota_template(
        self, config: UserQuotaTemplateInput, *, identity: CallerIdentity
    ) -> UserQuotaTemplateView:
        return self._user_quota().configure_template(config, identity=identity)

    def bind_quota_template_to_role(
        self, config: RoleQuotaBindingInput, *, identity: CallerIdentity
    ) -> None:
        self._user_quota().bind_role(config, identity=identity)

    def assign_user_quota(
        self, config: UserQuotaAssignmentInput, *, identity: CallerIdentity
    ) -> None:
        self._user_quota().assign_user(config, identity=identity)

    def get_user_quota(
        self,
        *,
        tenant_id: str,
        user_id: str,
        roles: set[str] | None,
        identity: CallerIdentity,
    ) -> UserQuotaSummary:
        return self._user_quota().get_summary(
            tenant_id=tenant_id,
            user_id=user_id,
            roles=roles or set(),
            identity=identity,
        )

    def query_user_costs(
        self,
        *,
        tenant_id: str,
        user_id: str | None,
        identity: CallerIdentity,
        start_at=None,
        end_at=None,
        limit: int = 100,
    ) -> UserCostReport:
        return self._user_quota().query_costs(
            tenant_id=tenant_id,
            user_id=user_id,
            identity=identity,
            start_at=start_at,
            end_at=end_at,
            limit=limit,
        )

    def summarize_user_costs(
        self,
        *,
        tenant_id: str,
        user_id: str | None,
        identity: CallerIdentity,
        start_at=None,
        end_at=None,
    ) -> UserCostSummary:
        return self._user_quota().summarize_costs(
            tenant_id=tenant_id,
            user_id=user_id,
            identity=identity,
            start_at=start_at,
            end_at=end_at,
        )

    async def finalize_async_quota(self, *, invocation_id: str, usage, succeeded: bool) -> None:
        await self.quota.settle(invocation_id=invocation_id, usage=usage, succeeded=succeeded)

    async def close(self) -> None:
        for descriptor in self.providers.list_providers():
            adapter = self.providers.get(descriptor.provider)
            close = getattr(adapter, "close", None)
            if close:
                result = close()
                if inspect.isawaitable(result):
                    await result
        self.repository.engine.dispose()
