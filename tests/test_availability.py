from __future__ import annotations

import pytest

from model_access import ModelAccessException
from model_access.contracts.entities import CallerIdentity
from model_access.contracts.enums import ErrorCode, ModelStatus, ModelType
from model_access.contracts.invocation import (
    ConfiguredModelAvailabilityUpdateRequest,
    ModelListQuery,
    ProviderAvailabilityUpdateRequest,
)

from .helpers import chat_request, registration_request


def tenant_admin() -> CallerIdentity:
    return CallerIdentity(
        tenant_id="tenant_001",
        user_id="user_123",
        roles={"tenant_admin"},
    )


@pytest.mark.asyncio
async def test_model_switch_controls_catalog_and_invocation(client, provider_ref) -> None:
    admin = tenant_admin()
    await client.register_model(
        registration_request(provider_ref),
        identity=admin,
        idempotency_key="availability-model",
    )
    models = await client.list_models(
        ModelListQuery(
            tenant_id="tenant_001",
            user_id="user_123",
            model_type=ModelType.TEXT_GENERATION,
            status=None,
        ),
        identity=admin,
    )
    model_id = models.items[0].configured_model_id

    disabled = await client.set_model_availability(
        ConfiguredModelAvailabilityUpdateRequest(
            tenant_id="tenant_001",
            configured_model_id=model_id,
            enabled=False,
        ),
        identity=admin,
    )
    assert disabled.enabled is False
    assert disabled.status == ModelStatus.DISABLED

    listed = await client.list_models(
        ModelListQuery(
            tenant_id="tenant_001",
            user_id="user_123",
            model_type=ModelType.TEXT_GENERATION,
            status=None,
        ),
        identity=admin,
    )
    assert listed.items[0].model_enabled is False
    assert listed.items[0].provider_enabled is True
    assert listed.items[0].status == ModelStatus.DISABLED

    with pytest.raises(ModelAccessException) as error:
        await client.invoke(chat_request(model_id), identity=admin)
    assert error.value.code == ErrorCode.MODEL_DISABLED

    enabled = await client.set_model_availability(
        ConfiguredModelAvailabilityUpdateRequest(
            tenant_id="tenant_001",
            configured_model_id=model_id,
            enabled=True,
        ),
        identity=admin,
    )
    assert enabled.enabled is True
    await client.invoke(chat_request(model_id), identity=admin)


@pytest.mark.asyncio
async def test_provider_switch_preserves_individual_model_state(client, provider_ref) -> None:
    admin = tenant_admin()
    await client.register_model(
        registration_request(provider_ref),
        identity=admin,
        idempotency_key="availability-provider",
    )
    models = await client.list_models(
        ModelListQuery(
            tenant_id="tenant_001",
            user_id="user_123",
            status=None,
            page_size=20,
        ),
        identity=admin,
    )
    chat = next(item for item in models.items if item.model_type == ModelType.TEXT_GENERATION)
    embedding = next(item for item in models.items if item.model_type == ModelType.EMBEDDING)
    await client.set_model_availability(
        ConfiguredModelAvailabilityUpdateRequest(
            tenant_id="tenant_001",
            configured_model_id=embedding.configured_model_id,
            enabled=False,
        ),
        identity=admin,
    )

    provider_disabled = await client.set_provider_availability(
        ProviderAvailabilityUpdateRequest(
            tenant_id="tenant_001",
            provider=provider_ref,
            enabled=False,
        ),
        identity=admin,
    )
    assert provider_disabled.enabled is False
    disabled_models = await client.list_models(
        ModelListQuery(
            tenant_id="tenant_001",
            user_id="user_123",
            status=None,
            page_size=20,
        ),
        identity=admin,
    )
    assert all(item.status == ModelStatus.DISABLED for item in disabled_models.items)
    assert next(
        item for item in disabled_models.items if item.configured_model_id == chat.configured_model_id
    ).model_enabled is True

    with pytest.raises(ModelAccessException) as error:
        await client.invoke(chat_request(chat.configured_model_id), identity=admin)
    assert error.value.code == ErrorCode.MODEL_DISABLED

    await client.set_provider_availability(
        ProviderAvailabilityUpdateRequest(
            tenant_id="tenant_001",
            provider=provider_ref,
            enabled=True,
        ),
        identity=admin,
    )
    restored = await client.list_models(
        ModelListQuery(
            tenant_id="tenant_001",
            user_id="user_123",
            status=None,
            page_size=20,
        ),
        identity=admin,
    )
    restored_by_id = {item.configured_model_id: item for item in restored.items}
    assert restored_by_id[chat.configured_model_id].status == ModelStatus.ACTIVE
    assert restored_by_id[embedding.configured_model_id].status == ModelStatus.DISABLED


@pytest.mark.asyncio
async def test_only_model_admin_can_change_availability(client, identity, provider_ref) -> None:
    await client.register_model(
        registration_request(provider_ref),
        identity=identity,
        idempotency_key="availability-permissions",
    )
    models = await client.list_models(
        ModelListQuery(
            tenant_id="tenant_001",
            user_id="user_123",
            status=None,
        ),
        identity=identity,
    )
    with pytest.raises(ModelAccessException) as model_error:
        await client.set_model_availability(
            ConfiguredModelAvailabilityUpdateRequest(
                tenant_id="tenant_001",
                configured_model_id=models.items[0].configured_model_id,
                enabled=False,
            ),
            identity=identity,
        )
    assert model_error.value.code == ErrorCode.PERMISSION_DENIED

    with pytest.raises(ModelAccessException) as provider_error:
        await client.set_provider_availability(
            ProviderAvailabilityUpdateRequest(
                tenant_id="tenant_001",
                provider=provider_ref,
                enabled=False,
            ),
            identity=identity,
        )
    assert provider_error.value.code == ErrorCode.PERMISSION_DENIED
