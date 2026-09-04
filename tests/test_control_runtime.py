from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from model_access import ModelAccessException
from model_access.contracts.entities import CallerIdentity, RuntimeContext
from model_access.contracts.enums import (
    ErrorCode,
    ModelCategory,
    ModelOperation,
    ModelType,
    QuotaPeriodType,
    ResponseMode,
)
from model_access.contracts.invocation import (
    EmbeddingInput,
    ExistingCredentialModelRegistrationRequest,
    ManualModelRegistration,
    ModelInvocationRequest,
    ModelListQuery,
    ModelSelector,
    VideoGenerationInput,
)
from model_access.contracts.quota import UserQuotaTemplateInput
from model_access.contracts.responses import AsyncInvocationResult, InvocationResult
from model_access.persistence.models import ProviderCredentialRecord

from .helpers import chat_request, registration_request


@pytest.mark.asyncio
async def test_register_list_and_blocking_invoke(client, identity, provider_ref) -> None:
    registration = await client.register_model(
        registration_request(provider_ref),
        identity=identity,
        idempotency_key="registration-1",
    )
    assert registration.discovered_model_count == 8
    assert "secret-key" not in registration.model_dump_json()

    models = await client.list_models(
        ModelListQuery(
            tenant_id="tenant_001",
            user_id="user_123",
            category=ModelCategory.TEXT_MODEL,
        ),
        identity=identity,
    )
    assert [item.model for item in models.items] == ["mock-chat"]
    chat_id = models.items[0].configured_model_id

    result = await client.invoke(chat_request(chat_id), identity=identity)
    assert isinstance(result, InvocationResult)
    assert result.output["content"][0]["text"] == "mock response"
    assert result.usage and result.usage.total_tokens == 5
    assert result.provider_request_id == "mock_request"
    assert result.response_model == "mock-chat"
    assert result.finish_reason == "stop"
    assert result.model_dump()["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_registration_inserts_credential_before_fk_models(
    client, identity, provider_ref
) -> None:
    with client.repository.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    registration = await client.register_model(
        registration_request(provider_ref),
        identity=identity,
        idempotency_key="foreign-key-order",
    )

    assert registration.credential_id
    assert len(registration.configured_model_ids) == 8


@pytest.mark.asyncio
async def test_credential_is_encrypted_at_rest(client, identity, provider_ref) -> None:
    result = await client.register_model(
        registration_request(provider_ref),
        identity=identity,
        idempotency_key="encrypted-1",
    )
    with client.repository._sessions() as session:
        record = session.scalar(
            select(ProviderCredentialRecord).where(
                ProviderCredentialRecord.credential_id == result.credential_id
            )
        )
        assert record is not None
        assert "secret-key" not in record.encrypted_values
        assert record.api_key_masked != "secret-key"


@pytest.mark.asyncio
async def test_register_model_reuses_existing_encrypted_credential(
    client, identity, provider_ref
) -> None:
    registration = await client.register_model(
        registration_request(provider_ref),
        identity=identity,
        idempotency_key="existing-credential-1",
    )
    request = ExistingCredentialModelRegistrationRequest(
        tenant_id="tenant_001",
        user_id="user_123",
        credential_id=registration.credential_id,
        model=ManualModelRegistration(
            model="mock-new-chat",
            label="Mock New Chat",
            model_type=ModelType.TEXT_GENERATION,
            features={"streaming"},
        ),
    )

    first = await client.register_model_with_credential(request, identity=identity)
    replay = await client.register_model_with_credential(request, identity=identity)

    assert replay.configured_model_id == first.configured_model_id
    models = await client.list_models(
        ModelListQuery(tenant_id="tenant_001", user_id="user_123"),
        identity=identity,
    )
    added = next(item for item in models.items if item.model == "mock-new-chat")
    assert added.credential.credential_id == registration.credential_id


@pytest.mark.asyncio
async def test_register_model_with_credential_checks_credential_owner(
    client, identity, provider_ref
) -> None:
    registration = await client.register_model(
        registration_request(provider_ref),
        identity=identity,
        idempotency_key="existing-credential-owner",
    )
    other = CallerIdentity(tenant_id="tenant_001", user_id="user_999")
    with pytest.raises(ModelAccessException) as error:
        await client.register_model_with_credential(
            ExistingCredentialModelRegistrationRequest(
                tenant_id="tenant_001",
                user_id="user_999",
                credential_id=registration.credential_id,
                model=ManualModelRegistration(
                    model="forbidden-model",
                    model_type=ModelType.TEXT_GENERATION,
                ),
            ),
            identity=other,
        )
    assert error.value.code == ErrorCode.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_registration_idempotency_and_conflict(client, identity, provider_ref) -> None:
    first = await client.register_model(
        registration_request(provider_ref),
        identity=identity,
        idempotency_key="same-key",
    )
    replay = await client.register_model(
        registration_request(provider_ref),
        identity=identity,
        idempotency_key="same-key",
    )
    assert replay.credential_id == first.credential_id

    with pytest.raises(ModelAccessException) as error:
        await client.register_model(
            registration_request(provider_ref, api_key="different-key"),
            identity=identity,
            idempotency_key="same-key",
        )
    assert error.value.code == ErrorCode.IDEMPOTENCY_CONFLICT


