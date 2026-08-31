from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from model_access import ModelAccessException
from model_access.adapters import MockProviderAdapter
from model_access.contracts.entities import CallerIdentity, CredentialInput
from model_access.contracts.enums import (
    CredentialScope,
    ErrorCode,
    QuotaOverrideMode,
    QuotaPeriodType,
    QuotaPolicySource,
    UserQuotaStatus,
)
from model_access.contracts.quota import (
    ModelCreditRateInput,
    RoleQuotaBindingInput,
    UserQuotaAssignmentInput,
    UserQuotaTemplateInput,
)
from model_access.persistence.models import (
    UserCostLedgerRecord,
    UserQuotaReservationRecord,
)

from .helpers import chat_request, registration_request


def tenant_admin() -> CallerIdentity:
    return CallerIdentity(tenant_id="tenant_001", user_id="user_123", roles={"tenant_admin"})


async def register_tenant_model(client, provider_ref) -> str:  # type: ignore[no-untyped-def]
    request = registration_request(provider_ref).model_copy(
        update={
            "credential": CredentialInput(
                name="tenant credential",
                base_url="https://mock.local/v1",
                api_key="tenant-secret",
                scope=CredentialScope.TENANT,
            )
        }
    )
    result = await client.register_model(
        request, identity=tenant_admin(), idempotency_key="tenant-model"
    )
    return result.configured_model_ids[0]


def default_template(client, limit: Decimal):  # type: ignore[no-untyped-def]
    return client.configure_user_quota_template(
        UserQuotaTemplateInput(
            tenant_id="tenant_001",
            name="monthly default",
            period_type=QuotaPeriodType.MONTH,
            credit_limit=limit,
            is_default=True,
        ),
        identity=tenant_admin(),
    )


@pytest.mark.asyncio
async def test_default_policy_is_100_monthly_credits_and_success_is_costed(
    client, provider_ref
) -> None:
    model_id = await register_tenant_model(client, provider_ref)
    await client.invoke(chat_request(model_id), identity=tenant_admin())

    summary = client.get_user_quota(
        tenant_id="tenant_001",
        user_id="user_123",
        roles=tenant_admin().roles,
        identity=tenant_admin(),
    )
    report = client.query_user_costs(
        tenant_id="tenant_001", user_id="user_123", identity=tenant_admin()
    )
    assert summary.status == UserQuotaStatus.ACTIVE
    assert summary.credit_limit == Decimal("100.000000")
    assert summary.credits_remaining == Decimal("99.000000")
    assert summary.credits_used == Decimal("1.000000")
    assert report.total_credits == Decimal("1.000000")

    aggregate = client.summarize_user_costs(
        tenant_id="tenant_001",
        user_id=None,
        identity=tenant_admin(),
    )
    assert aggregate.invocation_count == 1
    assert aggregate.total_credits == Decimal("1.000000")
    assert aggregate.by_user[0].user_id == "user_123"


@pytest.mark.asyncio
async def test_user_budget_prevents_second_call(client, provider_ref) -> None:
    model_id = await register_tenant_model(client, provider_ref)
    default_template(client, Decimal("1"))

    await client.invoke(chat_request(model_id), identity=tenant_admin())
    second = chat_request(model_id)
    second.context.query_id = "query_2"
    with pytest.raises(ModelAccessException) as error:
        await client.invoke(second, identity=tenant_admin())

    assert error.value.code == ErrorCode.QUOTA_EXCEEDED
    summary = client.get_user_quota(
        tenant_id="tenant_001",
        user_id="user_123",
        roles=set(),
        identity=tenant_admin(),
    )
    assert summary.source_type == QuotaPolicySource.TENANT_DEFAULT
    assert summary.status == UserQuotaStatus.EXCEEDED


