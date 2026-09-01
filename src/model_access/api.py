from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from .client import ModelRepositoryClient
from .contracts.common import PROTOCOL_VERSION, ensure_protocol_version
from .contracts.entities import CallerIdentity, ProviderRef
from .contracts.enums import ErrorCode, ModelCategory, ModelStatus, ModelType
from .contracts.invocation import (
    ConfiguredModelAvailabilityUpdateRequest,
    ExistingCredentialModelRegistrationRequest,
    ModelInvocationRequest,
    ModelListQuery,
    ModelRegistrationRequest,
    ProviderAvailabilityUpdateRequest,
    TenantDefaultModelUpdateRequest,
)
from .contracts.quota import (
    ModelCreditRateInput,
    RoleQuotaBindingInput,
    UserQuotaAssignmentInput,
    UserQuotaTemplateInput,
)
from .contracts.responses import AsyncInvocationResult, ResponseEnvelope, StreamEvent
from .errors import HTTP_STATUS_BY_ERROR, ModelAccessException

IdentityResolver = Callable[[Request], Awaitable[CallerIdentity]]


class HeaderIdentityResolver:
    """Minimal service-to-service resolver.

    It checks that a bearer token exists but deliberately does not decode it.
    Production deployments should replace this with the platform JWT/service
    identity resolver and treat these headers only as consistency checks.
    """

    async def __call__(self, request: Request) -> CallerIdentity:
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            raise ModelAccessException(
                code=ErrorCode.UNAUTHORIZED,
                message="Bearer authorization is required",
            )
        tenant_id = request.headers.get("x-tenant-id")
        if not tenant_id:
            raise ModelAccessException(
                code=ErrorCode.CONTEXT_INVALID,
                message="X-Tenant-ID is required",
            )
        roles = {
            item.strip() for item in request.headers.get("x-roles", "").split(",") if item.strip()
        }
        return CallerIdentity(
            tenant_id=tenant_id,
            user_id=request.headers.get("x-user-id"),
            roles=roles,
            is_service=request.headers.get("x-service-identity", "false").lower() == "true",
        )


