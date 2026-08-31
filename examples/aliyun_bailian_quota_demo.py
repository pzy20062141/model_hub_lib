from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import cast

import httpx

from model_access import FernetCredentialCipher, ModelRepositoryClient, URLSecurityPolicy
from model_access.adapters import OpenAICompatibleAdapter, load_provider_manifest
from model_access.contracts.entities import CallerIdentity, CredentialInput, RuntimeContext
from model_access.contracts.enums import (
    CredentialScope,
    ModelOperation,
    ModelType,
    ProviderQuotaType,
    ProviderType,
    QuotaUnit,
    ResponseMode,
)
from model_access.contracts.invocation import (
    ChatInput,
    ModelInvocationRequest,
    ModelListQuery,
    ModelRegistrationOptions,
    ModelRegistrationRequest,
    ModelSelector,
    PromptMessage,
    TextContentPart,
)
from model_access.contracts.quota import ProviderQuotaPoolInput
from model_access.contracts.responses import InvocationResult, StreamEvent

BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
TENANT_ID = "tenant_aliyun_demo"
ADMIN_USER_ID = "platform_admin"
END_USER_ID = "user_001"
QUERY = "你好，请问你可以做哪些事情"


def build_chat_request(
    *,
    configured_model_id: str,
    query_id: str,
    response_mode: ResponseMode,
) -> ModelInvocationRequest:
    return ModelInvocationRequest(
        context=RuntimeContext(
            tenant_id=TENANT_ID,
            user_id=END_USER_ID,
            session_id="session_bailian_demo",
            query_id=query_id,
        ),
        model=ModelSelector(
            configured_model_id=configured_model_id,
            model_type=ModelType.TEXT_GENERATION,
        ),
        operation=ModelOperation.CHAT,
        input=ChatInput(
            messages=[
                PromptMessage(
                    role="user",
                    content=[TextContentPart(type="text", text=QUERY)],
                )
            ]
        ),
        response_mode=response_mode,
        parameters={"temperature": 0.7, "max_tokens": 512},
        metadata={"scene": "conversation"},
    )


async def invoke_blocking(
    *,
    client: ModelRepositoryClient,
    configured_model_id: str,
    identity: CallerIdentity,
) -> str:
    """非流式调用：等待完整响应后一次性返回 InvocationResult。"""
    result = await client.invoke(
        build_chat_request(
            configured_model_id=configured_model_id,
            query_id="query_bailian_blocking_001",
            response_mode=ResponseMode.BLOCKING,
        ),
        identity=identity,
    )
    if not isinstance(result, InvocationResult):
        raise RuntimeError("非流式调用预期得到 InvocationResult")

    answer = str(result.output["content"][0]["text"])
    print("\n[非流式调用]")
    print("用户问题：", QUERY)
    print("模型回答：", answer)
    print("本次用量：", result.usage.model_dump(mode="json") if result.usage else None)
    return answer


async def invoke_streaming(
    *,
    client: ModelRepositoryClient,
    configured_model_id: str,
    identity: CallerIdentity,
) -> str:
    """流式调用：逐个消费 StreamEvent，并实时输出文本增量。"""
    result = await client.invoke(
        build_chat_request(
            configured_model_id=configured_model_id,
            query_id="query_bailian_streaming_001",
            response_mode=ResponseMode.STREAMING,
        ),
        identity=identity,
    )
    if not hasattr(result, "__aiter__"):
        raise RuntimeError("流式调用预期得到异步事件流")

    stream = cast(AsyncIterator[StreamEvent], result)
    answer_parts: list[str] = []
    usage: dict | None = None
    print("\n[流式调用]")
    print("用户问题：", QUERY)
    print("模型回答：", end="", flush=True)

    async for event in stream:
        if event.event == "output.delta":
            delta = event.data.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text":
                text = str(delta.get("text", ""))
                answer_parts.append(text)
                print(text, end="", flush=True)
        elif event.event == "usage":
            usage = event.data
        elif event.event == "error":
            raise RuntimeError(f"流式模型调用失败：{event.data}")

    print()
    print("本次用量：", usage)
    return "".join(answer_parts)