@pytest.mark.asyncio
async def test_actual_cost_uses_rate_snapshot(client, provider_ref) -> None:
    model_id = await register_tenant_model(client, provider_ref)
    client.configure_model_credit_rate(
        ModelCreditRateInput(
            tenant_id="tenant_001",
            configured_model_id=model_id,
            per_request_credits=Decimal("1"),
            input_credits_per_1k=Decimal("100"),
            output_credits_per_1k=Decimal("100"),
        ),
        identity=tenant_admin(),
    )
    default_template(client, Decimal("100"))
    await client.invoke(chat_request(model_id), identity=tenant_admin())

    report = client.query_user_costs(
        tenant_id="tenant_001", user_id="user_123", identity=tenant_admin()
    )
    assert report.total_credits == Decimal("1.500000")
    with client.repository._sessions() as session:
        ledger = session.scalar(select(UserCostLedgerRecord))
    assert ledger is not None
    assert ledger.rate_snapshot["version"] == "1"


@pytest.mark.asyncio
async def test_failed_provider_call_releases_reservation(
    client, provider_ref, provider_descriptor, model_descriptors
) -> None:
    model_id = await register_tenant_model(client, provider_ref)
    default_template(client, Decimal("1"))

    class FailingAdapter(MockProviderAdapter):
        async def invoke(self, invocation):  # type: ignore[no-untyped-def]
            del invocation
            raise ModelAccessException(
                ErrorCode.PROVIDER_UNAVAILABLE, "planned failure", retryable=False
            )

    client.register_adapter(FailingAdapter(provider_descriptor, model_descriptors), replace=True)
    request = chat_request(model_id)
    with pytest.raises(ModelAccessException):
        await client.invoke(request, identity=tenant_admin())

    summary = client.get_user_quota(
        tenant_id="tenant_001",
        user_id="user_123",
        roles=set(),
        identity=tenant_admin(),
    )
    assert summary.credits_used == 0
    assert summary.credits_reserved == 0
    with client.repository._sessions() as session:
        reservation = session.get(UserQuotaReservationRecord, request.context.invocation_id)
    assert reservation is not None
    assert reservation.status == "RELEASED"


def test_role_template_and_user_override(client) -> None:  # type: ignore[no-untyped-def]
    role_template = client.configure_user_quota_template(
        UserQuotaTemplateInput(
            tenant_id="tenant_001",
            name="developer daily",
            period_type=QuotaPeriodType.DAY,
            credit_limit=Decimal("10"),
        ),
        identity=tenant_admin(),
    )
    client.bind_quota_template_to_role(
        RoleQuotaBindingInput(
            tenant_id="tenant_001",
            role_code="developer",
            template_id=role_template.template_id,
            priority=10,
        ),
        identity=tenant_admin(),
    )
    user = CallerIdentity(tenant_id="tenant_001", user_id="child_1", roles={"developer"})
    summary = client.get_user_quota(
        tenant_id="tenant_001",
        user_id="child_1",
        roles=user.roles,
        identity=user,
    )
    assert summary.source_type == QuotaPolicySource.ROLE
    assert summary.period_type == QuotaPeriodType.DAY
    assert summary.credit_limit == Decimal("10.000000")

    client.assign_user_quota(
        UserQuotaAssignmentInput(
            tenant_id="tenant_001",
            user_id="child_1",
            override_mode=QuotaOverrideMode.UNLIMITED,
        ),
        identity=tenant_admin(),
    )
    overridden = client.get_user_quota(
        tenant_id="tenant_001",
        user_id="child_1",
        roles=user.roles,
        identity=user,
    )
    assert overridden.source_type == QuotaPolicySource.USER
    assert overridden.status == UserQuotaStatus.UNLIMITED


def test_only_tenant_admin_can_manage_quota(client) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ModelAccessException) as error:
        client.configure_user_quota_template(
            UserQuotaTemplateInput(tenant_id="tenant_001", name="forbidden"),
            identity=CallerIdentity(tenant_id="tenant_001", user_id="user_123"),
        )
    assert error.value.code == ErrorCode.PERMISSION_DENIED
