from __future__ import annotations

import pytest

from model_access import FernetCredentialCipher, ModelRepositoryClient, URLSecurityPolicy
from model_access.adapters import MockProviderAdapter
from model_access.contracts.entities import (
    CallerIdentity,
    I18nObject,
    ModelDescriptor,
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderRef,
)
from model_access.contracts.enums import ModelType


@pytest.fixture
def provider_ref() -> ProviderRef:
    return ProviderRef(plugin_id="builtin/mock", provider_id="mock")


@pytest.fixture
def model_descriptors(provider_ref: ProviderRef) -> list[ModelDescriptor]:
    specs = [
        ("mock-chat", ModelType.TEXT_GENERATION, {"text", "image"}, {"text"}),
        ("mock-embed", ModelType.EMBEDDING, {"text"}, {"vector"}),
        ("mock-rerank", ModelType.RERANK, {"text"}, {"json"}),
        ("mock-stt", ModelType.SPEECH_TO_TEXT, {"audio"}, {"text"}),
        ("mock-tts", ModelType.TEXT_TO_SPEECH, {"text"}, {"audio"}),
        ("mock-image", ModelType.IMAGE_GENERATION, {"text"}, {"image"}),
        ("mock-video", ModelType.VIDEO_GENERATION, {"text", "image"}, {"video"}),
        ("mock-moderation", ModelType.MODERATION, {"text"}, {"json"}),
    ]
    return [
        ModelDescriptor(
            provider=provider_ref,
            model=model,
            label=model,
            model_type=model_type,
            input_modalities=input_modalities,
            output_modalities=output_modalities,
            features={"streaming"} if model_type == ModelType.TEXT_GENERATION else set(),
        )
        for model, model_type, input_modalities, output_modalities in specs
    ]


@pytest.fixture
def provider_descriptor(
    provider_ref: ProviderRef,
    model_descriptors: list[ModelDescriptor],
) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider=provider_ref,
        display_name=I18nObject(default="Mock Provider", zh_Hans="模拟供应商"),
        supported_model_types=[item.model_type for item in model_descriptors],
        capabilities=ProviderCapabilities(
            supports_streaming=True,
            supports_tools=True,
            supports_vision=True,
            supports_polling=True,
        ),
        dynamic_model_discovery=True,
    )


@pytest.fixture
def client(
    provider_descriptor: ProviderDescriptor,
    model_descriptors: list[ModelDescriptor],
) -> ModelRepositoryClient:
    client = ModelRepositoryClient.sqlite(
        encryption_key=FernetCredentialCipher.generate_key(),
        url_policy=URLSecurityPolicy(allowed_hosts={"mock.local"}),
    )
    client.register_adapter(MockProviderAdapter(provider_descriptor, model_descriptors))
    return client


@pytest.fixture
def identity() -> CallerIdentity:
    return CallerIdentity(tenant_id="tenant_001", user_id="user_123")
