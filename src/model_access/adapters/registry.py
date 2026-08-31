from __future__ import annotations

from collections.abc import Sequence

from ..contracts.entities import ProviderDescriptor, ProviderRef, RuntimeContext
from ..contracts.enums import ErrorCode
from ..errors import ModelAccessException
from ..protocols import ProviderAdapter


class ProviderRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter, *, replace: bool = False) -> None:
        key = adapter.descriptor.provider.key
        if key in self._adapters and not replace:
            raise ValueError(f"provider adapter already registered: {key}")
        self._adapters[key] = adapter

    def unregister(self, provider: ProviderRef) -> None:
        self._adapters.pop(provider.key, None)

    def get(self, provider: ProviderRef) -> ProviderAdapter:
        adapter = self._adapters.get(provider.key)
        if adapter is None:
            raise ModelAccessException(
                ErrorCode.PROVIDER_NOT_FOUND,
                "provider is not registered",
                provider=provider.key,
            )
        if not adapter.descriptor.enabled:
            raise ModelAccessException(
                ErrorCode.PROVIDER_DISABLED,
                "provider is disabled",
                provider=provider.key,
            )
        return adapter

    def list_providers(self, context: RuntimeContext | None = None) -> Sequence[ProviderDescriptor]:
        del context
        return [adapter.descriptor for adapter in self._adapters.values()]
