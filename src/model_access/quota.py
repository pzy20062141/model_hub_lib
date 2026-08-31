from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Protocol

import yaml

from .contracts.entities import CallerIdentity, ProviderRef
from .contracts.enums import (
    CredentialScope,
    CredentialStatus,
    ErrorCode,
    ModelStatus,
    ProviderQuotaType,
    ProviderType,
    QuotaReservationStatus,
    QuotaUnit,
)
from .contracts.quota import (
    HostingQuotaDefinition,
    ProviderConfiguration,
    ProviderQuotaPoolInput,
    ProviderQuotaView,
    QuotaAllocation,
)
from .contracts.responses import Usage
from .errors import ModelAccessException
from .persistence.repository import ModelAccessRepository, ResolvedModelRecord


class ConfigurationSourceCache(Protocol):
    async def get(self, cache_key: str, version: int) -> ProviderConfiguration | None: ...

    async def set(
        self, cache_key: str, version: int, value: ProviderConfiguration, ttl_seconds: int
    ) -> None: ...


class InMemoryConfigurationSourceCache:
    """Development cache. Production deployments should use the Redis implementation."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, int], tuple[float, ProviderConfiguration]] = {}

    async def get(self, cache_key: str, version: int) -> ProviderConfiguration | None:
        item = self._items.get((cache_key, version))
        if not item:
            return None
        expires_at, value = item
        if expires_at <= time.monotonic():
            self._items.pop((cache_key, version), None)
            return None
        return value.model_copy(deep=True)

    async def set(
        self, cache_key: str, version: int, value: ProviderConfiguration, ttl_seconds: int
    ) -> None:
        self._items[(cache_key, version)] = (
            time.monotonic() + ttl_seconds,
            value.model_copy(deep=True),
        )
        if len(self._items) > 4096:
            now = time.monotonic()
            self._items = {key: item for key, item in self._items.items() if item[0] > now}


class RedisConfigurationSourceCache:
    """Version-keyed cache compatible with ``redis.asyncio.Redis`` clients."""

    def __init__(self, redis_client: Any, *, prefix: str = "model-access:provider-config"):
        self._redis = redis_client
        self._prefix = prefix.rstrip(":")

    def _key(self, cache_key: str, version: int) -> str:
        digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
        return f"{self._prefix}:{digest}:v{version}"

    async def get(self, cache_key: str, version: int) -> ProviderConfiguration | None:
        payload = await self._redis.get(self._key(cache_key, version))
        if not payload:
            return None
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return ProviderConfiguration.model_validate_json(payload)

    async def set(
        self, cache_key: str, version: int, value: ProviderConfiguration, ttl_seconds: int
    ) -> None:
        await self._redis.set(
            self._key(cache_key, version),
            value.model_dump_json(),
            ex=ttl_seconds,
        )


class HostingConfiguration:
    """Global hosted-quota definitions. Only CLOUD edition activates lazy initialization."""

    def __init__(
        self,
        *,
        edition: str = "SELF_HOSTED",
        quotas: list[HostingQuotaDefinition] | None = None,
    ) -> None:
        self.edition = edition.upper()
        self.quotas = [item for item in (quotas or []) if item.enabled]

    @property
    def enabled(self) -> bool:
        return self.edition == "CLOUD"

    def for_provider(self, provider: ProviderRef) -> list[HostingQuotaDefinition]:
        if not self.enabled:
            return []
        return [item for item in self.quotas if item.provider.key == provider.key]

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> HostingConfiguration:
        return cls(
            edition=str(data.get("edition", "SELF_HOSTED")),
            quotas=[HostingQuotaDefinition.model_validate(item) for item in data.get("quotas", [])],
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> HostingConfiguration:
        with Path(path).open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}
        if not isinstance(data, dict):
            raise ValueError("hosting configuration root must be an object")
        return cls.from_mapping(data)


class ManagedQuotaManager:
    """Dify-style quota arbitration backed by transactional database records."""

    _PRIORITY = {
        ProviderQuotaType.PAID: 0,
        ProviderQuotaType.FREE: 1,
        ProviderQuotaType.TRIAL: 2,
    }

    def __init__(
        self,
        repository: ModelAccessRepository,
        *,
        hosting: HostingConfiguration | None = None,
        cache: ConfigurationSourceCache | None = None,
        cache_ttl_seconds: int = 300,
    ) -> None:
        self._repository = repository
        self._hosting = hosting or HostingConfiguration()
        self._cache = cache or InMemoryConfigurationSourceCache()
        self._cache_ttl_seconds = max(1, cache_ttl_seconds)

    def configure_pool(
        self, config: ProviderQuotaPoolInput, *, identity: CallerIdentity
    ) -> ProviderQuotaView:
        self._authorize_admin(config.tenant_id, identity)
        quota_id = config.quota_id or self._quota_id(
            config.tenant_id,
            config.provider,
            config.quota_type,
            config.quota_unit,
            config.restrict_models,
        )
        self._repository.upsert_quota_pool(
            quota_id=quota_id,
            tenant_id=config.tenant_id,
            plugin_id=config.provider.plugin_id,
            provider_id=config.provider.provider_id,
            quota_type=config.quota_type,
            quota_unit=config.quota_unit,
            quota_limit=config.quota_limit,
            quota_used=config.quota_used,
            restrict_models=sorted(config.restrict_models),
            is_valid=config.is_valid,
        )
        record = next(
            item
            for item in self._repository.list_quota_pools(
                tenant_id=config.tenant_id,
                plugin_id=config.provider.plugin_id,
                provider_id=config.provider.provider_id,
            )
            if item.quota_id == quota_id
        )
        return self._quota_view(record)

    def set_preference(
        self,
        *,
        tenant_id: str,
        provider: ProviderRef,
        preferred_provider_type: ProviderType,
        identity: CallerIdentity,
    ) -> None:
        self._authorize_admin(tenant_id, identity)
        self._repository.set_provider_preference(
            tenant_id=tenant_id,
            plugin_id=provider.plugin_id,
            provider_id=provider.provider_id,
            preferred_provider_type=preferred_provider_type,
        )

    def list_pools(
        self,
        *,
        tenant_id: str,
        provider: ProviderRef,
        identity: CallerIdentity,
    ) -> list[ProviderQuotaView]:
        self._authorize_admin(tenant_id, identity)
        return [
            self._quota_view(item)
            for item in self._repository.list_quota_pools(
                tenant_id=tenant_id,
                plugin_id=provider.plugin_id,
                provider_id=provider.provider_id,
            )
        ]

    async def describe(
        self,
        *,
        requested: ResolvedModelRecord,
        tenant_id: str,
        user_id: str | None,
        bypass_cache: bool = False,
    ) -> ProviderConfiguration:
        provider = ProviderRef(
            plugin_id=requested.model.plugin_id,
            provider_id=requested.model.provider_id,
        )
        self._initialize_hosting_quotas(tenant_id, provider)
        source_key = self._repository.provider_source_key(
            tenant_id, provider.plugin_id, provider.provider_id
        )
        version = self._repository.get_source_version(source_key)
        cache_key = ":".join(
            [source_key, user_id or "-", requested.model.configured_model_id, requested.model.model]
        )
        if not bypass_cache:
            cached = await self._cache.get(cache_key, version)
            if cached:
                return cached
        configuration = self._assemble(
            requested=requested,
            tenant_id=tenant_id,
            user_id=user_id,
            source_version=version,
        )
        await self._cache.set(
            cache_key,
            version,
            configuration,
            self._cache_ttl_seconds,
        )
        return configuration

    async def acquire(
        self,
        *,
        invocation_id: str,
        tenant_id: str,
        user_id: str | None,
        requested: ResolvedModelRecord,
        operation: str,
        estimated_tokens: int,
    ) -> QuotaAllocation:
        del operation
        configuration = await self.describe(
            requested=requested,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if configuration.status == ModelStatus.QUOTA_EXCEEDED:
            raise self._quota_exceeded(configuration)
        if configuration.status == ModelStatus.NO_CONFIGURE:
            raise ModelAccessException(
                ErrorCode.CREDENTIAL_REQUIRED,
                "preferred CUSTOM provider has no valid credential",
                provider=configuration.provider.key,
                model=configuration.model,
                extensions={
                    "preferred_provider_type": configuration.preferred_provider_type.value,
                    "using_provider_type": None,
                    "model_status": ModelStatus.NO_CONFIGURE.value,
                },
            )
        if not configuration.selected_configured_model_id or not configuration.using_provider_type:
            raise ModelAccessException(
                ErrorCode.PROVIDER_UNAVAILABLE, "no usable provider configuration"
            )

        if configuration.using_provider_type == ProviderType.CUSTOM:
            reservation = self._repository.reserve_custom(
                invocation_id=invocation_id,
                tenant_id=tenant_id,
                configured_model_id=configuration.selected_configured_model_id,
            )
            self._assert_reservation_open(reservation.status)
            return QuotaAllocation(
                invocation_id=invocation_id,
                configured_model_id=reservation.configured_model_id,
                preferred_provider_type=configuration.preferred_provider_type,
                using_provider_type=ProviderType.CUSTOM,
                reservation_id=reservation.invocation_id,
                fallback_reason=configuration.fallback_reason,
            )

        reservation = self._repository.reserve_quota(
            invocation_id=invocation_id,
            tenant_id=tenant_id,
            configured_model_id=configuration.selected_configured_model_id,
            eligible_quota_ids=configuration.eligible_quota_ids,
            estimated_tokens=max(1, estimated_tokens),
        )
        if reservation:
            self._assert_reservation_open(reservation.status)
            return QuotaAllocation(
                invocation_id=invocation_id,
                configured_model_id=reservation.configured_model_id,
                preferred_provider_type=configuration.preferred_provider_type,
                using_provider_type=ProviderType.SYSTEM,
                reservation_id=reservation.invocation_id,
                quota_id=reservation.quota_id,
                quota_type=(
                    ProviderQuotaType(reservation.quota_type) if reservation.quota_type else None
                ),
                quota_unit=QuotaUnit(reservation.quota_unit) if reservation.quota_unit else None,
                reserved_units=reservation.reserved_units,
            )

        # A concurrent request may have exhausted the cached pool after assembly.
        # Re-read candidates and degrade to CUSTOM if the tenant has one.
        custom = self._select_custom(
            self._candidates(requested, tenant_id=tenant_id, user_id=user_id), requested
        )
        if configuration.preferred_provider_type == ProviderType.SYSTEM and custom:
            fallback = self._repository.reserve_custom(
                invocation_id=invocation_id,
                tenant_id=tenant_id,
                configured_model_id=custom.model.configured_model_id,
            )
            self._assert_reservation_open(fallback.status)
            return QuotaAllocation(
                invocation_id=invocation_id,
                configured_model_id=fallback.configured_model_id,
                preferred_provider_type=ProviderType.SYSTEM,
                using_provider_type=ProviderType.CUSTOM,
                reservation_id=fallback.invocation_id,
                fallback_reason="SYSTEM_QUOTA_EXHAUSTED",
            )
        latest = await self.describe(
            requested=requested,
            tenant_id=tenant_id,
            user_id=user_id,
            bypass_cache=True,
        )
        raise self._quota_exceeded(latest)

    async def reserve(
        self,
        *,
        invocation_id: str,
        tenant_id: str,
        configured_model_id: str,
        operation: str,
    ) -> None:
        """Compatibility path for callers using the v0.1 QuotaManager protocol."""

        del operation
        reservation = self._repository.reserve_custom(
            invocation_id=invocation_id,
            tenant_id=tenant_id,
            configured_model_id=configured_model_id,
        )
        self._assert_reservation_open(reservation.status)

    async def settle(self, *, invocation_id: str, usage: Usage | None, succeeded: bool) -> None:
        actual_tokens = None
        if usage:
            actual_tokens = usage.total_tokens
            if actual_tokens is None and usage.billable_units is not None:
                actual_tokens = int(usage.billable_units)
        self._repository.settle_quota(
            invocation_id=invocation_id,
            succeeded=succeeded,
            actual_tokens=actual_tokens,
        )

    def _assemble(
        self,
        *,
        requested: ResolvedModelRecord,
        tenant_id: str,
        user_id: str | None,
        source_version: int,
    ) -> ProviderConfiguration:
        provider = ProviderRef(
            plugin_id=requested.model.plugin_id,
            provider_id=requested.model.provider_id,
        )
        candidates = self._candidates(requested, tenant_id=tenant_id, user_id=user_id)
        system = self._select_system(candidates, requested)
        custom = self._select_custom(candidates, requested)
        requested_type = self._provider_type(requested)
        preferred = (
            self._repository.get_provider_preference(
                tenant_id=tenant_id,
                plugin_id=provider.plugin_id,
                provider_id=provider.provider_id,
            )
            or requested_type
        )
        pools = self._eligible_pools(
            tenant_id=tenant_id,
            provider=provider,
            model=requested.model.model,
        )
        base = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "provider": provider,
            "model": requested.model.model,
            "model_type": requested.model.model_type,
            "preferred_provider_type": preferred,
            "source_version": source_version,
        }
        if preferred == ProviderType.CUSTOM:
            if custom:
                return ProviderConfiguration(
                    **base,
                    using_provider_type=ProviderType.CUSTOM,
                    selected_configured_model_id=custom.model.configured_model_id,
                    status=ModelStatus.ACTIVE,
                )
            return ProviderConfiguration(**base, status=ModelStatus.NO_CONFIGURE)

        if system and pools:
            selected_pool = pools[0]
            remaining = self._remaining(selected_pool)
            return ProviderConfiguration(
                **base,
                using_provider_type=ProviderType.SYSTEM,
                selected_configured_model_id=system.model.configured_model_id,
                selected_quota_id=selected_pool.quota_id,
                selected_quota_type=ProviderQuotaType(selected_pool.quota_type),
                quota_unit=QuotaUnit(selected_pool.quota_unit),
                quota_remaining=remaining,
                eligible_quota_ids=[item.quota_id for item in pools],
                status=ModelStatus.ACTIVE,
            )
        if custom:
            return ProviderConfiguration(
                **base,
                using_provider_type=ProviderType.CUSTOM,
                selected_configured_model_id=custom.model.configured_model_id,
                status=ModelStatus.ACTIVE,
                fallback_reason="SYSTEM_QUOTA_EXHAUSTED" if system else None,
            )
        return ProviderConfiguration(
            **base,
            status=ModelStatus.QUOTA_EXCEEDED if system else ModelStatus.NO_CONFIGURE,
        )

    def _initialize_hosting_quotas(self, tenant_id: str, provider: ProviderRef) -> None:
        for definition in self._hosting.for_provider(provider):
            quota_id = self._quota_id(
                tenant_id,
                provider,
                definition.quota_type,
                definition.quota_unit,
                definition.restrict_models,
            )
            self._repository.upsert_quota_pool(
                quota_id=quota_id,
                tenant_id=tenant_id,
                plugin_id=provider.plugin_id,
                provider_id=provider.provider_id,
                quota_type=definition.quota_type,
                quota_unit=definition.quota_unit,
                quota_limit=definition.quota_limit,
                quota_used=0,
                restrict_models=sorted(definition.restrict_models),
                is_valid=True,
                create_only=True,
            )

    def _candidates(
        self, requested: ResolvedModelRecord, *, tenant_id: str, user_id: str | None
    ) -> list[ResolvedModelRecord]:
        return [
            item
            for item in self._repository.resolve_model_candidates(
                tenant_id=tenant_id,
                user_id=user_id,
                plugin_id=requested.model.plugin_id,
                provider_id=requested.model.provider_id,
                model=requested.model.model,
                model_type=requested.model.model_type,
            )
            if item.model.status == ModelStatus.ACTIVE.value
            and item.credential.status == CredentialStatus.VALID.value
        ]

    @classmethod
    def _select_system(
        cls, candidates: list[ResolvedModelRecord], requested: ResolvedModelRecord
    ) -> ResolvedModelRecord | None:
        system = [item for item in candidates if cls._provider_type(item) == ProviderType.SYSTEM]
        if cls._provider_type(requested) == ProviderType.SYSTEM:
            exact = next(
                (
                    item
                    for item in system
                    if item.model.configured_model_id == requested.model.configured_model_id
                ),
                None,
            )
            if exact:
                return exact
        return system[0] if system else None

    @classmethod
    def _select_custom(
        cls, candidates: list[ResolvedModelRecord], requested: ResolvedModelRecord
    ) -> ResolvedModelRecord | None:
        custom = [item for item in candidates if cls._provider_type(item) == ProviderType.CUSTOM]
        if cls._provider_type(requested) == ProviderType.CUSTOM:
            exact = next(
                (
                    item
                    for item in custom
                    if item.model.configured_model_id == requested.model.configured_model_id
                ),
                None,
            )
            if exact:
                return exact
        user_scoped = [
            item for item in custom if item.credential.scope == CredentialScope.USER.value
        ]
        return user_scoped[0] if user_scoped else (custom[0] if custom else None)

    def _eligible_pools(self, *, tenant_id: str, provider: ProviderRef, model: str) -> list[Any]:
        pools = self._repository.list_quota_pools(
            tenant_id=tenant_id,
            plugin_id=provider.plugin_id,
            provider_id=provider.provider_id,
        )
        eligible = [
            item
            for item in pools
            if item.is_valid
            and (not item.restrict_models or model in item.restrict_models)
            and (item.quota_limit == -1 or item.quota_used + item.quota_reserved < item.quota_limit)
        ]
        return sorted(
            eligible,
            key=lambda item: (self._PRIORITY[ProviderQuotaType(item.quota_type)], item.created_at),
        )

    @staticmethod
    def _provider_type(record: ResolvedModelRecord) -> ProviderType:
        return (
            ProviderType.SYSTEM
            if record.credential.scope == CredentialScope.SYSTEM.value
            else ProviderType.CUSTOM
        )

    @staticmethod
    def _remaining(record: Any) -> int | None:
        if record.quota_limit == -1:
            return None
        return max(0, record.quota_limit - record.quota_used - record.quota_reserved)

    @classmethod
    def _quota_view(cls, record: Any) -> ProviderQuotaView:
        return ProviderQuotaView(
            quota_id=record.quota_id,
            quota_type=ProviderQuotaType(record.quota_type),
            quota_unit=QuotaUnit(record.quota_unit),
            quota_limit=record.quota_limit,
            quota_used=record.quota_used,
            quota_reserved=record.quota_reserved,
            quota_remaining=cls._remaining(record),
            is_valid=record.is_valid,
            restrict_models=set(record.restrict_models),
        )

    @staticmethod
    def _quota_id(
        tenant_id: str,
        provider: ProviderRef,
        quota_type: ProviderQuotaType,
        quota_unit: QuotaUnit,
        restrict_models: set[str],
    ) -> str:
        payload = json.dumps(
            [tenant_id, provider.key, quota_type.value, quota_unit.value, sorted(restrict_models)],
            separators=(",", ":"),
        )
        return f"quota_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"

    @staticmethod
    def _authorize_admin(tenant_id: str, identity: CallerIdentity) -> None:
        if identity.tenant_id != tenant_id or not identity.is_admin:
            raise ModelAccessException(
                ErrorCode.PERMISSION_DENIED,
                "quota configuration requires an administrator in the same tenant",
            )

    @staticmethod
    def _assert_reservation_open(status: str) -> None:
        if status != QuotaReservationStatus.RESERVED.value:
            raise ModelAccessException(
                ErrorCode.CONTEXT_INVALID,
                "invocation_id already has a completed quota settlement",
            )

    @staticmethod
    def _quota_exceeded(configuration: ProviderConfiguration) -> ModelAccessException:
        return ModelAccessException(
            ErrorCode.QUOTA_EXCEEDED,
            "all hosted quota pools are exhausted and no custom credential is available",
            provider=configuration.provider.key,
            model=configuration.model,
            extensions={
                "preferred_provider_type": configuration.preferred_provider_type.value,
                "using_provider_type": None,
                "model_status": ModelStatus.QUOTA_EXCEEDED.value,
            },
        )
