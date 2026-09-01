from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from opentelemetry import trace

from .adapters.registry import ProviderRegistry
from .contracts.entities import (
    CallerIdentity,
    CredentialSet,
    ModelDescriptor,
    ProviderRef,
    RuntimeContext,
)
from .contracts.enums import (
    CredentialScope,
    CredentialSourceType,
    CredentialStatus,
    ErrorCode,
    ModelStatus,
    ModelType,
    ProviderType,
)
from .contracts.invocation import (
    ConfiguredModelAvailabilityUpdateRequest,
    ExistingCredentialModelRegistrationRequest,
    ManualModelRegistration,
    ModelListQuery,
    ModelRegistrationRequest,
    ProviderAvailabilityUpdateRequest,
    TenantDefaultModelUpdateRequest,
)
from .contracts.responses import (
    ConfiguredModelAvailabilityResult,
    ConfiguredModelItem,
    ConfiguredModelRegistrationResult,
    CredentialSummary,
    ModelListResult,
    ProviderAvailabilityResult,
    ProviderSummary,
    RegistrationResult,
    TenantDefaultModelsResult,
)
from .errors import ModelAccessException
from .observability import OpenTelemetryFacade
from .persistence.repository import ModelAccessRepository
from .protocols import CredentialCipher, QuotaManager, UserQuotaAwareManager
from .security import URLSecurityPolicy, mask_secret


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class ModelControlPlaneService:
    def __init__(
        self,
        *,
        repository: ModelAccessRepository,
        providers: ProviderRegistry,
        cipher: CredentialCipher,
        url_policy: URLSecurityPolicy,
        observability: OpenTelemetryFacade,
        quota: QuotaManager,
    ):
        self._repository = repository
        self._providers = providers
        self._cipher = cipher
        self._url_policy = url_policy
        self._observability = observability
        self._quota = quota

    async def register_model(
        self,
        request: ModelRegistrationRequest,
        *,
        identity: CallerIdentity,
        idempotency_key: str,
    ) -> RegistrationResult:
        self._authorize_registration(request, identity)
        if not idempotency_key or len(idempotency_key) > 256:
            raise ModelAccessException(
                ErrorCode.REQUEST_INVALID,
                "Idempotency-Key is required and must not exceed 256 characters",
                field="Idempotency-Key",
            )
        request_hash = self._request_hash(request)
        replay = self._repository.get_idempotency(
            request.tenant_id,
            request.user_id,
            idempotency_key,
        )
        if replay:
            if replay.request_hash != request_hash:
                raise ModelAccessException(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "Idempotency-Key is already bound to a different request",
                )
            return replay.result

        request_id = new_id("req")
        trace_id = self._observability.current_trace_id()
        with self._observability.start_span(
            "model.registration",
            {
                "model_access.tenant.id": request.tenant_id,
                "gen_ai.provider.name": request.provider.provider_id,
                "model_access.scope": request.credential.scope.value,
            },
        ) as registration_span:
            span_context = registration_span.span.get_span_context()
            if span_context.is_valid:
                trace_id = trace.format_trace_id(span_context.trace_id)
            adapter = self._providers.get(request.provider)
            base_url = self._url_policy.validate(request.credential.base_url)
            if request.deployment:
                for endpoint in request.deployment.endpoints:
                    self._url_policy.validate(endpoint.base_url)

            credential_values = {
                "base_url": base_url,
                "api_key": request.credential.api_key.get_secret_value(),
            }
            credentials = CredentialSet(
                provider=request.provider,
                values=credential_values,
                source=(
                    CredentialSourceType.SELF_HOSTED
                    if request.deployment
                    else (
                        CredentialSourceType.SYSTEM
                        if request.credential.scope == CredentialScope.SYSTEM
                        else (
                            CredentialSourceType.USER
                            if request.credential.scope == CredentialScope.USER
                            else CredentialSourceType.TENANT
                        )
                    )
                ),
            )
            if request.options.validate_credentials:
                validation = await adapter.validate_credentials(
                    context=RuntimeContext(
                        tenant_id=request.tenant_id,
                        user_id=request.user_id,
                        request_id=request_id,
                    ),
                    provider=request.provider,
                    credentials=credentials,
                )
                if not validation.valid:
                    raise ModelAccessException(
                        ErrorCode.CREDENTIAL_INVALID,
                        validation.message or "provider credential validation failed",
                        provider=request.provider.key,
                        provider_error_code=validation.error_code,
                    )
                if validation.normalized_credentials:
                    credential_values.update(validation.normalized_credentials)
                    credential_values["base_url"] = base_url

            descriptors = await self._collect_models(request, credentials, adapter)
            enabled = request.options.enable_discovered_models
            if not enabled:
                descriptors = [
                    item.model_copy(update={"status": ModelStatus.DISABLED}) for item in descriptors
                ]
            descriptors = self._deduplicate(descriptors)

            credential_id = new_id("cred")
            registration_id = new_id("reg")
            configured = [(new_id("cm"), descriptor) for descriptor in descriptors]
            result = RegistrationResult(
                registration_id=registration_id,
                credential_id=credential_id,
                credential_name=request.credential.name,
                provider_id=request.provider.provider_id,
                base_url=base_url,
                api_key_masked=mask_secret(credential_values.get("api_key", "")),
                scope=request.credential.scope,
                validation_status="VALID",
                discovered_model_count=len(descriptors),
                configured_model_ids=[item[0] for item in configured],
                created_at=datetime.now(UTC),
            )
            try:
                self._repository.save_registration(
                    credential_id=credential_id,
                    registration_id=registration_id,
                    tenant_id=request.tenant_id,
                    owner_user_id=request.user_id,
                    provider=adapter.descriptor,
                    credential_name=request.credential.name,
                    base_url=base_url,
                    encrypted_values=self._cipher.encrypt(credential_values),
                    api_key_masked=result.api_key_masked,
                    scope=request.credential.scope,
                    deployment=request.deployment.model_dump(mode="json")
                    if request.deployment
                    else None,
                    models=configured,
                    source="SELF_HOSTED" if request.deployment else "PROVIDER",
                    request_hash=request_hash,
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    trace_id=trace_id,
                    result=result,
                )
            except Exception:
                replay = self._repository.get_idempotency(
                    request.tenant_id,
                    request.user_id,
                    idempotency_key,
                )
                if replay and replay.request_hash == request_hash:
                    return replay.result
                raise
            return result

    async def register_model_with_credential(
        self,
        request: ExistingCredentialModelRegistrationRequest,
        *,
        identity: CallerIdentity,
    ) -> ConfiguredModelRegistrationResult:
        if identity.tenant_id != request.tenant_id:
            raise ModelAccessException(ErrorCode.PERMISSION_DENIED, "tenant identity mismatch")
        if not identity.is_service and identity.user_id != request.user_id:
            raise ModelAccessException(ErrorCode.PERMISSION_DENIED, "user identity mismatch")
        credential = self._repository.get_provider_credential(request.credential_id)
        if credential is None or credential.tenant_id != request.tenant_id:
            raise ModelAccessException(
                ErrorCode.CREDENTIAL_REQUIRED,
                "provider credential was not found",
                field="credential_id",
            )
        scope = CredentialScope(credential.scope)
        if scope == CredentialScope.TENANT and not identity.is_admin:
            raise ModelAccessException(
                ErrorCode.PERMISSION_DENIED,
                "tenant-scoped credentials require model administrator role",
            )
        if scope == CredentialScope.SYSTEM and "system_admin" not in identity.roles:
            raise ModelAccessException(
                ErrorCode.PERMISSION_DENIED,
                "system-hosted credentials require system administrator role",
            )
        if scope == CredentialScope.USER and credential.owner_user_id != identity.user_id:
            raise ModelAccessException(
                ErrorCode.PERMISSION_DENIED,
                "user-scoped credential belongs to another user",
            )
        provider_ref = ProviderRef(
            plugin_id=credential.plugin_id,
            provider_id=credential.provider_id,
        )
        adapter = self._providers.get(provider_ref)
        descriptor = self._manual_descriptor(provider_ref, request.model)
        configured_model_id = self._repository.add_model_to_credential(
            configured_model_id=new_id("cm"),
            credential=credential,
            provider=adapter.descriptor,
            descriptor=descriptor,
            operator_user_id=request.user_id,
        )
        return ConfiguredModelRegistrationResult(
            configured_model_id=configured_model_id,
            credential_id=credential.credential_id,
            provider_id=credential.provider_id,
            model=descriptor.model,
            model_type=descriptor.model_type,
        )

    async def list_models(
        self,
        query: ModelListQuery,
        *,
        identity: CallerIdentity,
    ) -> ModelListResult:
        self._authorize_list(query, identity)
        try:
            offset = self._repository.decode_page_token(query.page_token)
        except ValueError as exc:
            raise ModelAccessException(
                ErrorCode.REQUEST_INVALID, str(exc), field="page_token"
            ) from exc
        with self._observability.start_span(
            "model.catalog.list",
            {
                "model_access.tenant.id": query.tenant_id,
                "model_access.catalog.page_size": query.page_size,
            },
        ):
            records, next_offset = self._repository.list_models(
                query=query,
                identity=identity,
                offset=offset,
                paginate=False,
            )
            del next_offset
            default_models = self._default_model_map(query.tenant_id)
            user_quota = None
            if isinstance(self._quota, UserQuotaAwareManager):
                user_quota = self._quota.get_summary(
                    tenant_id=query.tenant_id,
                    user_id=query.user_id,
                    roles=identity.roles if identity.user_id == query.user_id else set(),
                    identity=identity,
                )
            items: list[ConfiguredModelItem] = []
            for record in records:
                provider_type = (
                    ProviderType.SYSTEM
                    if record.credential.scope == CredentialScope.SYSTEM.value
                    else ProviderType.CUSTOM
                )
                model_enabled = record.model.status == ModelStatus.ACTIVE.value
                status = (
                    ModelStatus(record.model.status)
                    if record.provider_enabled
                    else ModelStatus.DISABLED
                )
                if (
                    status == ModelStatus.ACTIVE
                    and user_quota
                    and user_quota.status.value in {"EXCEEDED", "DISABLED"}
                ):
                    status = ModelStatus.QUOTA_EXCEEDED
                items.append(
                    ConfiguredModelItem(
                        configured_model_id=record.model.configured_model_id,
                        provider=ProviderSummary(
                            plugin_id=record.model.plugin_id,
                            provider_id=record.model.provider_id,
                            display_name=record.model.provider_display_name,
                        ),
                        model=record.model.model,
                        label=record.model.label,
                        model_type=record.model.model_type,
                        categories=set(record.model.categories),
                        input_modalities=set(record.model.input_modalities),
                        output_modalities=set(record.model.output_modalities),
                        features=set(record.model.features),
                        operations=set(record.model.operations),
                        context_window=record.model.context_window,
                        max_output_tokens=record.model.max_output_tokens,
                        credential=CredentialSummary(
                            credential_id=record.credential.credential_id,
                            name=record.credential.name,
                            scope=record.credential.scope,
                            api_key_masked=record.credential.api_key_masked,
                        ),
                        status=status,
                        model_enabled=model_enabled,
                        provider_enabled=record.provider_enabled,
                        provider_type=provider_type,
                        user_quota_status=user_quota.status if user_quota else None,
                        user_quota_remaining=(user_quota.credits_remaining if user_quota else None),
                        is_default=(
                            default_models.get(ModelType(record.model.model_type))
                            == record.model.configured_model_id
                        ),
                    )
                )
            if query.status:
                items = [item for item in items if item.status == query.status]
            page = items[offset : offset + query.page_size + 1]
            next_offset = offset + query.page_size if len(page) > query.page_size else None
            return ModelListResult(
                items=page[: query.page_size],
                next_page_token=(
                    self._repository.encode_page_token(next_offset)
                    if next_offset is not None
                    else None
                ),
                default_models=default_models,
                user_quota=user_quota,
            )

    async def set_model_availability(
        self,
        request: ConfiguredModelAvailabilityUpdateRequest,
        *,
        identity: CallerIdentity,
    ) -> ConfiguredModelAvailabilityResult:
        self._authorize_availability_update(request.tenant_id, identity)
        model = self._repository.set_model_enabled(
            tenant_id=request.tenant_id,
            configured_model_id=request.configured_model_id,
            enabled=request.enabled,
            operator_user_id=identity.user_id,
        )
        if model is None:
            raise ModelAccessException(
                ErrorCode.MODEL_NOT_FOUND,
                "configured model was not found",
                field="configured_model_id",
            )
        provider_enabled = self._repository.is_provider_enabled(
            tenant_id=request.tenant_id,
            plugin_id=model.plugin_id,
            provider_id=model.provider_id,
        )
        return ConfiguredModelAvailabilityResult(
            tenant_id=request.tenant_id,
            configured_model_id=model.configured_model_id,
            enabled=model.status == ModelStatus.ACTIVE.value,
            provider_enabled=provider_enabled,
            status=(
                ModelStatus(model.status) if provider_enabled else ModelStatus.DISABLED
            ),
        )

    async def set_provider_availability(
        self,
        request: ProviderAvailabilityUpdateRequest,
        *,
        identity: CallerIdentity,
    ) -> ProviderAvailabilityResult:
        self._authorize_availability_update(request.tenant_id, identity)
        self._providers.get(request.provider)
        enabled = self._repository.set_provider_enabled(
            tenant_id=request.tenant_id,
            plugin_id=request.provider.plugin_id,
            provider_id=request.provider.provider_id,
            enabled=request.enabled,
            operator_user_id=identity.user_id,
        )
        return ProviderAvailabilityResult(
            tenant_id=request.tenant_id,
            provider=request.provider,
            enabled=enabled,
        )

    async def get_provider_availability(
        self,
        *,
        tenant_id: str,
        provider: ProviderRef,
        identity: CallerIdentity,
    ) -> ProviderAvailabilityResult:
        self._authorize_tenant_access(tenant_id, identity)
        self._providers.get(provider)
        return ProviderAvailabilityResult(
            tenant_id=tenant_id,
            provider=provider,
            enabled=self._repository.is_provider_enabled(
                tenant_id=tenant_id,
                plugin_id=provider.plugin_id,
                provider_id=provider.provider_id,
            ),
        )

    async def set_default_model(
        self,
        request: TenantDefaultModelUpdateRequest,
        *,
        model_type: ModelType,
        identity: CallerIdentity,
    ) -> TenantDefaultModelsResult:
        self._authorize_default_model_update(request.tenant_id, identity)
        if request.configured_model_id:
            resolved = self._repository.resolve_model(
                configured_model_id=request.configured_model_id,
                tenant_id=request.tenant_id,
                user_id=None,
            )
            if resolved is None or resolved.credential.scope not in {
                CredentialScope.TENANT.value,
                CredentialScope.SYSTEM.value,
            }:
                raise ModelAccessException(
                    ErrorCode.MODEL_NOT_FOUND,
                    "tenant default must be selected from TENANT or SYSTEM configured models",
                    field="configured_model_id",
                )
            if resolved.model.model_type != model_type.value:
                raise ModelAccessException(
                    ErrorCode.MODEL_TYPE_MISMATCH,
                    "configured model type does not match the default model type",
                    field="configured_model_id",
                )
            if not resolved.provider_enabled or resolved.model.status != ModelStatus.ACTIVE.value:
                raise ModelAccessException(
                    ErrorCode.MODEL_DISABLED,
                    "disabled provider or model cannot be selected as the default",
                    field="configured_model_id",
                )
            if resolved.credential.status != CredentialStatus.VALID.value:
                raise ModelAccessException(
                    ErrorCode.CREDENTIAL_INVALID,
                    "model credential is not valid",
                    field="configured_model_id",
                )
        self._repository.set_tenant_default_model(
            tenant_id=request.tenant_id,
            model_type=model_type,
            configured_model_id=request.configured_model_id,
        )
        return self._default_models_result(request.tenant_id)

    async def get_default_models(
        self,
        *,
        tenant_id: str,
        identity: CallerIdentity,
    ) -> TenantDefaultModelsResult:
        self._authorize_tenant_access(tenant_id, identity)
        return self._default_models_result(tenant_id)

    def _default_model_map(
        self,
        tenant_id: str,
    ) -> dict[ModelType, str | None]:
        stored = self._repository.list_tenant_default_models(
            tenant_id=tenant_id,
        )
        return {model_type: stored.get(model_type) for model_type in ModelType}

    def _default_models_result(
        self,
        tenant_id: str,
    ) -> TenantDefaultModelsResult:
        return TenantDefaultModelsResult(
            tenant_id=tenant_id,
            defaults=self._default_model_map(tenant_id),
        )

    async def _collect_models(self, request, credentials, adapter) -> list[ModelDescriptor]:  # type: ignore[no-untyped-def]
        descriptors: list[ModelDescriptor] = []
        if request.options.discover_models:
            discovered = await adapter.discover_models(
                context=RuntimeContext(
                    tenant_id=request.tenant_id,
                    user_id=request.user_id,
                    request_id=new_id("req"),
                ),
                provider=request.provider,
                credentials=credentials,
                deployment=request.deployment.model_dump(mode="json")
                if request.deployment
                else None,
            )
            descriptors.extend(discovered)
        elif adapter.descriptor.models:
            descriptors.extend(adapter.descriptor.models)
        if request.model:
            descriptors.append(self._manual_descriptor(request.provider, request.model))
        if not descriptors:
            raise ModelAccessException(
                ErrorCode.MODEL_NOT_FOUND,
                "provider did not return models and no manual model was supplied",
                provider=request.provider.key,
            )
        return descriptors

    @staticmethod
    def _manual_descriptor(
        provider: ProviderRef, model: ManualModelRegistration
    ) -> ModelDescriptor:
        return ModelDescriptor(
            provider=provider,
            model=model.model,
            model_type=model.model_type,
            label=model.label,
            input_modalities=model.input_modalities,
            output_modalities=model.output_modalities,
            operations=model.operations,
            features=model.features,
            categories=model.categories,
            context_window=model.context_window,
            max_output_tokens=model.max_output_tokens,
        )

    @staticmethod
    def _deduplicate(descriptors: list[ModelDescriptor]) -> list[ModelDescriptor]:
        result: dict[tuple[str, str], ModelDescriptor] = {}
        for item in descriptors:
            result[(item.model, item.model_type.value)] = item
        return list(result.values())

    @staticmethod
    def _request_hash(request: ModelRegistrationRequest) -> str:
        data = request.model_dump(mode="json")
        data["credential"]["api_key"] = hashlib.sha256(
            request.credential.api_key.get_secret_value().encode("utf-8")
        ).hexdigest()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _authorize_registration(
        request: ModelRegistrationRequest, identity: CallerIdentity
    ) -> None:
        if identity.tenant_id != request.tenant_id:
            raise ModelAccessException(ErrorCode.PERMISSION_DENIED, "tenant identity mismatch")
        if not identity.is_service and identity.user_id != request.user_id:
            raise ModelAccessException(ErrorCode.PERMISSION_DENIED, "user identity mismatch")
        if request.credential.scope == CredentialScope.TENANT and not identity.is_admin:
            raise ModelAccessException(
                ErrorCode.PERMISSION_DENIED,
                "tenant-scoped credentials require model administrator role",
            )
        if (
            request.credential.scope == CredentialScope.SYSTEM
            and "system_admin" not in identity.roles
        ):
            raise ModelAccessException(
                ErrorCode.PERMISSION_DENIED,
                "system-hosted credentials require system administrator role",
            )

    @staticmethod
    def _authorize_list(query: ModelListQuery, identity: CallerIdentity) -> None:
        if identity.tenant_id != query.tenant_id:
            raise ModelAccessException(ErrorCode.PERMISSION_DENIED, "tenant identity mismatch")
        if not (identity.is_admin or identity.is_service) and identity.user_id != query.user_id:
            raise ModelAccessException(
                ErrorCode.PERMISSION_DENIED, "cannot query another user's models"
            )

    @staticmethod
    def _authorize_tenant_access(
        tenant_id: str,
        identity: CallerIdentity,
    ) -> None:
        if identity.tenant_id != tenant_id:
            raise ModelAccessException(ErrorCode.PERMISSION_DENIED, "tenant identity mismatch")

    @staticmethod
    def _authorize_default_model_update(
        tenant_id: str,
        identity: CallerIdentity,
    ) -> None:
        ModelControlPlaneService._authorize_tenant_access(tenant_id, identity)
        if "tenant_admin" not in identity.roles:
            raise ModelAccessException(
                ErrorCode.PERMISSION_DENIED,
                "tenant administrator role is required to manage default models",
            )

    @staticmethod
    def _authorize_availability_update(
        tenant_id: str,
        identity: CallerIdentity,
    ) -> None:
        ModelControlPlaneService._authorize_tenant_access(tenant_id, identity)
        if not identity.is_admin:
            raise ModelAccessException(
                ErrorCode.PERMISSION_DENIED,
                "model administrator role is required to manage availability",
            )