def create_router(
    client: ModelRepositoryClient,
    *,
    identity_resolver: IdentityResolver | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["model-access"])
    resolve_identity = identity_resolver or HeaderIdentityResolver()

    @router.post("/model-registrations", status_code=201)
    async def register_model(
        body: ModelRegistrationRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        identity: CallerIdentity = Depends(resolve_identity),
        protocol_header: Annotated[str | None, Header(alias="X-Model-Protocol-Version")] = None,
    ) -> JSONResponse:
        try:
            _validate_protocol_header(protocol_header, None)
            result = await client.register_model(
                body,
                identity=identity,
                idempotency_key=idempotency_key,
            )
            envelope = ResponseEnvelope(
                request_id=request.headers.get("x-request-id") or result.registration_id,
                trace_id=client.observability.current_trace_id(),
                data=result,
            )
            return JSONResponse(status_code=201, content=jsonable_encoder(envelope))
        except ModelAccessException as exc:
            return _error_response(exc, request.headers.get("x-request-id"))

    @router.post("/credential-model-registrations", status_code=201)
    async def register_model_with_credential(
        body: ExistingCredentialModelRegistrationRequest,
        request: Request,
        identity: CallerIdentity = Depends(resolve_identity),
        protocol_header: Annotated[
            str | None, Header(alias="X-Model-Protocol-Version")
        ] = None,
    ) -> JSONResponse:
        try:
            _validate_protocol_header(protocol_header, None)
            result = await client.register_model_with_credential(body, identity=identity)
            return _data_response(result, request, "req_credential_model_registration", 201)
        except ModelAccessException as exc:
            return _error_response(exc, request.headers.get("x-request-id"))

    @router.get("/models")
    async def list_models(
        request: Request,
        tenant_id: str,
        user_id: str,
        identity: CallerIdentity = Depends(resolve_identity),
        category: ModelCategory | None = None,
        model_type: ModelType | None = None,
        provider_id: str | None = None,
        status: ModelStatus | None = ModelStatus.ACTIVE,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        page_token: str | None = None,
        protocol_header: Annotated[str | None, Header(alias="X-Model-Protocol-Version")] = None,
    ) -> JSONResponse:
        try:
            _validate_protocol_header(protocol_header, None)
            result = await client.list_models(
                ModelListQuery(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    category=category,
                    model_type=model_type,
                    provider_id=provider_id,
                    status=status,
                    page_size=page_size,
                    page_token=page_token,
                ),
                identity=identity,
            )
            envelope = ResponseEnvelope(
                request_id=request.headers.get("x-request-id") or "req_catalog",
                trace_id=client.observability.current_trace_id(),
                data=result,
            )
            return JSONResponse(content=jsonable_encoder(envelope))
        except ModelAccessException as exc:
            return _error_response(exc, request.headers.get("x-request-id"))

    @router.put("/model-availability")
    async def set_model_availability(
        body: ConfiguredModelAvailabilityUpdateRequest,
        request: Request,
        identity: CallerIdentity = Depends(resolve_identity),
    ) -> JSONResponse:
        try:
            result = await client.set_model_availability(body, identity=identity)
            return _data_response(result, request, "req_model_availability")
        except ModelAccessException as exc:
            return _error_response(exc, request.headers.get("x-request-id"))

    @router.put("/provider-availability")
    async def set_provider_availability(
        body: ProviderAvailabilityUpdateRequest,
        request: Request,
        identity: CallerIdentity = Depends(resolve_identity),
    ) -> JSONResponse:
        try:
            result = await client.set_provider_availability(body, identity=identity)
            return _data_response(result, request, "req_provider_availability")
        except ModelAccessException as exc:
            return _error_response(exc, request.headers.get("x-request-id"))

    @router.get("/provider-availability")
    async def get_provider_availability(
        request: Request,
        tenant_id: str,
        plugin_id: str,
        provider_id: str,
        identity: CallerIdentity = Depends(resolve_identity),
    ) -> JSONResponse:
        try:
            result = await client.get_provider_availability(
                tenant_id=tenant_id,
                provider=ProviderRef(plugin_id=plugin_id, provider_id=provider_id),
                identity=identity,
            )
            return _data_response(result, request, "req_provider_availability")
        except ModelAccessException as exc:
            return _error_response(exc, request.headers.get("x-request-id"))

    @router.post("/model-invocations")
    async def invoke_model(
        body: ModelInvocationRequest,
        request: Request,
        identity: CallerIdentity = Depends(resolve_identity),
        protocol_header: Annotated[str | None, Header(alias="X-Model-Protocol-Version")] = None,
    ) -> Any:
        try:
            _validate_protocol_header(protocol_header, body.protocol_version)
            result = await client.invoke(body, identity=identity)
            if hasattr(result, "__aiter__"):
                return StreamingResponse(
                    _sse(result),  # type: ignore[arg-type]
                    media_type="text/event-stream",
                    headers={"X-Model-Protocol-Version": PROTOCOL_VERSION},
                )
            status_code = 202 if isinstance(result, AsyncInvocationResult) else 200
            envelope = ResponseEnvelope(
                request_id=body.context.request_id
                or request.headers.get("x-request-id")
                or "req_invocation",
                trace_id=client.observability.current_trace_id(),
                data=result,
            )
            return JSONResponse(status_code=status_code, content=jsonable_encoder(envelope))
        except ModelAccessException as exc:
            return _error_response(
                exc,
                body.context.request_id or request.headers.get("x-request-id"),
            )

    @router.get("/model-defaults")
    async def get_default_models(
        request: Request,
        tenant_id: str,
        identity: CallerIdentity = Depends(resolve_identity),
        protocol_header: Annotated[str | None, Header(alias="X-Model-Protocol-Version")] = None,
    ) -> JSONResponse:
        try:
            _validate_protocol_header(protocol_header, None)
            result = await client.get_default_models(
                tenant_id=tenant_id,
                identity=identity,
            )
            envelope = ResponseEnvelope(
                request_id=request.headers.get("x-request-id") or "req_model_defaults",
                trace_id=client.observability.current_trace_id(),
                data=result,
            )
            return JSONResponse(content=jsonable_encoder(envelope))
        except ModelAccessException as exc:
            return _error_response(exc, request.headers.get("x-request-id"))

    @router.put("/model-defaults/{model_type}")
    async def set_default_model(
        model_type: ModelType,
        body: TenantDefaultModelUpdateRequest,
        request: Request,
        identity: CallerIdentity = Depends(resolve_identity),
        protocol_header: Annotated[str | None, Header(alias="X-Model-Protocol-Version")] = None,
    ) -> JSONResponse:
        try:
            _validate_protocol_header(protocol_header, None)
            result = await client.set_default_model(
                body,
                model_type=model_type,
                identity=identity,
            )
            envelope = ResponseEnvelope(
                request_id=request.headers.get("x-request-id") or "req_model_default_update",
                trace_id=client.observability.current_trace_id(),
                data=result,
            )
            return JSONResponse(content=jsonable_encoder(envelope))
        except ModelAccessException as exc:
            return _error_response(exc, request.headers.get("x-request-id"))

    @router.put("/user-quotas/model-rates")
    async def configure_model_credit_rate(
        body: ModelCreditRateInput,
        request: Request,
        identity: CallerIdentity = Depends(resolve_identity),
    ) -> JSONResponse:
        try:
            result = client.configure_model_credit_rate(body, identity=identity)
            return _data_response(result, request, "req_model_rate")
        except ModelAccessException as exc:
            return _error_response(exc, request.headers.get("x-request-id"))

    @router.put("/user-quotas/templates")
    async def configure_user_quota_template(
        body: UserQuotaTemplateInput,
        request: Request,
        identity: CallerIdentity = Depends(resolve_identity),
    ) -> JSONResponse:
        try:
            result = client.configure_user_quota_template(body, identity=identity)
            return _data_response(result, request, "req_quota_template")
        except ModelAccessException as exc:
            return _error_response(exc, request.headers.get("x-request-id"))

    @router.put("/user-quotas/role-bindings", status_code=204)
    async def bind_quota_template_to_role(
        body: RoleQuotaBindingInput,
        request: Request,
        identity: CallerIdentity = Depends(resolve_identity),
    ) -> JSONResponse:
        try:
            client.bind_quota_template_to_role(body, identity=identity)
            return JSONResponse(status_code=204, content=None)
        except ModelAccessException as exc:
            return _error_response(exc, request.headers.get("x-request-id"))

    @router.put("/user-quotas/users", status_code=204)
    async def assign_user_quota(
        body: UserQuotaAssignmentInput,
        request: Request,
        identity: CallerIdentity = Depends(resolve_identity),
    ) -> JSONResponse:
        try:
            client.assign_user_quota(body, identity=identity)
            return JSONResponse(status_code=204, content=None)
        except ModelAccessException as exc:
            return _error_response(exc, request.headers.get("x-request-id"))

    @router.get("/user-quotas/users/{user_id}")
    async def get_user_quota(
        user_id: str,
        tenant_id: str,
        request: Request,
        roles: str | None = None,
        identity: CallerIdentity = Depends(resolve_identity),
    ) -> JSONResponse:
        try:
            result = client.get_user_quota(
                tenant_id=tenant_id,
                user_id=user_id,
                roles={item.strip() for item in (roles or "").split(",") if item.strip()},
                identity=identity,
            )
            return _data_response(result, request, "req_user_quota")
        except ModelAccessException as exc:
            return _error_response(exc, request.headers.get("x-request-id"))

    @router.get("/user-costs")
    async def query_user_costs(
        tenant_id: str,
        request: Request,
        user_id: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        identity: CallerIdentity = Depends(resolve_identity),
    ) -> JSONResponse:
        try:
            result = client.query_user_costs(
                tenant_id=tenant_id,
                user_id=user_id,
                start_at=start_at,
                end_at=end_at,
                limit=limit,
                identity=identity,
            )
            return _data_response(result, request, "req_user_costs")
        except ModelAccessException as exc:
            return _error_response(exc, request.headers.get("x-request-id"))

    @router.get("/user-cost-summary")
    async def summarize_user_costs(
        tenant_id: str,
        request: Request,
        user_id: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        identity: CallerIdentity = Depends(resolve_identity),
    ) -> JSONResponse:
        try:
            result = client.summarize_user_costs(
                tenant_id=tenant_id,
                user_id=user_id,
                start_at=start_at,
                end_at=end_at,
                identity=identity,
            )
            return _data_response(result, request, "req_user_cost_summary")
        except ModelAccessException as exc:
            return _error_response(exc, request.headers.get("x-request-id"))

    return router


