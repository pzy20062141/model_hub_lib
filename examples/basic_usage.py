from __future__ import annotations

import asyncio
import os

from model_access import ModelRepositoryClient, URLSecurityPolicy
from model_access.adapters import MockProviderAdapter
from model_access.contracts.entities import (
    CallerIdentity,
    CredentialInput,
    I18nObject,
    ModelDescriptor,
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderRef,
    RuntimeContext,
)
from model_access.contracts.enums import CredentialScope, ModelOperation, ModelType
from model_access.contracts.invocation import (
    ChatInput,
    ModelInvocationRequest,
    ModelListQuery,
    ModelRegistrationRequest,
    ModelSelector,
    PromptMessage,
    TextContentPart,
)


async def main() -> None:
    provider = ProviderRef(plugin_id="builtin/mock", provider_id="mock")
    model = ModelDescriptor(
        provider=provider,
        model="mock-chat",
        model_type=ModelType.TEXT_GENERATION,
        features={"streaming", "tool_calling"},
    )
    descriptor = ProviderDescriptor(
        provider=provider,
        display_name=I18nObject(default="Mock Provider"),
        supported_model_types=[ModelType.TEXT_GENERATION],
        capabilities=ProviderCapabilities(supports_streaming=True, supports_tools=True),
        dynamic_model_discovery=True,
    )
    encryption_key = os.environ["MODEL_ACCESS_MASTER_KEY"]
    client = ModelRepositoryClient.sqlite(
        ":memory:",
        encryption_key=encryption_key,
        url_policy=URLSecurityPolicy(allowed_hosts={"mock.local"}),
    )
    client.register_adapter(MockProviderAdapter(descriptor, [model]))
    identity = CallerIdentity(tenant_id="tenant_001", user_id="user_123")

    registration = await client.register_model(
        ModelRegistrationRequest(
            tenant_id="tenant_001",
            user_id="user_123",
            provider=provider,
            credential=CredentialInput(
                name="local mock",
                base_url="https://mock.local/v1",
                api_key="mock-key",
                scope=CredentialScope.USER,
            ),
        ),
        identity=identity,
        idempotency_key="example-registration-1",
    )
    models = await client.list_models(
        ModelListQuery(
            tenant_id="tenant_001",
            user_id="user_123",
            model_type=ModelType.TEXT_GENERATION,
        ),
        identity=identity,
    )
    result = await client.invoke(
        ModelInvocationRequest(
            context=RuntimeContext(
                tenant_id="tenant_001",
                user_id="user_123",
                session_id="session_1",
                query_id="query_1",
            ),
            model=ModelSelector(
                configured_model_id=models.items[0].configured_model_id,
                model_type=ModelType.TEXT_GENERATION,
            ),
            operation=ModelOperation.CHAT,
            input=ChatInput(
                messages=[
                    PromptMessage(
                        role="user",
                        content=[TextContentPart(type="text", text="总结设备告警")],
                    )
                ]
            ),
            metadata={"scene": "conversation"},
        ),
        identity=identity,
    )
    print(registration.model_dump_json(indent=2))
    print(result.model_dump_json(indent=2))  # type: ignore[union-attr]
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
