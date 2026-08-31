from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from .contracts.entities import CallerIdentity
from .contracts.enums import (
    ErrorCode,
    QuotaOverrideMode,
    QuotaPeriodType,
    QuotaPolicySource,
    QuotaReservationStatus,
    UserQuotaStatus,
)
from .contracts.quota import (
    ModelCreditRateInput,
    ModelCreditRateView,
    RoleQuotaBindingInput,
    UserCostItem,
    UserCostReport,
    UserCostSummary,
    UserCostSummaryItem,
    UserQuotaAllocation,
    UserQuotaAssignmentInput,
    UserQuotaSummary,
    UserQuotaTemplateInput,
    UserQuotaTemplateView,
)
from .contracts.responses import Usage
from .errors import ModelAccessException
from .persistence.models import (
    ConfiguredModelRecord,
    ModelCreditRateRecord,
    UserCostLedgerRecord,
    UserQuotaAssignmentRecord,
    UserQuotaAuditRecord,
    UserQuotaPeriodRecord,
    UserQuotaReservationRecord,
    UserQuotaRoleBindingRecord,
    UserQuotaTemplateRecord,
)
from .persistence.repository import ModelAccessRepository

_SCALE = Decimal("0.000001")
_DEFAULT_CREDIT_LIMIT = Decimal("100")


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(_SCALE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class _Policy:
    period_type: QuotaPeriodType
    credit_limit: Decimal | None
    soft_limit_percent: int
    source_type: QuotaPolicySource
    source_id: str | None
    enabled: bool = True


class UserQuotaManager:
    """Child-user budget manager scoped strictly by ``tenant_id + user_id``.

    Provider quota, credential selection and hosted/custom fallback are deliberately
    outside this component. A missing tenant policy uses the platform's monthly
    100-credit default, while every successful invocation is still written to the
    per-user cost ledger.
    """

    def __init__(self, repository: ModelAccessRepository):
        self._repository = repository

    @staticmethod
    def _require_tenant_admin(identity: CallerIdentity, tenant_id: str) -> None:
        if identity.tenant_id != tenant_id or "tenant_admin" not in identity.roles:
            raise ModelAccessException(
                ErrorCode.PERMISSION_DENIED,
                "tenant_admin role is required to manage child-user quotas",
            )

    @staticmethod
    def _audit(session, identity: CallerIdentity, action: str, target: str, before, after) -> None:  # type: ignore[no-untyped-def]
        session.add(
            UserQuotaAuditRecord(
                audit_id=f"qau_{uuid4().hex}",
                tenant_id=identity.tenant_id,
                operator_user_id=identity.user_id,
                action=action,
                target_id=target,
                before=before,
                after=after,
            )
        )

    def configure_model_rate(
        self, config: ModelCreditRateInput, *, identity: CallerIdentity
    ) -> ModelCreditRateView:
        self._require_tenant_admin(identity, config.tenant_id)
        values = {
            "per_request_credits": _decimal(config.per_request_credits),
            "input_credits_per_1k": _decimal(config.input_credits_per_1k),
            "output_credits_per_1k": _decimal(config.output_credits_per_1k),
            "billable_unit_credits": _decimal(config.billable_unit_credits),
        }
        key = {
            "tenant_id": config.tenant_id,
            "configured_model_id": config.configured_model_id,
        }
        with self._repository._sessions.begin() as session:
            configured_model = session.get(ConfiguredModelRecord, config.configured_model_id)
            if configured_model is None or configured_model.tenant_id != config.tenant_id:
                raise ModelAccessException(
                    ErrorCode.MODEL_NOT_FOUND,
                    "configured model does not belong to this tenant",
                    field="configured_model_id",
                )
            record = session.get(ModelCreditRateRecord, key)
            before = self._rate_dict(record) if record else None
            if record is None:
                record = ModelCreditRateRecord(**key, **values)
                session.add(record)
            else:
                for name, value in values.items():
                    setattr(record, name, value)
                record.version += 1
                record.updated_at = datetime.now(UTC)
            session.flush()
            self._audit(
                session,
                identity,
                "MODEL_RATE_UPSERT",
                config.configured_model_id,
                before,
                self._rate_dict(record),
            )
            return self._rate_view(record)

    def configure_template(
        self, config: UserQuotaTemplateInput, *, identity: CallerIdentity
    ) -> UserQuotaTemplateView:
        self._require_tenant_admin(identity, config.tenant_id)
        template_id = config.template_id or f"qtpl_{uuid4().hex}"
        with self._repository._sessions.begin() as session:
            record = session.get(UserQuotaTemplateRecord, template_id)
            if record is not None and record.tenant_id != config.tenant_id:
                raise ModelAccessException(
                    ErrorCode.PERMISSION_DENIED, "quota template belongs to another tenant"
                )
            before = self._template_dict(record) if record else None
            if config.is_default:
                session.execute(
                    update(UserQuotaTemplateRecord)
                    .where(
                        UserQuotaTemplateRecord.tenant_id == config.tenant_id,
                        UserQuotaTemplateRecord.is_default.is_(True),
                        UserQuotaTemplateRecord.template_id != template_id,
                    )
                    .values(is_default=False, version=UserQuotaTemplateRecord.version + 1)
                )
            values = {
                "name": config.name,
                "period_type": config.period_type.value,
                "credit_limit": _decimal(config.credit_limit)
                if config.credit_limit is not None
                else None,
                "soft_limit_percent": config.soft_limit_percent,
                "is_default": config.is_default,
                "enabled": config.enabled,
            }
            if record is None:
                record = UserQuotaTemplateRecord(
                    template_id=template_id, tenant_id=config.tenant_id, **values
                )
                session.add(record)
            else:
                for name, value in values.items():
                    setattr(record, name, value)
                record.version += 1
                record.updated_at = datetime.now(UTC)
            session.flush()
            self._audit(
                session,
                identity,
                "QUOTA_TEMPLATE_UPSERT",
                template_id,
                before,
                self._template_dict(record),
            )
            return self._template_view(record)

    def bind_role(self, config: RoleQuotaBindingInput, *, identity: CallerIdentity) -> None:
        self._require_tenant_admin(identity, config.tenant_id)
        key = {"tenant_id": config.tenant_id, "role_code": config.role_code}
        with self._repository._sessions.begin() as session:
            template = session.get(UserQuotaTemplateRecord, config.template_id)
            if template is None or template.tenant_id != config.tenant_id:
                raise ModelAccessException(
                    ErrorCode.REQUEST_INVALID, "quota template not found", field="template_id"
                )
            record = session.get(UserQuotaRoleBindingRecord, key)
            before = (
                {"template_id": record.template_id, "priority": record.priority} if record else None
            )
            if record is None:
                record = UserQuotaRoleBindingRecord(
                    **key, template_id=config.template_id, priority=config.priority
                )
                session.add(record)
            else:
                record.template_id = config.template_id
                record.priority = config.priority
            self._audit(
                session,
                identity,
                "ROLE_QUOTA_BIND",
                config.role_code,
                before,
                {"template_id": config.template_id, "priority": config.priority},
            )

    def assign_user(self, config: UserQuotaAssignmentInput, *, identity: CallerIdentity) -> None:
        self._require_tenant_admin(identity, config.tenant_id)
        key = {"tenant_id": config.tenant_id, "user_id": config.user_id}
        with self._repository._sessions.begin() as session:
            if config.template_id:
                template = session.get(UserQuotaTemplateRecord, config.template_id)
                if template is None or template.tenant_id != config.tenant_id:
                    raise ModelAccessException(
                        ErrorCode.REQUEST_INVALID, "quota template not found", field="template_id"
                    )
            record = session.get(UserQuotaAssignmentRecord, key)
            before = self._assignment_dict(record) if record else None
            values = {
                "template_id": config.template_id,
                "override_mode": config.override_mode.value,
                "credit_limit": _decimal(config.credit_limit)
                if config.credit_limit is not None
                else None,
                "enabled": config.enabled,
            }
            if record is None:
                record = UserQuotaAssignmentRecord(**key, **values)
                session.add(record)
            else:
                for name, value in values.items():
                    setattr(record, name, value)
                record.version += 1
                record.updated_at = datetime.now(UTC)
            self._audit(
                session,
                identity,
                "USER_QUOTA_ASSIGN",
                config.user_id,
                before,
                self._assignment_dict(record),
            )

    async def acquire_user_quota(
        self,
        *,
        invocation_id: str,
        tenant_id: str,
        user_id: str,
        roles: set[str],
        configured_model_id: str,
        operation: str,
        estimated_usage: Usage,
    ) -> UserQuotaAllocation:
        now = datetime.now(UTC)
        try:
            with self._repository._sessions.begin() as session:
                existing = session.get(UserQuotaReservationRecord, invocation_id)
                if existing:
                    period = session.get(UserQuotaPeriodRecord, existing.period_id)
                    assert period is not None
                    return UserQuotaAllocation(
                        invocation_id=invocation_id,
                        configured_model_id=existing.configured_model_id,
                        user_id=existing.user_id,
                        reserved_credits=_decimal(existing.estimated_credits),
                        summary=self._summary(period),
                    )
                policy = self._resolve_policy(session, tenant_id, user_id, roles)
                if not policy.enabled:
                    raise ModelAccessException(
                        ErrorCode.QUOTA_EXCEEDED, "child-user quota is disabled"
                    )
                period = self._get_or_create_period(session, tenant_id, user_id, policy, now)
                rate = self._rate_snapshot(session, tenant_id, configured_model_id)
                estimated = self._calculate(rate, estimated_usage)
                statement = (
                    update(UserQuotaPeriodRecord)
                    .where(
                        UserQuotaPeriodRecord.period_id == period.period_id,
                        or_(
                            UserQuotaPeriodRecord.credit_limit.is_(None),
                            UserQuotaPeriodRecord.credits_used
                            + UserQuotaPeriodRecord.credits_reserved
                            + estimated
                            <= UserQuotaPeriodRecord.credit_limit,
                        ),
                    )
                    .values(
                        credits_reserved=UserQuotaPeriodRecord.credits_reserved + estimated,
                        version=UserQuotaPeriodRecord.version + 1,
                    )
                )
                if session.execute(statement).rowcount != 1:
                    raise ModelAccessException(
                        ErrorCode.QUOTA_EXCEEDED,
                        "child-user credit quota is exhausted",
                        extensions={"tenant_id": tenant_id, "user_id": user_id},
                    )
                session.add(
                    UserQuotaReservationRecord(
                        invocation_id=invocation_id,
                        period_id=period.period_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        configured_model_id=configured_model_id,
                        operation=operation,
                        estimated_credits=estimated,
                        estimated_usage=estimated_usage.model_dump(mode="json"),
                        rate_snapshot=rate,
                        status=QuotaReservationStatus.RESERVED.value,
                    )
                )
                session.flush()
                session.refresh(period)
                return UserQuotaAllocation(
                    invocation_id=invocation_id,
                    configured_model_id=configured_model_id,
                    user_id=user_id,
                    reserved_credits=estimated,
                    summary=self._summary(period),
                )
        except IntegrityError:
            with self._repository._sessions() as session:
                existing = session.get(UserQuotaReservationRecord, invocation_id)
                if existing is None:
                    raise
                period = session.get(UserQuotaPeriodRecord, existing.period_id)
                assert period is not None
                return UserQuotaAllocation(
                    invocation_id=invocation_id,
                    configured_model_id=existing.configured_model_id,
                    user_id=existing.user_id,
                    reserved_credits=_decimal(existing.estimated_credits),
                    summary=self._summary(period),
                )

    async def reserve(self, **_: Any) -> None:
        raise RuntimeError("UserQuotaManager must be called through acquire_user_quota")

    async def settle(self, *, invocation_id: str, usage: Usage | None, succeeded: bool) -> None:
        with self._repository._sessions.begin() as session:
            reservation = session.scalar(
                select(UserQuotaReservationRecord)
                .where(UserQuotaReservationRecord.invocation_id == invocation_id)
                .with_for_update()
            )
            if reservation is None or reservation.status != QuotaReservationStatus.RESERVED.value:
                return
            period = session.scalar(
                select(UserQuotaPeriodRecord)
                .where(UserQuotaPeriodRecord.period_id == reservation.period_id)
                .with_for_update()
            )
            if period is None:
                raise RuntimeError("quota period no longer exists")
            actual_usage = usage
            if succeeded and actual_usage is None and reservation.estimated_usage:
                actual_usage = Usage.model_validate(reservation.estimated_usage)
            actual = (
                self._calculate(reservation.rate_snapshot, actual_usage)
                if succeeded and actual_usage
                else Decimal("0")
            )
            period.credits_reserved = max(
                Decimal("0"),
                _decimal(period.credits_reserved) - _decimal(reservation.estimated_credits),
            )
            period.credits_used = _decimal(period.credits_used) + actual
            period.version += 1
            reservation.actual_credits = actual
            reservation.status = (
                QuotaReservationStatus.SETTLED.value
                if succeeded
                else QuotaReservationStatus.RELEASED.value
            )
            reservation.settled_at = datetime.now(UTC)
            if succeeded:
                session.add(
                    UserCostLedgerRecord(
                        invocation_id=invocation_id,
                        tenant_id=reservation.tenant_id,
                        user_id=reservation.user_id,
                        configured_model_id=reservation.configured_model_id,
                        operation=reservation.operation,
                        usage=actual_usage.model_dump(mode="json") if actual_usage else None,
                        credits=actual,
                        rate_snapshot=reservation.rate_snapshot,
                    )
                )

    def get_summary(
        self,
        *,
        tenant_id: str,
        user_id: str,
        roles: set[str],
        identity: CallerIdentity,
    ) -> UserQuotaSummary:
        if identity.tenant_id != tenant_id or (
            identity.user_id != user_id and "tenant_admin" not in identity.roles
        ):
            raise ModelAccessException(
                ErrorCode.PERMISSION_DENIED, "cannot view another user's quota"
            )
        if "tenant_admin" not in identity.roles:
            roles = identity.roles
        now = datetime.now(UTC)
        with self._repository._sessions.begin() as session:
            policy = self._resolve_policy(session, tenant_id, user_id, roles)
            period = self._get_or_create_period(session, tenant_id, user_id, policy, now)
            return self._summary(period, enabled=policy.enabled)

    def query_costs(
        self,
        *,
        tenant_id: str,
        user_id: str | None,
        identity: CallerIdentity,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        limit: int = 100,
    ) -> UserCostReport:
        if identity.tenant_id != tenant_id:
            raise ModelAccessException(ErrorCode.PERMISSION_DENIED, "tenant identity mismatch")
        if "tenant_admin" not in identity.roles:
            if not identity.user_id:
                raise ModelAccessException(
                    ErrorCode.PERMISSION_DENIED,
                    "a user identity is required to query personal costs",
                )
            if user_id not in {None, identity.user_id}:
                raise ModelAccessException(
                    ErrorCode.PERMISSION_DENIED, "cannot view another user's costs"
                )
            user_id = identity.user_id
        with self._repository._sessions() as session:
            statement = select(UserCostLedgerRecord).where(
                UserCostLedgerRecord.tenant_id == tenant_id
            )
            if user_id:
                statement = statement.where(UserCostLedgerRecord.user_id == user_id)
            if start_at:
                statement = statement.where(UserCostLedgerRecord.created_at >= start_at)
            if end_at:
                statement = statement.where(UserCostLedgerRecord.created_at < end_at)
            records = list(
                session.scalars(
                    statement.order_by(UserCostLedgerRecord.created_at.desc()).limit(
                        min(max(limit, 1), 1000)
                    )
                )
            )
            items = [
                UserCostItem(
                    invocation_id=item.invocation_id,
                    tenant_id=item.tenant_id,
                    user_id=item.user_id,
                    configured_model_id=item.configured_model_id,
                    operation=item.operation,
                    credits=_decimal(item.credits),
                    usage=item.usage,
                    created_at=item.created_at,
                )
                for item in records
            ]
            return UserCostReport(
                items=items, total_credits=sum((item.credits for item in items), Decimal("0"))
            )

    def summarize_costs(
        self,
        *,
        tenant_id: str,
        user_id: str | None,
        identity: CallerIdentity,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> UserCostSummary:
        if identity.tenant_id != tenant_id:
            raise ModelAccessException(ErrorCode.PERMISSION_DENIED, "tenant identity mismatch")
        if "tenant_admin" not in identity.roles:
            if not identity.user_id:
                raise ModelAccessException(
                    ErrorCode.PERMISSION_DENIED,
                    "a user identity is required to query personal costs",
                )
            if user_id not in {None, identity.user_id}:
                raise ModelAccessException(
                    ErrorCode.PERMISSION_DENIED, "cannot view another user's costs"
                )
            user_id = identity.user_id
        with self._repository._sessions() as session:
            statement = (
                select(
                    UserCostLedgerRecord.user_id,
                    func.count(UserCostLedgerRecord.invocation_id),
                    func.sum(UserCostLedgerRecord.credits),
                )
                .where(UserCostLedgerRecord.tenant_id == tenant_id)
                .group_by(UserCostLedgerRecord.user_id)
                .order_by(UserCostLedgerRecord.user_id)
            )
            if user_id:
                statement = statement.where(UserCostLedgerRecord.user_id == user_id)
            if start_at:
                statement = statement.where(UserCostLedgerRecord.created_at >= start_at)
            if end_at:
                statement = statement.where(UserCostLedgerRecord.created_at < end_at)
            items = [
                UserCostSummaryItem(
                    user_id=row_user_id,
                    invocation_count=int(invocation_count),
                    total_credits=_decimal(total_credits),
                )
                for row_user_id, invocation_count, total_credits in session.execute(statement)
            ]
        return UserCostSummary(
            tenant_id=tenant_id,
            user_id=user_id,
            invocation_count=sum(item.invocation_count for item in items),
            total_credits=sum((item.total_credits for item in items), Decimal("0")),
            by_user=items,
        )

    @staticmethod
    def _period_bounds(period_type: QuotaPeriodType, now: datetime) -> tuple[datetime, datetime]:
        now = now.astimezone(UTC)
        if period_type == QuotaPeriodType.DAY:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return start, start + timedelta(days=1)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return start, end

    def _resolve_policy(self, session, tenant_id: str, user_id: str, roles: set[str]) -> _Policy:  # type: ignore[no-untyped-def]
        assignment = session.get(
            UserQuotaAssignmentRecord, {"tenant_id": tenant_id, "user_id": user_id}
        )
        if assignment:
            assigned_template = (
                session.get(UserQuotaTemplateRecord, assignment.template_id)
                if assignment.template_id
                else None
            )
            assigned_period = (
                QuotaPeriodType(assigned_template.period_type)
                if assigned_template and assigned_template.enabled
                else QuotaPeriodType.MONTH
            )
            assigned_soft_limit = (
                assigned_template.soft_limit_percent
                if assigned_template and assigned_template.enabled
                else 80
            )
            if not assignment.enabled:
                return _Policy(
                    assigned_period,
                    Decimal("0"),
                    assigned_soft_limit,
                    QuotaPolicySource.USER,
                    user_id,
                    False,
                )
            if assignment.override_mode == QuotaOverrideMode.LIMITED.value:
                return _Policy(
                    assigned_period,
                    _decimal(assignment.credit_limit),
                    assigned_soft_limit,
                    QuotaPolicySource.USER,
                    user_id,
                )
            if assignment.override_mode == QuotaOverrideMode.UNLIMITED.value:
                return _Policy(
                    assigned_period,
                    None,
                    assigned_soft_limit,
                    QuotaPolicySource.USER,
                    user_id,
                )
            if assignment.template_id:
                template = assigned_template
                if template and template.enabled:
                    return self._template_policy(template, QuotaPolicySource.USER, user_id)
        if roles:
            row = session.execute(
                select(UserQuotaRoleBindingRecord, UserQuotaTemplateRecord)
                .join(
                    UserQuotaTemplateRecord,
                    UserQuotaRoleBindingRecord.template_id == UserQuotaTemplateRecord.template_id,
                )
                .where(
                    UserQuotaRoleBindingRecord.tenant_id == tenant_id,
                    UserQuotaRoleBindingRecord.role_code.in_(sorted(roles)),
                    UserQuotaTemplateRecord.enabled.is_(True),
                )
                .order_by(
                    UserQuotaRoleBindingRecord.priority.desc(),
                    UserQuotaRoleBindingRecord.role_code.asc(),
                )
            ).first()
            if row:
                binding, template = row
                return self._template_policy(template, QuotaPolicySource.ROLE, binding.role_code)
        template = session.scalar(
            select(UserQuotaTemplateRecord).where(
                UserQuotaTemplateRecord.tenant_id == tenant_id,
                UserQuotaTemplateRecord.is_default.is_(True),
                UserQuotaTemplateRecord.enabled.is_(True),
            )
        )
        if template:
            return self._template_policy(
                template, QuotaPolicySource.TENANT_DEFAULT, template.template_id
            )
        return _Policy(
            QuotaPeriodType.MONTH,
            _DEFAULT_CREDIT_LIMIT,
            80,
            QuotaPolicySource.PLATFORM_DEFAULT,
            None,
        )

    @staticmethod
    def _template_policy(
        template: UserQuotaTemplateRecord, source: QuotaPolicySource, source_id: str
    ) -> _Policy:
        return _Policy(
            QuotaPeriodType(template.period_type),
            _decimal(template.credit_limit) if template.credit_limit is not None else None,
            template.soft_limit_percent,
            source,
            source_id,
        )

    def _get_or_create_period(
        self, session, tenant_id: str, user_id: str, policy: _Policy, now: datetime
    ) -> UserQuotaPeriodRecord:  # type: ignore[no-untyped-def]
        start, end = self._period_bounds(policy.period_type, now)
        digest = hashlib.sha256(
            f"{tenant_id}\0{user_id}\0{start.isoformat()}\0{end.isoformat()}".encode()
        ).hexdigest()[:40]
        period_id = f"qprd_{digest}"
        period = session.get(UserQuotaPeriodRecord, period_id)
        if period is None:
            period = UserQuotaPeriodRecord(
                period_id=period_id,
                tenant_id=tenant_id,
                user_id=user_id,
                period_type=policy.period_type.value,
                period_start=start,
                period_end=end,
                credit_limit=policy.credit_limit,
                credits_used=Decimal("0"),
                credits_reserved=Decimal("0"),
                source_type=policy.source_type.value,
                source_id=policy.source_id,
                soft_limit_percent=policy.soft_limit_percent,
            )
            session.add(period)
            session.flush()
        else:
            period.credit_limit = policy.credit_limit
            period.source_type = policy.source_type.value
            period.source_id = policy.source_id
            period.soft_limit_percent = policy.soft_limit_percent
        return period

    @staticmethod
    def _rate_snapshot(session, tenant_id: str, configured_model_id: str) -> dict[str, str]:  # type: ignore[no-untyped-def]
        rate = session.get(
            ModelCreditRateRecord,
            {"tenant_id": tenant_id, "configured_model_id": configured_model_id},
        )
        if rate is None:
            return {
                "per_request_credits": "1",
                "input_credits_per_1k": "0",
                "output_credits_per_1k": "0",
                "billable_unit_credits": "0",
                "version": "0",
            }
        return {
            "per_request_credits": str(rate.per_request_credits),
            "input_credits_per_1k": str(rate.input_credits_per_1k),
            "output_credits_per_1k": str(rate.output_credits_per_1k),
            "billable_unit_credits": str(rate.billable_unit_credits),
            "version": str(rate.version),
        }

    @staticmethod
    def _calculate(rate: dict[str, Any], usage: Usage | None) -> Decimal:
        if usage is None:
            return _decimal(rate.get("per_request_credits"))
        result = _decimal(rate.get("per_request_credits"))
        result += (
            _decimal(rate.get("input_credits_per_1k"))
            * Decimal(usage.input_tokens or 0)
            / Decimal(1000)
        )
        result += (
            _decimal(rate.get("output_credits_per_1k"))
            * Decimal(usage.output_tokens or 0)
            / Decimal(1000)
        )
        result += _decimal(rate.get("billable_unit_credits")) * Decimal(
            str(usage.billable_units or 0)
        )
        return result.quantize(_SCALE, rounding=ROUND_HALF_UP)

    @staticmethod
    def _summary(period: UserQuotaPeriodRecord, *, enabled: bool = True) -> UserQuotaSummary:
        used = _decimal(period.credits_used)
        reserved = _decimal(period.credits_reserved)
        limit = _decimal(period.credit_limit) if period.credit_limit is not None else None
        if not enabled:
            status = UserQuotaStatus.DISABLED
        elif limit is None:
            status = UserQuotaStatus.UNLIMITED
        elif used + reserved >= limit:
            status = UserQuotaStatus.EXCEEDED
        elif limit > 0 and (used + reserved) * 100 >= limit * period.soft_limit_percent:
            status = UserQuotaStatus.SOFT_LIMIT
        else:
            status = UserQuotaStatus.ACTIVE
        return UserQuotaSummary(
            tenant_id=period.tenant_id,
            user_id=period.user_id,
            status=status,
            source_type=QuotaPolicySource(period.source_type),
            source_id=period.source_id,
            period_type=QuotaPeriodType(period.period_type),
            period_start=period.period_start,
            period_end=period.period_end,
            credit_limit=limit,
            credits_used=used,
            credits_reserved=reserved,
            credits_remaining=None if limit is None else max(Decimal("0"), limit - used - reserved),
            soft_limit_percent=period.soft_limit_percent,
        )

    @staticmethod
    def _rate_dict(record: ModelCreditRateRecord) -> dict[str, Any]:
        return {
            "per_request_credits": str(record.per_request_credits),
            "input_credits_per_1k": str(record.input_credits_per_1k),
            "output_credits_per_1k": str(record.output_credits_per_1k),
            "billable_unit_credits": str(record.billable_unit_credits),
            "version": record.version,
        }

    @classmethod
    def _rate_view(cls, record: ModelCreditRateRecord) -> ModelCreditRateView:
        return ModelCreditRateView(
            tenant_id=record.tenant_id,
            configured_model_id=record.configured_model_id,
            per_request_credits=record.per_request_credits,
            input_credits_per_1k=record.input_credits_per_1k,
            output_credits_per_1k=record.output_credits_per_1k,
            billable_unit_credits=record.billable_unit_credits,
            version=record.version,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _template_dict(record: UserQuotaTemplateRecord) -> dict[str, Any]:
        return {
            "name": record.name,
            "period_type": record.period_type,
            "credit_limit": str(record.credit_limit) if record.credit_limit is not None else None,
            "soft_limit_percent": record.soft_limit_percent,
            "is_default": record.is_default,
            "enabled": record.enabled,
            "version": record.version,
        }

    @classmethod
    def _template_view(cls, record: UserQuotaTemplateRecord) -> UserQuotaTemplateView:
        return UserQuotaTemplateView(
            tenant_id=record.tenant_id,
            template_id=record.template_id,
            name=record.name,
            period_type=record.period_type,
            credit_limit=record.credit_limit,
            soft_limit_percent=record.soft_limit_percent,
            is_default=record.is_default,
            enabled=record.enabled,
            version=record.version,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _assignment_dict(record: UserQuotaAssignmentRecord) -> dict[str, Any]:
        return {
            "template_id": record.template_id,
            "override_mode": record.override_mode,
            "credit_limit": str(record.credit_limit) if record.credit_limit is not None else None,
            "enabled": record.enabled,
            "version": record.version,
        }