def _data_response(
    data: Any,
    request: Request,
    fallback_request_id: str,
    status_code: int = 200,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(
            ResponseEnvelope(
                request_id=request.headers.get("x-request-id") or fallback_request_id,
                data=data,
            )
        )
    )


def create_app(
    client: ModelRepositoryClient,
    *,
    identity_resolver: IdentityResolver | None = None,
    title: str = "Model Access API",
) -> FastAPI:
    app = FastAPI(title=title, version=PROTOCOL_VERSION)
    install_exception_handlers(app)
    app.include_router(create_router(client, identity_resolver=identity_resolver))
    return app


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ModelAccessException)
    async def model_access_error_handler(
        request: Request, exc: ModelAccessException
    ) -> JSONResponse:
        return _error_response(exc, request.headers.get("x-request-id"))

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        error = ModelAccessException(
            ErrorCode.REQUEST_INVALID,
            "request validation failed",
            field=".".join(str(item) for item in exc.errors()[0].get("loc", ()))
            if exc.errors()
            else None,
            extensions={"validation_errors": _safe_validation_errors(exc.errors())},
        )
        return _error_response(error, request.headers.get("x-request-id"))


async def _sse(events: AsyncIterator[StreamEvent]) -> AsyncIterator[str]:
    async for item in events:
        payload = json.dumps(item.data, ensure_ascii=False, separators=(",", ":"))
        yield f"event: {item.event}\ndata: {payload}\n\n"


def _validate_protocol_header(header: str | None, body: str | None) -> None:
    if header:
        try:
            ensure_protocol_version(header)
        except ValueError as exc:
            raise ModelAccessException(
                code=ErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
                message=str(exc),
            ) from exc
    if header and body and header != body:
        raise ModelAccessException(
            code=ErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
            message="protocol version header and body do not match",
        )


def _error_response(exc: ModelAccessException, request_id: str | None) -> JSONResponse:
    envelope = ResponseEnvelope(
        request_id=request_id or "req_error",
        error=exc.to_dict(),
    )
    return JSONResponse(
        status_code=HTTP_STATUS_BY_ERROR.get(exc.code, 500),
        content=jsonable_encoder(envelope),
        headers={"X-Model-Protocol-Version": PROTOCOL_VERSION},
    )


def _safe_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "loc": [str(item) for item in error.get("loc", ())],
            "type": error.get("type"),
            "msg": error.get("msg"),
        }
        for error in errors[:20]
    ]
