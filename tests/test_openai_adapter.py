from __future__ import annotations

import base64
import json

import httpx
import pytest

from model_access.adapters import OpenAICompatibleAdapter
from model_access.contracts.entities import (
    CredentialSet,
    I18nObject,
    ModelDescriptor,
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderRef,
    RuntimeContext,
)
from model_access.contracts.enums import (
    CredentialSourceType,
    ModelOperation,
    ModelType,
    ResponseMode,
)
from model_access.contracts.invocation import (
    AdapterInvocation,
    ChatInput,
    ImageGenerationInput,
    PromptMessage,
    TextContentPart,
)
from model_access.contracts.responses import AdapterResponse


def build_adapter(handler):  # type: ignore[no-untyped-def]
    provider = ProviderRef(plugin_id="builtin/openai", provider_id="openai")
    model = ModelDescriptor(
        provider=provider,
        model="gpt-test",
        model_type=ModelType.TEXT_GENERATION,
        features={"streaming"},
    )
    image_model = ModelDescriptor(
        provider=provider,
        model="image-test",
        model_type=ModelType.IMAGE_GENERATION,
    )
    descriptor = ProviderDescriptor(
        provider=provider,
        display_name=I18nObject(default="OpenAI Compatible"),
        supported_model_types=[ModelType.TEXT_GENERATION, ModelType.IMAGE_GENERATION],
        capabilities=ProviderCapabilities(supports_streaming=True),
        dynamic_model_discovery=True,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleAdapter(
        descriptor=descriptor,
        model_manifest=[model, image_model],
        client=client,
    )
    return adapter, client, provider


def invocation(provider: ProviderRef, *, stream: bool = False) -> AdapterInvocation:
    return AdapterInvocation(
        context=RuntimeContext(tenant_id="tenant", user_id="user", invocation_id="inv_1"),
        provider=provider,
        model="gpt-test",
        model_type=ModelType.TEXT_GENERATION,
        operation=ModelOperation.CHAT,
        response_mode=ResponseMode.STREAMING if stream else ResponseMode.BLOCKING,
        input=ChatInput(
            messages=[
                PromptMessage(
                    role="user",
                    content=[TextContentPart(type="text", text="hello")],
                )
            ]
        ),
        credential_values={"base_url": "https://api.test/v1", "api_key": "sk-secret"},
        configured_model_id="cm_1",
        credential_id="cred_1",
    )


@pytest.mark.asyncio
async def test_discovery_and_blocking_chat_normalization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "gpt-test"}, {"id": "unknown"}]})
        assert request.headers["authorization"] == "Bearer sk-secret"
        body = json.loads(request.content)
        assert body["model"] == "gpt-test"
        return httpx.Response(
            200,
            headers={"x-request-id": "provider_req"},
            json={
                "id": "chat_1",
                "model": "gpt-test-2026",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
        )

    adapter, client, provider = build_adapter(handler)
    credentials = CredentialSet(
        provider=provider,
        values={"base_url": "https://api.test/v1", "api_key": "sk-secret"},
        source=CredentialSourceType.USER,
    )
    validation = await adapter.validate_credentials(
        context=RuntimeContext(tenant_id="tenant"),
        provider=provider,
        credentials=credentials,
    )
    assert validation.valid
    models = await adapter.discover_models(
        context=RuntimeContext(tenant_id="tenant"),
        provider=provider,
        credentials=credentials,
    )
    assert [item.model for item in models] == ["gpt-test"]

    response = await adapter.invoke(invocation(provider))
    assert isinstance(response, AdapterResponse)
    assert response.output["content"][0]["text"] == "answer"
    assert response.usage and response.usage.total_tokens == 5
    await client.aclose()


@pytest.mark.asyncio
async def test_streaming_chat_and_image_artifact() -> None:
    png = b"\x89PNG\r\n\x1a\nmock"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            content = (
                b'data: {"id":"1","choices":[{"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
                b'data: {"id":"1","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
                b"data: [DONE]\n\n"
            )
            return httpx.Response(
                200, content=content, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(png).decode("ascii")}]},
        )

    adapter, client, provider = build_adapter(handler)
    stream = await adapter.invoke(invocation(provider, stream=True))
    chunks = [item async for item in stream]  # type: ignore[union-attr]
    assert chunks[0].delta == {"type": "text", "text": "hello"}
    assert chunks[1].usage and chunks[1].usage.total_tokens == 2

    image_response = await adapter.invoke(
        AdapterInvocation(
            context=RuntimeContext(tenant_id="tenant", user_id="user", invocation_id="inv_image"),
            provider=provider,
            model="image-test",
            model_type=ModelType.IMAGE_GENERATION,
            operation=ModelOperation.IMAGE_GENERATE,
            response_mode=ResponseMode.BLOCKING,
            input=ImageGenerationInput(prompt="factory"),
            credential_values={"base_url": "https://api.test/v1", "api_key": "sk-secret"},
            configured_model_id="cm_image",
            credential_id="cred_1",
        )
    )
    assert isinstance(image_response, AdapterResponse)
    assert image_response.artifacts[0].data == png
    await client.aclose()
