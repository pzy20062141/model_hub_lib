from __future__ import annotations

import pytest

from model_access import ModelAccessException
from model_access.contracts.entities import CallerIdentity, CredentialInput
from model_access.contracts.enums import CredentialScope, ErrorCode, ModelType
from model_access.contracts.invocation import (
    ModelInvocationRequest,
    ModelListQuery,
    TenantDefaultModelUpdateRequest,
)
from model_access.contracts.responses import InvocationResult

from .helpers import chat_request, registration_request


def tenant_admin() -> CallerIdentity:
    return CallerIdentity(
        tenant_id="tenant_001",
        user_id="admin_001",
        roles={"tenant_admin"},
    )


def tenant_registration(provider_ref):  # type: ignore[no-untyped-def]
    request = registration_request(provider_ref)
    request.user_id = "admin_001"
    request.credential = CredentialInput(
        name="tenant mock credential",
        base_url="https://mock.local/v1",
        api_key="tenant-secret",
        scope=CredentialScope.TENANT,
    )
    return request


def default_update(configured_model_id: str | None) -> TenantDefaultModelUpdateRequest:
    return TenantDefaultModelUpdateRequest(
        tenant_id="tenant_001",
        configured_model_id=configured_model_id,
    )


def implicit_chat_request(*, user_id: str, query_id: str) -> ModelInvocationRequest:
    payload = chat_request("unused").model_dump(mode="json")
    payload.pop("model")
    payload["context"]["user_id"] = user_id
    payload["context"]["query_id"] = query_id
    return ModelInvocationRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_tenant_default_is_shared_and_used_when_model_is_omitted(
    client, provider_ref
) -> None:
    admin = tenant_admin()
    child = CallerIdentity(tenant_id="tenant_001", user_id="child_001")

    empty = await client.get_default_models(
        tenant_id="tenant_001",
        identity=child,
    )
    assert set(empty.defaults) == set(ModelType)
    assert all(configured_model_id is None for configured_model_id in empty.defaults.values())

    await client.register_model(
        tenant_registration(provider_ref),
        identity=admin,
        idempotency_key="tenant-default-models-1",
    )
    models = await client.list_models(
        ModelListQuery(tenant_id="tenant_001", user_id="child_001"),
        identity=child,
    )
    ids = {item.model_type: item.configured_model_id for item in models.items}

    defaults = await client.set_default_model(
        default_update(ids[ModelType.TEXT_GENERATION]),
        model_type=ModelType.TEXT_GENERATION,
        identity=admin,
    )
    assert defaults.defaults[ModelType.TEXT_GENERATION] == ids[ModelType.TEXT_GENERATION]
    assert defaults.defaults[ModelType.IMAGE_GENERATION] is None

    inherited = await client.get_default_models(
        tenant_id="tenant_001",
        identity=child,
    )
    assert inherited.defaults == defaults.defaults

    text_models = await client.list_models(
        ModelListQuery(
            tenant_id="tenant_001",
            user_id="child_001",
            model_type=ModelType.TEXT_GENERATION,
        ),
        identity=child,
    )
    assert text_models.default_models[ModelType.TEXT_GENERATION] == ids[ModelType.TEXT_GENERATION]
    assert text_models.items[0].is_default is True

    request = implicit_chat_request(user_id="child_001", query_id="tenant_default_query")
    assert request.model is not None and request.model.uses_default
    result = await client.invoke(request, identity=child)
    assert isinstance(result, InvocationResult)
    assert result.output["content"][0]["text"] == "mock response"


@pytest.mark.asyncio
async def test_tenant_default_can_be_cleared_and_type_is_validated(client, provider_ref) -> None:
    admin = tenant_admin()
    child = CallerIdentity(tenant_id="tenant_001", user_id="child_001")
    await client.register_model(
        tenant_registration(provider_ref),
        identity=admin,
        idempotency_key="tenant-default-models-2",
    )
    models = await client.list_models(
        ModelListQuery(tenant_id="tenant_001", user_id="child_001"),
        identity=child,
    )
    ids = {item.model_type: item.configured_model_id for item in models.items}

    with pytest.raises(ModelAccessException) as mismatch:
        await client.set_default_model(
            default_update(ids[ModelType.EMBEDDING]),
            model_type=ModelType.TEXT_GENERATION,
            identity=admin,
        )
    assert mismatch.value.code == ErrorCode.MODEL_TYPE_MISMATCH

    await client.set_default_model(
        default_update(ids[ModelType.TEXT_GENERATION]),
        model_type=ModelType.TEXT_GENERATION,
        identity=admin,
    )
    cleared = await client.set_default_model(
        default_update(None),
        model_type=ModelType.TEXT_GENERATION,
        identity=admin,
    )
    assert cleared.defaults[ModelType.TEXT_GENERATION] is None

    with pytest.raises(ModelAccessException) as missing:
        await client.invoke(
            implicit_chat_request(user_id="child_001", query_id="missing_default_query"),
            identity=child,
        )
    assert missing.value.code == ErrorCode.MODEL_NOT_FOUND


@pytest.mark.asyncio
async def test_only_admin_can_update_and_user_scoped_model_cannot_be_tenant_default(
    client, provider_ref
) -> None:
    user = CallerIdentity(tenant_id="tenant_001", user_id="user_123")
    admin = CallerIdentity(
        tenant_id="tenant_001",
        user_id="user_123",
        roles={"tenant_admin"},
    )
    registration = await client.register_model(
        registration_request(provider_ref),
        identity=user,
        idempotency_key="user-model-not-tenant-default",
    )

    with pytest.raises(ModelAccessException) as denied:
        await client.set_default_model(
            default_update(registration.configured_model_ids[0]),
            model_type=ModelType.TEXT_GENERATION,
            identity=user,
        )
    assert denied.value.code == ErrorCode.PERMISSION_DENIED

    model_admin = admin.model_copy(update={"roles": {"model_admin"}})
    with pytest.raises(ModelAccessException) as model_admin_denied:
        await client.set_default_model(
            default_update(registration.configured_model_ids[0]),
            model_type=ModelType.TEXT_GENERATION,
            identity=model_admin,
        )
    assert model_admin_denied.value.code == ErrorCode.PERMISSION_DENIED

    system_admin = admin.model_copy(update={"roles": {"system_admin"}})
    with pytest.raises(ModelAccessException) as system_admin_denied:
        await client.set_default_model(
            default_update(registration.configured_model_ids[0]),
            model_type=ModelType.TEXT_GENERATION,
            identity=system_admin,
        )
    assert system_admin_denied.value.code == ErrorCode.PERMISSION_DENIED

    with pytest.raises(ModelAccessException) as unavailable:
        await client.set_default_model(
            default_update(registration.configured_model_ids[0]),
            model_type=ModelType.TEXT_GENERATION,
            identity=admin,
        )
    assert unavailable.value.code == ErrorCode.MODEL_NOT_FOUND
