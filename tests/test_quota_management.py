from __future__ import annotations

import pytest
from sqlalchemy import select

from model_access import (
    FernetCredentialCipher,
    HostingConfiguration,
    ModelAccessException,
    ModelRepositoryClient,
    URLSecurityPolicy,
)
from model_access.adapters import MockProviderAdapter
from model_access.contracts.entities import CallerIdentity, CredentialInput
from model_access.contracts.enums import (
    CredentialScope,
    ErrorCode,
    ModelStatus,
    ModelType,
    ProviderQuotaType,
    ProviderType,
    QuotaUnit,
)
from model_access.contracts.invocation import ModelListQuery
from model_access.contracts.quota import HostingQuotaDefinition, ProviderQuotaPoolInput
from model_access.persistence.models import (
    ModelInvocationUsageRecord,
    ProviderQuotaRecord,
    QuotaReservationRecord,
)

from .helpers import chat_request, registration_request


class FailingMockProviderAdapter(MockProviderAdapter):
    async def invoke(self, invocation):  # type: ignore[no-untyped-def]
        del invocation
        raise ModelAccessException(
            ErrorCode.PROVIDER_UNAVAILABLE,
            "planned provider failure",
            retryable=False,
        )


def admin_identity() -> CallerIdentity:
    return CallerIdentity(
        tenant_id="tenant_001",
        user_id="user_123",
        roles={"system_admin"},
    )


def scoped_registration(provider_ref, scope: CredentialScope, name: str):  # type: ignore[no-untyped-def]
    request = registration_request(provider_ref)
    return request.model_copy(
        update={
            "credential": CredentialInput(
                name=name,
                base_url="https://mock.local/v1",
                api_key=f"{name}-secret",
                scope=scope,
            )
        }
    )


async def register_scope(client, provider_ref, scope: CredentialScope, key: str) -> str:  # type: ignore[no-untyped-def]
    result = await client.register_model(
        scoped_registration(provider_ref, scope, key),
        identity=admin_identity(),
        idempotency_key=key,
    )
    return result.configured_model_ids[0]


def add_pool(
    client,
    provider_ref,
    quota_type: ProviderQuotaType,
    limit: int,
    *,
    unit: QuotaUnit = QuotaUnit.TIMES,
):  # type: ignore[no-untyped-def]
    return client.configure_quota_pool(
        ProviderQuotaPoolInput(
            tenant_id="tenant_001",
            provider=provider_ref,
            quota_type=quota_type,
            quota_unit=unit,
            quota_limit=limit,
            restrict_models={"mock-chat"},
        ),
        identity=admin_identity(),
    )


@pytest.mark.asyncio
async def test_hosted_quota_priority_is_paid_free_trial(client, provider_ref) -> None:
    system_model_id = await register_scope(
        client, provider_ref, CredentialScope.SYSTEM, "system-priority"
    )
    trial = add_pool(client, provider_ref, ProviderQuotaType.TRIAL, 10)
    free = add_pool(client, provider_ref, ProviderQuotaType.FREE, 10)
    paid = add_pool(client, provider_ref, ProviderQuotaType.PAID, 1)
    client.set_provider_preference(
        tenant_id="tenant_001",
        provider=provider_ref,
        preferred_provider_type=ProviderType.SYSTEM,
        identity=admin_identity(),
    )

    await client.invoke(chat_request(system_model_id), identity=admin_identity())

    with client.repository._sessions() as session:
        pools = {item.quota_id: item for item in session.scalars(select(ProviderQuotaRecord)).all()}
    assert pools[paid.quota_id].quota_used == 1
    assert pools[paid.quota_id].is_valid is False
    assert pools[free.quota_id].quota_used == 0
    assert pools[trial.quota_id].quota_used == 0


