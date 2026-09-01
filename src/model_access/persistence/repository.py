from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from ..contracts.entities import CallerIdentity, ModelDescriptor, ProviderDescriptor
from ..contracts.enums import (
    CredentialScope,
    CredentialStatus,
    ModelStatus,
    ModelType,
    ProviderQuotaType,
    ProviderType,
    QuotaReservationStatus,
    QuotaUnit,
)
from ..contracts.invocation import ModelListQuery
from ..contracts.responses import RegistrationResult, Usage
from .models import (
    Base,
    ConfigurationSourceVersionRecord,
    ConfiguredModelRecord,
    IdempotencyRecord,
    InvocationBindingRecord,
    ModelAccessOutboxRecord,
    ModelInvocationUsageRecord,
    ModelRegistrationAuditRecord,
    ProviderCredentialRecord,
    ProviderPreferenceRecord,
    ProviderQuotaRecord,
    QuotaReservationRecord,
    TenantDefaultModelRecord,
)


@dataclass(slots=True)
class ResolvedModelRecord:
    model: ConfiguredModelRecord
    credential: ProviderCredentialRecord


@dataclass(slots=True)
class IdempotencyValue:
    request_hash: str
    result: RegistrationResult


class ModelAccessRepository:
    def __init__(self, engine: Engine, *, create_schema: bool = False):
        self.engine = engine
        self._sessions = sessionmaker(engine, expire_on_commit=False)
        if create_schema:
            Base.metadata.create_all(engine)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    @staticmethod
    def provider_source_key(tenant_id: str, plugin_id: str, provider_id: str) -> str:
        return f"provider:{tenant_id}:{plugin_id}:{provider_id}"

    @staticmethod
    def _bump_source_version(session, source_key: str) -> int:  # type: ignore[no-untyped-def]
        version = session.scalar(
            update(ConfigurationSourceVersionRecord)
            .where(ConfigurationSourceVersionRecord.source_key == source_key)
            .values(version=ConfigurationSourceVersionRecord.version + 1)
            .returning(ConfigurationSourceVersionRecord.version)
        )
        if version is not None:
            return int(version)
        session.add(ConfigurationSourceVersionRecord(source_key=source_key, version=1))
        return 1

    @staticmethod
    def _add_outbox(
        session,  # type: ignore[no-untyped-def]
        *,
        source_key: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        session.add(
            ModelAccessOutboxRecord(
                event_id=f"evt_{uuid4().hex}",
                aggregate_type="PROVIDER_CONFIGURATION",
                aggregate_id=source_key,
                event_type=event_type,
                payload=payload,
            )
        )

    def get_idempotency(self, tenant_id: str, user_id: str, key: str) -> IdempotencyValue | None:
        with self._sessions() as session:
            record = session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.tenant_id == tenant_id,
                    IdempotencyRecord.user_id == user_id,
                    IdempotencyRecord.idempotency_key == key,
                )
            )
            if not record:
                return None
            return IdempotencyValue(
                request_hash=record.request_hash,
                result=RegistrationResult.model_validate(record.response_json),
            )

    def save_registration(
        self,
        *,
        credential_id: str,
        registration_id: str,
        tenant_id: str,
        owner_user_id: str,
        provider: ProviderDescriptor,
        credential_name: str,
        base_url: str,
        encrypted_values: str,
        api_key_masked: str,
        scope: CredentialScope,
        deployment: dict[str, Any] | None,
        models: list[tuple[str, ModelDescriptor]],
        source: str,
        request_hash: str,
        idempotency_key: str,
        request_id: str | None,
        trace_id: str | None,
        result: RegistrationResult,
    ) -> None:
        with self._sessions.begin() as session:
            credential = ProviderCredentialRecord(
                credential_id=credential_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                plugin_id=provider.provider.plugin_id,
                provider_id=provider.provider.provider_id,
                name=credential_name,
                base_url=base_url,
                encrypted_values=encrypted_values,
                api_key_masked=api_key_masked,
                scope=scope.value,
                status=CredentialStatus.VALID.value,
                deployment=deployment,
            )
            session.add(credential)
            # These mapped classes deliberately do not expose ORM relationships.
            # Flush the FK parent explicitly so PostgreSQL never schedules a
            # configured_model insert before its provider_credential row.
            session.flush([credential])
            for configured_model_id, descriptor in models:
                session.add(
                    ConfiguredModelRecord(
                        configured_model_id=configured_model_id,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        credential_id=credential_id,
                        plugin_id=descriptor.provider.plugin_id,
                        provider_id=descriptor.provider.provider_id,
                        provider_display_name=provider.display_name.default,
                        model=descriptor.model,
                        label=descriptor.label,
                        model_type=descriptor.model_type.value,
                        categories=sorted(item.value for item in descriptor.categories),
                        input_modalities=sorted(descriptor.input_modalities),
                        output_modalities=sorted(descriptor.output_modalities),
                        features=sorted(descriptor.features),
                        operations=sorted(item.value for item in descriptor.operations),
                        properties=descriptor.properties,
                        parameter_schema=descriptor.parameter_schema,
                        context_window=descriptor.context_window,
                        max_output_tokens=descriptor.max_output_tokens,
                        protocol_versions=sorted(descriptor.protocol_versions),
                        status=descriptor.status.value,
                        source=source,
                    )
                )
            session.add(
                ModelRegistrationAuditRecord(
                    audit_id=registration_id,
                    tenant_id=tenant_id,
                    operator_user_id=owner_user_id,
                    credential_id=credential_id,
                    action="REGISTER",
                    result="SUCCEEDED",
                    request_id=request_id,
                    trace_id=trace_id,
                    details={
                        "provider_id": provider.provider.provider_id,
                        "scope": scope.value,
                        "configured_model_count": len(models),
                    },
                )
            )
            session.add(
                IdempotencyRecord(
                    record_id=f"idem_{registration_id.removeprefix('reg_')}",
                    tenant_id=tenant_id,
                    user_id=owner_user_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    response_json=result.model_dump(mode="json"),
                )
            )
            source_key = self.provider_source_key(
                tenant_id,
                provider.provider.plugin_id,
                provider.provider.provider_id,
            )
            self._bump_source_version(session, source_key)
            self._add_outbox(
                session,
                source_key=source_key,
                event_type="PROVIDER_CREDENTIAL_CHANGED",
                payload={"credential_id": credential_id, "scope": scope.value},
            )

    def get_provider_credential(self, credential_id: str) -> ProviderCredentialRecord | None:
        with self._sessions() as session:
            return session.get(ProviderCredentialRecord, credential_id)

    def add_model_to_credential(
        self,
        *,
        configured_model_id: str,
        credential: ProviderCredentialRecord,
        provider: ProviderDescriptor,
        descriptor: ModelDescriptor,
        operator_user_id: str,
    ) -> str:
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(ConfiguredModelRecord).where(
                    ConfiguredModelRecord.credential_id == credential.credential_id,
                    ConfiguredModelRecord.model == descriptor.model,
                    ConfiguredModelRecord.model_type == descriptor.model_type.value,
                )
            )
            if existing is not None:
                return existing.configured_model_id
            session.add(
                ConfiguredModelRecord(
                    configured_model_id=configured_model_id,
                    tenant_id=credential.tenant_id,
                    owner_user_id=credential.owner_user_id,
                    credential_id=credential.credential_id,
                    plugin_id=descriptor.provider.plugin_id,
                    provider_id=descriptor.provider.provider_id,
                    provider_display_name=provider.display_name.default,
                    model=descriptor.model,
                    label=descriptor.label,
                    model_type=descriptor.model_type.value,
                    categories=sorted(item.value for item in descriptor.categories),
                    input_modalities=sorted(descriptor.input_modalities),
                    output_modalities=sorted(descriptor.output_modalities),
                    features=sorted(descriptor.features),
                    operations=sorted(item.value for item in descriptor.operations),
                    properties=descriptor.properties,
                    parameter_schema=descriptor.parameter_schema,
                    context_window=descriptor.context_window,
                    max_output_tokens=descriptor.max_output_tokens,
                    protocol_versions=sorted(descriptor.protocol_versions),
                    status=descriptor.status.value,
                    source="PROVIDER",
                )
            )
            audit_id = f"reg_{uuid4().hex}"
            session.add(
                ModelRegistrationAuditRecord(
                    audit_id=audit_id,
                    tenant_id=credential.tenant_id,
                    operator_user_id=operator_user_id,
                    credential_id=credential.credential_id,
                    action="REGISTER_MODEL",
                    result="SUCCEEDED",
                    details={
                        "provider_id": descriptor.provider.provider_id,
                        "configured_model_id": configured_model_id,
                        "model": descriptor.model,
                        "model_type": descriptor.model_type.value,
                    },
                )
            )
            source_key = self.provider_source_key(
                credential.tenant_id,
                descriptor.provider.plugin_id,
                descriptor.provider.provider_id,
            )
            self._bump_source_version(session, source_key)
            self._add_outbox(
                session,
                source_key=source_key,
                event_type="CONFIGURED_MODEL_CHANGED",
                payload={
                    "credential_id": credential.credential_id,
                    "configured_model_id": configured_model_id,
                },
            )
            session.flush()
            return configured_model_id

    def list_models(
        self,
        *,
        query: ModelListQuery,
        identity: CallerIdentity,
        offset: int,
        paginate: bool = True,
    ) -> tuple[list[ResolvedModelRecord], int | None]:
        with self._sessions() as session:
            statement = (
                select(ConfiguredModelRecord, ProviderCredentialRecord)
                .join(
                    ProviderCredentialRecord,
                    ConfiguredModelRecord.credential_id == ProviderCredentialRecord.credential_id,
                )
                .where(ConfiguredModelRecord.tenant_id == query.tenant_id)
                .where(
                    or_(
                        and_(
                            ProviderCredentialRecord.scope == CredentialScope.USER.value,
                            ProviderCredentialRecord.owner_user_id == query.user_id,
                        ),
                        ProviderCredentialRecord.scope.in_(
                            [CredentialScope.TENANT.value, CredentialScope.SYSTEM.value]
                        ),
                    )
                )
            )
            if query.category:
                # JSON containment differs by database, so use a conservative
                # post-filter below while keeping all other filters in SQL.
                pass
            if query.model_type:
                statement = statement.where(
                    ConfiguredModelRecord.model_type == query.model_type.value
                )
            if query.provider_id:
                statement = statement.where(ConfiguredModelRecord.provider_id == query.provider_id)
            if query.status and query.status.value not in {
                "QUOTA_EXCEEDED",
                "NO_CONFIGURE",
            }:
                statement = statement.where(ConfiguredModelRecord.status == query.status.value)
            statement = statement.order_by(
                ConfiguredModelRecord.provider_id,
                ConfiguredModelRecord.model,
                ConfiguredModelRecord.configured_model_id,
            )
            rows = session.execute(statement).all()
            resolved = [
                ResolvedModelRecord(model=model, credential=credential)
                for model, credential in rows
            ]
            if query.category:
                resolved = [row for row in resolved if query.category.value in row.model.categories]
            if not paginate:
                return resolved, None
            page = resolved[offset : offset + query.page_size + 1]
            next_offset = offset + query.page_size if len(page) > query.page_size else None
            return page[: query.page_size], next_offset

    def resolve_model(
        self,
        *,
        configured_model_id: str | None,
        tenant_id: str,
        user_id: str | None,
        provider_key: tuple[str, str] | None = None,
        model: str | None = None,
        model_type: str | None = None,
    ) -> ResolvedModelRecord | None:
        with self._sessions() as session:
            statement = (
                select(ConfiguredModelRecord, ProviderCredentialRecord)
                .join(
                    ProviderCredentialRecord,
                    ConfiguredModelRecord.credential_id == ProviderCredentialRecord.credential_id,
                )
                .where(ConfiguredModelRecord.tenant_id == tenant_id)
                .where(
                    or_(
                        and_(
                            ProviderCredentialRecord.scope == CredentialScope.USER.value,
                            ProviderCredentialRecord.owner_user_id == user_id,
                        ),
                        ProviderCredentialRecord.scope.in_(
                            [CredentialScope.TENANT.value, CredentialScope.SYSTEM.value]
                        ),
                    )
                )
            )
            if configured_model_id:
                statement = statement.where(
                    ConfiguredModelRecord.configured_model_id == configured_model_id
                )
            else:
                if provider_key:
                    statement = statement.where(
                        ConfiguredModelRecord.plugin_id == provider_key[0],
                        ConfiguredModelRecord.provider_id == provider_key[1],
                    )
                if model:
                    statement = statement.where(ConfiguredModelRecord.model == model)
                if model_type:
                    statement = statement.where(ConfiguredModelRecord.model_type == model_type)
            row = session.execute(
                statement.order_by(ConfiguredModelRecord.created_at.desc())
            ).first()
            if not row:
                return None
            return ResolvedModelRecord(model=row[0], credential=row[1])

    def resolve_model_candidates(
        self,
        *,
        tenant_id: str,
        user_id: str | None,
        plugin_id: str,
        provider_id: str,
        model: str,
        model_type: str,
    ) -> list[ResolvedModelRecord]:
        with self._sessions() as session:
            rows = session.execute(
                select(ConfiguredModelRecord, ProviderCredentialRecord)
                .join(
                    ProviderCredentialRecord,
                    ConfiguredModelRecord.credential_id == ProviderCredentialRecord.credential_id,
                )
                .where(
                    ConfiguredModelRecord.tenant_id == tenant_id,
                    ConfiguredModelRecord.plugin_id == plugin_id,
                    ConfiguredModelRecord.provider_id == provider_id,
                    ConfiguredModelRecord.model == model,
                    ConfiguredModelRecord.model_type == model_type,
                    or_(
                        and_(
                            ProviderCredentialRecord.scope == CredentialScope.USER.value,
                            ProviderCredentialRecord.owner_user_id == user_id,
                        ),
                        ProviderCredentialRecord.scope.in_(
                            [CredentialScope.TENANT.value, CredentialScope.SYSTEM.value]
                        ),
                    ),
                )
                .order_by(ConfiguredModelRecord.created_at.desc())
            ).all()
            return [ResolvedModelRecord(model=row[0], credential=row[1]) for row in rows]

    def set_tenant_default_model(
        self,
        *,
        tenant_id: str,
        model_type: ModelType,
        configured_model_id: str | None,
    ) -> None:
        key = {
            "tenant_id": tenant_id,
            "model_type": model_type.value,
        }
        with self._sessions.begin() as session:
            record = session.get(TenantDefaultModelRecord, key)
            if configured_model_id is None:
                if record is not None:
                    session.delete(record)
                return
            if record is None:
                session.add(
                    TenantDefaultModelRecord(
                        **key,
                        configured_model_id=configured_model_id,
                    )
                )
            else:
                record.configured_model_id = configured_model_id

    def list_tenant_default_models(
        self,
        *,
        tenant_id: str,
    ) -> dict[ModelType, str]:
        with self._sessions() as session:
            records = session.scalars(
                select(TenantDefaultModelRecord)
                .join(
                    ConfiguredModelRecord,
                    TenantDefaultModelRecord.configured_model_id
                    == ConfiguredModelRecord.configured_model_id,
                )
                .join(
                    ProviderCredentialRecord,
                    ConfiguredModelRecord.credential_id == ProviderCredentialRecord.credential_id,
                )
                .where(
                    TenantDefaultModelRecord.tenant_id == tenant_id,
                    ConfiguredModelRecord.tenant_id == tenant_id,
                    ConfiguredModelRecord.model_type == TenantDefaultModelRecord.model_type,
                    ConfiguredModelRecord.status == ModelStatus.ACTIVE.value,
                    ProviderCredentialRecord.status == CredentialStatus.VALID.value,
                    ProviderCredentialRecord.scope.in_(
                        [CredentialScope.TENANT.value, CredentialScope.SYSTEM.value]
                    ),
                )
            ).all()
            return {ModelType(record.model_type): record.configured_model_id for record in records}

    def get_tenant_default_model(
        self,
        *,
        tenant_id: str,
        model_type: ModelType,
    ) -> str | None:
        return self.list_tenant_default_models(
            tenant_id=tenant_id,
        ).get(model_type)

    def get_source_version(self, source_key: str) -> int:
        with self._sessions() as session:
            record = session.get(ConfigurationSourceVersionRecord, source_key)
            return record.version if record else 0

    def get_provider_preference(
        self, *, tenant_id: str, plugin_id: str, provider_id: str
    ) -> ProviderType | None:
        with self._sessions() as session:
            record = session.get(
                ProviderPreferenceRecord,
                {"tenant_id": tenant_id, "plugin_id": plugin_id, "provider_id": provider_id},
            )
            return ProviderType(record.preferred_provider_type) if record else None

    def set_provider_preference(
        self,
        *,
        tenant_id: str,
        plugin_id: str,
        provider_id: str,
        preferred_provider_type: ProviderType,
    ) -> None:
        source_key = self.provider_source_key(tenant_id, plugin_id, provider_id)
        with self._sessions.begin() as session:
            record = session.get(
                ProviderPreferenceRecord,
                {"tenant_id": tenant_id, "plugin_id": plugin_id, "provider_id": provider_id},
            )
            if record is None:
                session.add(
                    ProviderPreferenceRecord(
                        tenant_id=tenant_id,
                        plugin_id=plugin_id,
                        provider_id=provider_id,
                        preferred_provider_type=preferred_provider_type.value,
                    )
                )
            else:
                record.preferred_provider_type = preferred_provider_type.value
                record.version += 1
            self._bump_source_version(session, source_key)
            self._add_outbox(
                session,
                source_key=source_key,
                event_type="PROVIDER_PREFERENCE_CHANGED",
                payload={"preferred_provider_type": preferred_provider_type.value},
            )

    def upsert_quota_pool(
        self,
        *,
        quota_id: str,
        tenant_id: str,
        plugin_id: str,
        provider_id: str,
        quota_type: ProviderQuotaType,
        quota_unit: QuotaUnit,
        quota_limit: int,
        quota_used: int | None,
        restrict_models: list[str],
        is_valid: bool,
        create_only: bool = False,
    ) -> bool:
        source_key = self.provider_source_key(tenant_id, plugin_id, provider_id)
        with self._sessions.begin() as session:
            record = session.get(ProviderQuotaRecord, quota_id)
            if record is not None:
                if create_only:
                    return False
                if (
                    record.tenant_id != tenant_id
                    or record.plugin_id != plugin_id
                    or record.provider_id != provider_id
                ):
                    raise ValueError("quota_id is already owned by another provider source")
                record.quota_type = quota_type.value
                record.quota_unit = quota_unit.value
                record.quota_limit = quota_limit
                if quota_used is not None:
                    record.quota_used = quota_used
                record.restrict_models = sorted(restrict_models)
                record.is_valid = is_valid and (
                    quota_limit == -1 or record.quota_used < quota_limit
                )
                record.version += 1
            else:
                initial_used = quota_used or 0
                session.add(
                    ProviderQuotaRecord(
                        quota_id=quota_id,
                        tenant_id=tenant_id,
                        plugin_id=plugin_id,
                        provider_id=provider_id,
                        quota_type=quota_type.value,
                        quota_unit=quota_unit.value,
                        quota_limit=quota_limit,
                        quota_used=initial_used,
                        quota_reserved=0,
                        restrict_models=sorted(restrict_models),
                        is_valid=is_valid and (quota_limit == -1 or initial_used < quota_limit),
                    )
                )
            self._bump_source_version(session, source_key)
            self._add_outbox(
                session,
                source_key=source_key,
                event_type="PROVIDER_QUOTA_CHANGED",
                payload={"quota_id": quota_id, "quota_type": quota_type.value},
            )
            return True

    def list_quota_pools(
        self, *, tenant_id: str, plugin_id: str, provider_id: str
    ) -> list[ProviderQuotaRecord]:
        with self._sessions() as session:
            return list(
                session.scalars(
                    select(ProviderQuotaRecord).where(
                        ProviderQuotaRecord.tenant_id == tenant_id,
                        ProviderQuotaRecord.plugin_id == plugin_id,
                        ProviderQuotaRecord.provider_id == provider_id,
                    )
                )
            )

    def reserve_quota(
        self,
        *,
        invocation_id: str,
        tenant_id: str,
        configured_model_id: str,
        eligible_quota_ids: list[str],
        estimated_tokens: int,
    ) -> QuotaReservationRecord | None:
        try:
            with self._sessions.begin() as session:
                existing = session.get(QuotaReservationRecord, invocation_id)
                if existing:
                    return existing
                for quota_id in eligible_quota_ids:
                    quota = session.get(ProviderQuotaRecord, quota_id)
                    if quota is None or not quota.is_valid:
                        continue
                    reserved_units = (
                        1 if quota.quota_unit == QuotaUnit.TIMES.value else estimated_tokens
                    )
                    reserved_units = max(1, reserved_units)
                    statement = (
                        update(ProviderQuotaRecord)
                        .where(
                            ProviderQuotaRecord.quota_id == quota.quota_id,
                            ProviderQuotaRecord.is_valid.is_(True),
                            or_(
                                ProviderQuotaRecord.quota_limit == -1,
                                ProviderQuotaRecord.quota_used
                                + ProviderQuotaRecord.quota_reserved
                                + reserved_units
                                <= ProviderQuotaRecord.quota_limit,
                            ),
                        )
                        .values(
                            quota_reserved=ProviderQuotaRecord.quota_reserved + reserved_units,
                            version=ProviderQuotaRecord.version + 1,
                        )
                    )
                    if session.execute(statement).rowcount != 1:
                        continue
                    reservation = QuotaReservationRecord(
                        invocation_id=invocation_id,
                        quota_id=quota.quota_id,
                        tenant_id=tenant_id,
                        configured_model_id=configured_model_id,
                        provider_type=ProviderType.SYSTEM.value,
                        quota_type=quota.quota_type,
                        quota_unit=quota.quota_unit,
                        reserved_units=reserved_units,
                        status=QuotaReservationStatus.RESERVED.value,
                    )
                    session.add(reservation)
                    source_key = self.provider_source_key(
                        quota.tenant_id, quota.plugin_id, quota.provider_id
                    )
                    self._bump_source_version(session, source_key)
                    return reservation
                return None
        except IntegrityError:
            with self._sessions() as session:
                return session.get(QuotaReservationRecord, invocation_id)

    def reserve_custom(
        self,
        *,
        invocation_id: str,
        tenant_id: str,
        configured_model_id: str,
    ) -> QuotaReservationRecord:
        try:
            with self._sessions.begin() as session:
                existing = session.get(QuotaReservationRecord, invocation_id)
                if existing:
                    return existing
                record = QuotaReservationRecord(
                    invocation_id=invocation_id,
                    tenant_id=tenant_id,
                    configured_model_id=configured_model_id,
                    provider_type=ProviderType.CUSTOM.value,
                    reserved_units=0,
                    status=QuotaReservationStatus.RESERVED.value,
                )
                session.add(record)
                return record
        except IntegrityError:
            with self._sessions() as session:
                record = session.get(QuotaReservationRecord, invocation_id)
                if record is None:
                    raise
                return record

    def settle_quota(
        self,
        *,
        invocation_id: str,
        succeeded: bool,
        actual_tokens: int | None,
    ) -> QuotaReservationRecord | None:
        with self._sessions.begin() as session:
            reservation = session.scalar(
                select(QuotaReservationRecord)
                .where(QuotaReservationRecord.invocation_id == invocation_id)
                .with_for_update()
            )
            if reservation is None or reservation.status != QuotaReservationStatus.RESERVED.value:
                return reservation
            reservation.settled_at = datetime.now(UTC)
            if reservation.quota_id is None:
                reservation.actual_units = 0
                reservation.status = (
                    QuotaReservationStatus.SETTLED.value
                    if succeeded
                    else QuotaReservationStatus.RELEASED.value
                )
                return reservation
            quota = session.scalar(
                select(ProviderQuotaRecord)
                .where(ProviderQuotaRecord.quota_id == reservation.quota_id)
                .with_for_update()
            )
            if quota is None:
                raise RuntimeError("reserved quota pool no longer exists")
            if succeeded:
                actual_units = (
                    1
                    if reservation.quota_unit == QuotaUnit.TIMES.value
                    else max(1, actual_tokens or reservation.reserved_units)
                )
            else:
                actual_units = 0
            quota.quota_reserved = max(0, quota.quota_reserved - reservation.reserved_units)
            quota.quota_used += actual_units
            if quota.quota_limit != -1 and quota.quota_used >= quota.quota_limit:
                quota.is_valid = False
            quota.version += 1
            reservation.actual_units = actual_units
            reservation.status = (
                QuotaReservationStatus.SETTLED.value
                if succeeded
                else QuotaReservationStatus.RELEASED.value
            )
            source_key = self.provider_source_key(
                quota.tenant_id, quota.plugin_id, quota.provider_id
            )
            self._bump_source_version(session, source_key)
            self._add_outbox(
                session,
                source_key=source_key,
                event_type="PROVIDER_QUOTA_USAGE_CHANGED",
                payload={
                    "quota_id": quota.quota_id,
                    "invocation_id": invocation_id,
                    "actual_units": actual_units,
                    "is_valid": quota.is_valid,
                },
            )
            return reservation

    def bind_invocation(
        self,
        *,
        invocation_id: str,
        tenant_id: str,
        session_id: str | None,
        query_id: str | None,
    ) -> bool:
        with self._sessions() as session:
            existing = session.get(InvocationBindingRecord, invocation_id)
            if existing:
                return (
                    existing.tenant_id == tenant_id
                    and existing.session_id == session_id
                    and existing.query_id == query_id
                )
            session.add(
                InvocationBindingRecord(
                    invocation_id=invocation_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    query_id=query_id,
                )
            )
            try:
                session.commit()
                return True
            except IntegrityError:
                session.rollback()
                existing = session.get(InvocationBindingRecord, invocation_id)
                return bool(
                    existing
                    and existing.tenant_id == tenant_id
                    and existing.session_id == session_id
                    and existing.query_id == query_id
                )

    def record_usage(
        self,
        *,
        invocation_id: str,
        tenant_id: str,
        user_id: str | None,
        session_id: str | None,
        query_id: str | None,
        app_id: str | None,
        configured_model_id: str,
        operation: str,
        usage: Usage | None,
        latency_ms: int | None,
        status: str,
        trace_id: str | None,
        error_code: str | None = None,
    ) -> bool:
        with self._sessions() as session:
            if session.get(ModelInvocationUsageRecord, invocation_id):
                return False
            session.add(
                ModelInvocationUsageRecord(
                    invocation_id=invocation_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                    query_id=query_id,
                    app_id=app_id,
                    configured_model_id=configured_model_id,
                    operation=operation,
                    usage=usage.model_dump(mode="json") if usage else None,
                    latency_ms=latency_ms,
                    status=status,
                    trace_id=trace_id,
                    error_code=error_code,
                )
            )
            try:
                session.commit()
                return True
            except IntegrityError:
                session.rollback()
                return False

    @staticmethod
    def encode_page_token(offset: int) -> str:
        return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii")

    @staticmethod
    def decode_page_token(token: str | None) -> int:
        if not token:
            return 0
        try:
            value = int(base64.urlsafe_b64decode(token.encode("ascii")).decode("ascii"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("invalid page_token") from exc
        if value < 0:
            raise ValueError("invalid page_token")
        return value
