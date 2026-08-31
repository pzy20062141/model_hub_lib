from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from .client import ModelRepositoryClient
from .contracts.common import PROTOCOL_VERSION, ensure_protocol_version
from .contracts.entities import CallerIdentity
from .contracts.enums import ErrorCode, ModelCategory, ModelStatus, ModelType
from .contracts.invocation import ModelInvocationRequest, ModelListQuery, ModelRegistrationRequest
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

    return router


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