@pytest.mark.asyncio
async def test_exhausted_system_quota_falls_back_and_invalidates_cache(
    client, provider_ref
) -> None:
    custom_model_id = await register_scope(
        client, provider_ref, CredentialScope.USER, "custom-fallback"
    )
    system_model_id = await register_scope(
        client, provider_ref, CredentialScope.SYSTEM, "system-fallback"
    )
    add_pool(client, provider_ref, ProviderQuotaType.PAID, 1)
    client.set_provider_preference(
        tenant_id="tenant_001",
        provider=provider_ref,
        preferred_provider_type=ProviderType.SYSTEM,
        identity=admin_identity(),
    )

    await client.invoke(chat_request(system_model_id), identity=admin_identity())
    models = await client.list_models(
        ModelListQuery(
            tenant_id="tenant_001",
            user_id="user_123",
            model_type=ModelType.TEXT_GENERATION,
        ),
        identity=admin_identity(),
    )
    system_item = next(item for item in models.items if item.provider_type == ProviderType.SYSTEM)
    assert system_item.status == ModelStatus.ACTIVE
    assert system_item.preferred_provider_type == ProviderType.SYSTEM
    assert system_item.using_provider_type == ProviderType.CUSTOM
    assert system_item.effective_configured_model_id == custom_model_id
    assert system_item.fallback_reason == "SYSTEM_QUOTA_EXHAUSTED"

    second = chat_request(system_model_id)
    second.context.query_id = "query_fallback"
    await client.invoke(second, identity=admin_identity())
    with client.repository._sessions() as session:
        reservation = session.get(QuotaReservationRecord, second.context.invocation_id)
        usage = session.get(ModelInvocationUsageRecord, second.context.invocation_id)
    assert reservation and reservation.provider_type == ProviderType.CUSTOM.value
    assert reservation.configured_model_id == custom_model_id
    assert usage and usage.configured_model_id == custom_model_id


@pytest.mark.asyncio
async def test_quota_exceeded_status_when_no_custom_fallback(client, provider_ref) -> None:
    system_model_id = await register_scope(
        client, provider_ref, CredentialScope.SYSTEM, "system-exhausted"
    )
    client.set_provider_preference(
        tenant_id="tenant_001",
        provider=provider_ref,
        preferred_provider_type=ProviderType.SYSTEM,
        identity=admin_identity(),
    )

    models = await client.list_models(
        ModelListQuery(
            tenant_id="tenant_001",
            user_id="user_123",
            model_type=ModelType.TEXT_GENERATION,
            status=ModelStatus.QUOTA_EXCEEDED,
        ),
        identity=admin_identity(),
    )
    assert len(models.items) == 1
    assert models.items[0].status == ModelStatus.QUOTA_EXCEEDED

    with pytest.raises(ModelAccessException) as error:
        await client.invoke(chat_request(system_model_id), identity=admin_identity())
    assert error.value.code == ErrorCode.QUOTA_EXCEEDED


@pytest.mark.asyncio
async def test_preferred_custom_without_credential_is_no_configure(client, provider_ref) -> None:
    system_model_id = await register_scope(
        client, provider_ref, CredentialScope.SYSTEM, "system-custom-preference"
    )
    add_pool(client, provider_ref, ProviderQuotaType.PAID, 5)
    client.set_provider_preference(
        tenant_id="tenant_001",
        provider=provider_ref,
        preferred_provider_type=ProviderType.CUSTOM,
        identity=admin_identity(),
    )

    models = await client.list_models(
        ModelListQuery(
            tenant_id="tenant_001",
            user_id="user_123",
            model_type=ModelType.TEXT_GENERATION,
            status=ModelStatus.NO_CONFIGURE,
        ),
        identity=admin_identity(),
    )
    assert len(models.items) == 1
    with pytest.raises(ModelAccessException) as error:
        await client.invoke(chat_request(system_model_id), identity=admin_identity())
    assert error.value.code == ErrorCode.CREDENTIAL_REQUIRED