async def run_demo(
    *,
    api_key: str,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[str, str]:
    manifest = load_provider_manifest("config/providers/aliyun-bailian.yaml")
    provider_descriptor, model_manifest = manifest.build()
    provider_ref = provider_descriptor.provider

    client = ModelRepositoryClient.sqlite(
        ":memory:",
        encryption_key=FernetCredentialCipher.generate_key(),
        url_policy=URLSecurityPolicy(allowed_hosts={"dashscope.aliyuncs.com"}),
    )
    client.register_adapter(
        OpenAICompatibleAdapter(
            descriptor=provider_descriptor,
            model_manifest=model_manifest,
            client=http_client,
        )
    )

    system_admin = CallerIdentity(
        tenant_id=TENANT_ID,
        user_id=ADMIN_USER_ID,
        roles={"system_admin"},
    )
    tenant_user = CallerIdentity(tenant_id=TENANT_ID, user_id=END_USER_ID)

    try:
        registration = await client.register_model(
            ModelRegistrationRequest(
                tenant_id=TENANT_ID,
                user_id=ADMIN_USER_ID,
                provider=provider_ref,
                credential=CredentialInput(
                    name="阿里百炼平台托管凭据",
                    base_url=BASE_URL,
                    api_key=api_key,
                    scope=CredentialScope.SYSTEM,
                ),
                # 百炼 OpenAI 兼容端点不提供 GET /models，使用本地显式模型清单。
                options=ModelRegistrationOptions(
                    validate_credentials=False,
                    discover_models=False,
                ),
            ),
            identity=system_admin,
            idempotency_key="aliyun-bailian-system-registration-v1",
        )
        print("注册模型配置：", registration.configured_model_ids)

        quota = client.configure_quota_pool(
            ProviderQuotaPoolInput(
                tenant_id=TENANT_ID,
                provider=provider_ref,
                quota_type=ProviderQuotaType.TRIAL,
                quota_unit=QuotaUnit.TOKENS,
                quota_limit=100_000,
                restrict_models={"qwen-plus"},
            ),
            identity=system_admin,
        )
        client.set_provider_preference(
            tenant_id=TENANT_ID,
            provider=provider_ref,
            preferred_provider_type=ProviderType.SYSTEM,
            identity=system_admin,
        )
        print("租户配额：", quota.model_dump(mode="json"))

        models = await client.list_models(
            ModelListQuery(
                tenant_id=TENANT_ID,
                user_id=END_USER_ID,
                model_type=ModelType.TEXT_GENERATION,
                provider_id=provider_ref.provider_id,
            ),
            identity=tenant_user,
        )
        if not models.items:
            raise RuntimeError("当前租户没有配额可用的文本生成模型")
        selected = models.items[0]
        print(
            "选中模型：",
            selected.model,
            "using=",
            selected.using_provider_type,
            "remaining=",
            selected.quota_remaining,
        )

        blocking_answer = await invoke_blocking(
            client=client,
            configured_model_id=selected.configured_model_id,
            identity=tenant_user,
        )
        streaming_answer = await invoke_streaming(
            client=client,
            configured_model_id=selected.configured_model_id,
            identity=tenant_user,
        )
        return blocking_answer, streaming_answer
    finally:
        await client.close()


async def main() -> None:
    api_key = os.getenv("DASHSCOPE_API_KEY", "sk-xxxx")
    if api_key == "sk-xxxx":
        raise RuntimeError("请先执行 export DASHSCOPE_API_KEY='sk-实际百炼Key'；sk-xxxx 仅为占位符")
    await run_demo(api_key=api_key)


if __name__ == "__main__":
    asyncio.run(main())