@pytest.mark.asyncio
async def test_user_cannot_list_another_users_models(client, provider_ref) -> None:
    identity = CallerIdentity(tenant_id="tenant_001", user_id="user_123")
    await client.register_model(
        registration_request(provider_ref),
        identity=identity,
        idempotency_key="permission-1",
    )
    with pytest.raises(ModelAccessException) as error:
        await client.list_models(
            ModelListQuery(tenant_id="tenant_001", user_id="user_999"),
            identity=identity,
        )
    assert error.value.code == ErrorCode.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_stream_event_order(client, identity, provider_ref) -> None:
    await client.register_model(
        registration_request(provider_ref),
        identity=identity,
        idempotency_key="stream-1",
    )
    models = await client.list_models(
        ModelListQuery(
            tenant_id="tenant_001",
            user_id="user_123",
            model_type=ModelType.TEXT_GENERATION,
        ),
        identity=identity,
    )
    stream = await client.invoke(
        chat_request(models.items[0].configured_model_id, mode=ResponseMode.STREAMING),
        identity=identity,
    )
    assert hasattr(stream, "__aiter__")
    events = [item async for item in stream]  # type: ignore[union-attr]
    assert [item.event for item in events] == [
        "response.created",
        "output.delta",
        "output.delta",
        "usage",
        "response.completed",
    ]


@pytest.mark.asyncio
async def test_embedding_and_async_video(client, identity, provider_ref) -> None:
    admin = identity.model_copy(update={"roles": {"tenant_admin"}})
    client.configure_user_quota_template(
        UserQuotaTemplateInput(
            tenant_id="tenant_001",
            name="multimodal test quota",
            period_type=QuotaPeriodType.MONTH,
            credit_limit=Decimal("2000"),
            is_default=True,
        ),
        identity=admin,
    )
    await client.register_model(
        registration_request(provider_ref),
        identity=identity,
        idempotency_key="multi-type-1",
    )
    models = await client.list_models(
        ModelListQuery(tenant_id="tenant_001", user_id="user_123"),
        identity=identity,
    )
    ids = {item.model_type: item.configured_model_id for item in models.items}
    embedding = await client.invoke(
        ModelInvocationRequest(
            context=RuntimeContext(tenant_id="tenant_001", user_id="user_123"),
            model=ModelSelector(
                configured_model_id=ids[ModelType.EMBEDDING],
                model_type=ModelType.EMBEDDING,
            ),
            operation=ModelOperation.EMBEDDINGS,
            input=EmbeddingInput(texts=["alpha", "beta"]),
        ),
        identity=identity,
    )
    assert isinstance(embedding, InvocationResult)
    assert len(embedding.output["vectors"]) == 2

    video = await client.invoke(
        ModelInvocationRequest(
            context=RuntimeContext(tenant_id="tenant_001", user_id="user_123"),
            model=ModelSelector(
                configured_model_id=ids[ModelType.VIDEO_GENERATION],
                model_type=ModelType.VIDEO_GENERATION,
            ),
            operation=ModelOperation.VIDEO_GENERATE,
            response_mode=ResponseMode.ASYNC,
            input=VideoGenerationInput(prompt="factory digital twin"),
        ),
        identity=identity,
    )
    assert isinstance(video, AsyncInvocationResult)
    assert video.status == "ACCEPTED"


@pytest.mark.asyncio
async def test_invocation_id_cannot_cross_queries(client, identity, provider_ref) -> None:
    await client.register_model(
        registration_request(provider_ref),
        identity=identity,
        idempotency_key="binding-1",
    )
    models = await client.list_models(
        ModelListQuery(
            tenant_id="tenant_001",
            user_id="user_123",
            model_type=ModelType.TEXT_GENERATION,
        ),
        identity=identity,
    )
    first = chat_request(models.items[0].configured_model_id)
    first.context.invocation_id = "inv_stable"
    await client.invoke(first, identity=identity)

    second = chat_request(models.items[0].configured_model_id)
    second.context.invocation_id = "inv_stable"
    second.context.query_id = "query_2"
    with pytest.raises(ModelAccessException) as error:
        await client.invoke(second, identity=identity)
    assert error.value.code == ErrorCode.CONTEXT_INVALID