@pytest.mark.asyncio
async def test_cloud_hosting_lazily_initializes_and_settles_token_quota(
    provider_ref, provider_descriptor, model_descriptors
) -> None:
    hosting = HostingConfiguration(
        edition="CLOUD",
        quotas=[
            HostingQuotaDefinition(
                provider=provider_ref,
                quota_type=ProviderQuotaType.TRIAL,
                quota_unit=QuotaUnit.TOKENS,
                quota_limit=1000,
                restrict_models={"mock-chat"},
            )
        ],
    )
    client = ModelRepositoryClient.sqlite(
        encryption_key=FernetCredentialCipher.generate_key(),
        url_policy=URLSecurityPolicy(allowed_hosts={"mock.local"}),
        hosting=hosting,
    )
    client.register_adapter(MockProviderAdapter(provider_descriptor, model_descriptors))
    system_model_id = await register_scope(
        client, provider_ref, CredentialScope.SYSTEM, "system-lazy-trial"
    )
    client.set_provider_preference(
        tenant_id="tenant_001",
        provider=provider_ref,
        preferred_provider_type=ProviderType.SYSTEM,
        identity=admin_identity(),
    )

    await client.invoke(chat_request(system_model_id), identity=admin_identity())
    with client.repository._sessions() as session:
        pool = session.scalar(select(ProviderQuotaRecord))
    assert pool is not None
    assert pool.quota_type == ProviderQuotaType.TRIAL.value
    assert pool.quota_used == 5
    assert pool.quota_reserved == 0
    assert pool.is_valid is True
    await client.close()


@pytest.mark.asyncio
async def test_failed_provider_call_releases_reserved_quota(
    client, provider_ref, provider_descriptor, model_descriptors
) -> None:
    system_model_id = await register_scope(
        client, provider_ref, CredentialScope.SYSTEM, "system-rollback"
    )
    pool_view = add_pool(client, provider_ref, ProviderQuotaType.PAID, 1)
    client.set_provider_preference(
        tenant_id="tenant_001",
        provider=provider_ref,
        preferred_provider_type=ProviderType.SYSTEM,
        identity=admin_identity(),
    )
    client.register_adapter(
        FailingMockProviderAdapter(provider_descriptor, model_descriptors),
        replace=True,
    )

    with pytest.raises(ModelAccessException) as error:
        await client.invoke(chat_request(system_model_id), identity=admin_identity())
    assert error.value.code == ErrorCode.PROVIDER_UNAVAILABLE
    with client.repository._sessions() as session:
        pool = session.get(ProviderQuotaRecord, pool_view.quota_id)
        reservation = session.scalar(select(QuotaReservationRecord))
    assert pool is not None and pool.quota_used == 0 and pool.quota_reserved == 0
    assert pool.is_valid is True
    assert reservation is not None and reservation.status == "RELEASED"


def test_quota_update_preserves_usage_and_supports_admin_listing(client, provider_ref) -> None:
    initial = client.configure_quota_pool(
        ProviderQuotaPoolInput(
            tenant_id="tenant_001",
            provider=provider_ref,
            quota_type=ProviderQuotaType.PAID,
            quota_unit=QuotaUnit.TIMES,
            quota_limit=10,
            quota_used=3,
            restrict_models={"mock-chat"},
        ),
        identity=admin_identity(),
    )
    updated = client.configure_quota_pool(
        ProviderQuotaPoolInput(
            quota_id=initial.quota_id,
            tenant_id="tenant_001",
            provider=provider_ref,
            quota_type=ProviderQuotaType.PAID,
            quota_unit=QuotaUnit.TIMES,
            quota_limit=20,
            restrict_models={"mock-chat"},
        ),
        identity=admin_identity(),
    )

    pools = client.list_quota_pools(
        tenant_id="tenant_001",
        provider=provider_ref,
        identity=admin_identity(),
    )
    assert updated.quota_used == 3
    assert updated.quota_remaining == 17
    assert [item.quota_id for item in pools] == [initial.quota_id]
