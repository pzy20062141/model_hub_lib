from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

from opentelemetry import trace

from .adapters.registry import ProviderRegistry
from .contracts.entities import CallerIdentity, ProviderRef, RuntimeContext, SelfHostedDeployment
from .contracts.enums import (
    ErrorCode,
    InvocationStatus,
    ModelStatus,
    ModelType,
    ResponseMode,
)
from .contracts.invocation import (
    AdapterInvocation,
    ChatInput,
    EmbeddingInput,
    ImageContentPart,
    ImageGenerationInput,
    ModelInvocationRequest,
    SynthesisInput,
    VideoGenerationInput,
)
from .contracts.responses import (
    AdapterArtifact,
    AdapterAsyncTask,
    AdapterChunk,
    AdapterResponse,
    ArtifactRef,
    AsyncInvocationResult,
    InvocationResult,
    StreamEvent,
    Usage,
)
from .errors import ModelAccessException
from .observability import OpenTelemetryFacade, SpanHandle
from .persistence.repository import ModelAccessRepository, ResolvedModelRecord
from .protocols import (
    ArtifactStore,
    CredentialCipher,
    QuotaManager,
    TaskBackend,
    UserQuotaAwareManager,
)
from .routing import SelfHostedEndpointRouter


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


@dataclass(slots=True)
class InvocationState:
    request: ModelInvocationRequest
    resolved: ResolvedModelRecord
    invocation: AdapterInvocation
    started_at: float
    trace_id: str | None


class ModelRuntimeService:
    RETRYABLE_CODES = {
        ErrorCode.RATE_LIMITED,
        ErrorCode.PROVIDER_TIMEOUT,
        ErrorCode.PROVIDER_UNAVAILABLE,
    }

    def __init__(
        self,
        *,
        repository: ModelAccessRepository,
        providers: ProviderRegistry,
        cipher: CredentialCipher,
        artifact_store: ArtifactStore,
        task_backend: TaskBackend,
        quota: QuotaManager,
        observability: OpenTelemetryFacade,
        max_attempts: int = 3,
        endpoint_router: SelfHostedEndpointRouter | None = None,
    ):
        self._repository = repository
        self._providers = providers
        self._cipher = cipher
        self._artifact_store = artifact_store
        self._task_backend = task_backend
        self._quota = quota
        self._observability = observability
        self._max_attempts = max(1, max_attempts)
        self._endpoint_router = endpoint_router or SelfHostedEndpointRouter()

    async def invoke(
        self,
        request: ModelInvocationRequest,
        *,
        identity: CallerIdentity,
    ) -> InvocationResult | AsyncInvocationResult | AsyncIterator[StreamEvent]:
        self._authorize_context(request.context, identity)
        request.context.invocation_id = request.context.invocation_id or new_id("inv")
        request.context.request_id = request.context.request_id or new_id("req")
        if request.metadata.scene == "conversation" and not (
            request.context.session_id and request.context.query_id
        ):
            raise ModelAccessException(
                ErrorCode.CONTEXT_INVALID,
                "conversation scene requires session_id and query_id",
            )
        if not self._repository.bind_invocation(
            invocation_id=request.context.invocation_id,
            tenant_id=request.context.tenant_id,
            session_id=request.context.session_id,
            query_id=request.context.query_id,
        ):
            raise ModelAccessException(
                ErrorCode.CONTEXT_INVALID,
                "invocation_id is already bound to a different tenant, session, or query",
            )

        resolved = self._resolve_model(request)
        quota_allocation = None
        if isinstance(self._quota, UserQuotaAwareManager):
            assert request.context.user_id is not None
            quota_allocation = await self._quota.acquire_user_quota(
                invocation_id=request.context.invocation_id,
                tenant_id=request.context.tenant_id,
                user_id=request.context.user_id,
                roles=identity.roles,
                configured_model_id=resolved.model.configured_model_id,
                operation=request.operation.value,
                estimated_usage=self._estimate_usage(request, resolved),
            )

        reservation_active = quota_allocation is not None
        try:
            provider_ref, adapter, invocation = self._build_invocation(request, resolved)
            if not reservation_active:
                await self._quota.reserve(
                    invocation_id=request.context.invocation_id,
                    tenant_id=request.context.tenant_id,
                    configured_model_id=resolved.model.configured_model_id,
                    operation=request.operation.value,
                )
                reservation_active = True
            span = self._start_invocation_span(invocation)
        except Exception:
            if reservation_active:
                await self._quota.settle(
                    invocation_id=request.context.invocation_id,
                    usage=None,
                    succeeded=False,
                )
            raise
        started_at = time.monotonic()
        state = InvocationState(
            request=request,
            resolved=resolved,
            invocation=invocation,
            started_at=started_at,
            trace_id=trace.format_trace_id(span.span.get_span_context().trace_id)
            if span.span.get_span_context().is_valid
            else None,
        )
        try:
            adapter_result = await self._invoke_with_retry(adapter, invocation, span)
        except asyncio.CancelledError:
            exc = ModelAccessException(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "model invocation was cancelled",
            )
            await self._finalize_failure(state, exc, span)
            raise
        except ModelAccessException as exc:
            await self._finalize_failure(state, exc, span)
            raise
        except Exception as exc:
            wrapped = ModelAccessException(
                ErrorCode.INTERNAL_ERROR,
                "unexpected model runtime failure",
            )
            span.record_exception(exc, wrapped.code.value)
            await self._finalize_failure(state, wrapped, span)
            raise wrapped from exc

        if request.response_mode == ResponseMode.STREAMING:
            if not hasattr(adapter_result, "__aiter__"):
                exc = ModelAccessException(
                    ErrorCode.PROVIDER_BAD_RESPONSE,
                    "streaming invocation did not return an async stream",
                    provider=provider_ref.key,
                )
                await self._finalize_failure(state, exc, span)
                raise exc
            return self._stream_response(state, adapter_result, span)  # type: ignore[arg-type]
        if request.response_mode == ResponseMode.ASYNC:
            if not isinstance(adapter_result, AdapterAsyncTask):
                exc = ModelAccessException(
                    ErrorCode.PROVIDER_BAD_RESPONSE,
                    "async invocation did not return a provider task",
                    provider=provider_ref.key,
                )
                await self._finalize_failure(state, exc, span)
                raise exc
            return await self._async_response(state, adapter_result, span)
        if not isinstance(adapter_result, AdapterResponse):
            exc = ModelAccessException(
                ErrorCode.PROVIDER_BAD_RESPONSE,
                "blocking invocation returned an unsupported response",
                provider=provider_ref.key,
            )
            await self._finalize_failure(state, exc, span)
            raise exc
        return await self._blocking_response(state, adapter_result, span)

    def _resolve_model(self, request: ModelInvocationRequest) -> ResolvedModelRecord:
        selector = request.model
        assert selector is not None
        configured_model_id = selector.configured_model_id
        if selector.uses_default:
            configured_model_id = self._repository.get_tenant_default_model(
                tenant_id=request.context.tenant_id,
                model_type=selector.model_type,
            )
            if not configured_model_id:
                raise ModelAccessException(
                    ErrorCode.MODEL_NOT_FOUND,
                    f"no default model is configured for {selector.model_type.value}",
                )
        provider_key = (
            (selector.provider.plugin_id, selector.provider.provider_id)
            if selector.provider
            else None
        )
        resolved = self._repository.resolve_model(
            configured_model_id=configured_model_id,
            tenant_id=request.context.tenant_id,
            user_id=request.context.user_id,
            provider_key=provider_key,
            model=selector.model,
            model_type=selector.model_type.value,
        )
        if not resolved:
            raise ModelAccessException(
                ErrorCode.MODEL_NOT_FOUND, "configured model is not available"
            )
        return self._validate_resolved(request, resolved)

    def _build_invocation(
        self,
        request: ModelInvocationRequest,
        resolved: ResolvedModelRecord,
    ) -> tuple[ProviderRef, object, AdapterInvocation]:
        provider_ref = ProviderRef(
            plugin_id=resolved.model.plugin_id,
            provider_id=resolved.model.provider_id,
        )
        adapter = self._providers.get(provider_ref)
        credential_values = self._cipher.decrypt(resolved.credential.encrypted_values)
        deployment = (
            SelfHostedDeployment.model_validate(resolved.credential.deployment)
            if resolved.credential.deployment
            else None
        )
        invocation = AdapterInvocation(
            context=request.context,
            provider=provider_ref,
            model=resolved.model.model,
            model_type=ModelType(resolved.model.model_type),
            operation=request.operation,
            response_mode=request.response_mode,
            input=request.input,
            parameters=request.parameters,
            provider_options=request.provider_options.get(provider_ref.provider_id, {}),
            credential_values=credential_values,
            configured_model_id=resolved.model.configured_model_id,
            credential_id=resolved.credential.credential_id,
            deployment=deployment,
        )
        return provider_ref, adapter, invocation

    @staticmethod
    def _validate_resolved(
        request: ModelInvocationRequest, resolved: ResolvedModelRecord
    ) -> ResolvedModelRecord:
        assert request.model is not None
        if not resolved.provider_enabled:
            raise ModelAccessException(
                ErrorCode.MODEL_DISABLED,
                "configured model provider is disabled for this tenant",
            )
        if resolved.model.status != ModelStatus.ACTIVE.value:
            code = (
                ErrorCode.MODEL_DISABLED
                if resolved.model.status == ModelStatus.DISABLED.value
                else ErrorCode.PROVIDER_UNAVAILABLE
            )
            raise ModelAccessException(code, f"configured model status is {resolved.model.status}")
        if resolved.credential.status != "VALID":
            raise ModelAccessException(ErrorCode.CREDENTIAL_INVALID, "credential is not valid")
        if resolved.model.model_type != request.model.model_type.value:
            raise ModelAccessException(
                ErrorCode.MODEL_TYPE_MISMATCH,
                "requested model_type does not match configured model",
            )
        if request.operation.value not in resolved.model.operations:
            raise ModelAccessException(
                ErrorCode.MODEL_UNSUPPORTED,
                "configured model does not support the requested operation",
            )
        return resolved

    @staticmethod
    def _estimate_tokens(request: ModelInvocationRequest, resolved: ResolvedModelRecord) -> int:
        assert request.model is not None
        input_tokens = max(1, (len(request.input.model_dump_json()) + 3) // 4)
        output_tokens = 0
        if request.model.model_type == ModelType.TEXT_GENERATION:
            requested_output = request.parameters.get(
                "max_output_tokens", request.parameters.get("max_tokens", 512)
            )
            if isinstance(requested_output, int) and requested_output > 0:
                output_tokens = requested_output
            if resolved.model.max_output_tokens:
                output_tokens = min(output_tokens, resolved.model.max_output_tokens)
        return input_tokens + output_tokens

    @classmethod
    def _estimate_usage(
        cls, request: ModelInvocationRequest, resolved: ResolvedModelRecord
    ) -> Usage:
        total = cls._estimate_tokens(request, resolved)
        input_tokens = max(1, (len(request.input.model_dump_json()) + 3) // 4)
        output_tokens = max(0, total - input_tokens)
        billable_units: int = total
        billable_unit_type = "tokens"
        value = request.input
        if isinstance(value, ChatInput):
            image_count = sum(
                isinstance(part, ImageContentPart)
                for message in value.messages
                for part in message.content
            )
            if image_count:
                billable_units = image_count
                billable_unit_type = "input_images"
        elif isinstance(value, ImageGenerationInput):
            billable_units = value.count
            billable_unit_type = "output_images"
        elif isinstance(value, SynthesisInput):
            billable_units = len(value.text)
            billable_unit_type = "characters"
        elif isinstance(value, EmbeddingInput):
            billable_units = sum(len(text) for text in value.texts)
            billable_unit_type = "characters"
        elif isinstance(value, VideoGenerationInput):
            # The contract permits providers to apply their own default duration.
            # Reserve one second when it is omitted; async settlement can supply
            # the provider's actual seconds through Usage.
            billable_units = value.duration or 1
            billable_unit_type = "seconds"
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total,
            billable_units=billable_units,
            billable_unit_type=billable_unit_type,
            usage_source="estimated",
        )

    async def _invoke_with_retry(self, adapter, invocation, span):  # type: ignore[no-untyped-def]
        attempts = 1 if invocation.response_mode == ResponseMode.STREAMING else self._max_attempts
        last_error: ModelAccessException | None = None
        for attempt in range(attempts):
            span.set_attribute("model_access.retry.index", attempt)
            if invocation.deployment:
                endpoint = self._endpoint_router.select(invocation.deployment)
                invocation.credential_values["base_url"] = endpoint.base_url
                invocation.endpoint_id = endpoint.endpoint_id
                span.set_attribute("model_access.endpoint.id", endpoint.endpoint_id)
                span.set_attribute(
                    "model_access.deployment.mode",
                    invocation.deployment.deployment_mode.value,
                )
                span.set_attribute(
                    "model_access.deployment.version",
                    invocation.deployment.deployment_version,
                )
            try:
                result = await adapter.invoke(invocation)
                if invocation.endpoint_id:
                    self._endpoint_router.report_success(invocation.endpoint_id)
                return result
            except ModelAccessException as exc:
                if invocation.endpoint_id and exc.retryable:
                    self._endpoint_router.report_failure(invocation.endpoint_id)
                last_error = exc
                if (
                    attempt + 1 >= attempts
                    or not exc.retryable
                    or exc.code not in self.RETRYABLE_CODES
                ):
                    raise
                await asyncio.sleep(min(0.1 * (2**attempt), 1.0))
        assert last_error is not None
        raise last_error

    async def _blocking_response(
        self,
        state: InvocationState,
        response: AdapterResponse,
        span: SpanHandle,
    ) -> InvocationResult:
        try:
            artifacts = await self._store_artifacts(state, response.artifacts)
        except asyncio.CancelledError:
            exc = ModelAccessException(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "model invocation was cancelled",
            )
            await self._finalize_failure(
                state,
                exc,
                span,
                usage=response.usage,
                provider_billed=True,
            )
            raise
        except Exception as exc:
            wrapped = ModelAccessException(
                ErrorCode.INTERNAL_ERROR,
                "failed to persist provider artifacts",
            )
            span.record_exception(exc, wrapped.code.value)
            await self._finalize_failure(
                state,
                wrapped,
                span,
                usage=response.usage,
                provider_billed=True,
            )
            raise wrapped from exc
        latency_ms = int((time.monotonic() - state.started_at) * 1000)
        result = InvocationResult(
            invocation_id=state.request.context.invocation_id or "",
            session_id=state.request.context.session_id,
            query_id=state.request.context.query_id,
            output=response.output,
            artifacts=artifacts,
            usage=response.usage,
            provider_request_id=response.provider_request_id,
            response_model=response.response_model,
            finish_reason=response.finish_reason,
            latency_ms=latency_ms,
        )
        await self._finalize_success(state, response.usage, latency_ms, span)
        return result

    async def _async_response(
        self,
        state: InvocationState,
        response: AdapterAsyncTask,
        span: SpanHandle,
    ) -> AsyncInvocationResult:
        try:
            task_id = await self._task_backend.register_external_task(
                invocation_id=state.request.context.invocation_id or "",
                provider_task_id=response.provider_task_id,
                result_type=response.result_type,
                tenant_id=state.request.context.tenant_id,
                user_id=state.request.context.user_id,
                trace_context={
                    key: value
                    for key, value in {
                        "traceparent": state.request.context.traceparent,
                        "tracestate": state.request.context.tracestate,
                    }.items()
                    if value
                },
                provider_payload=response.provider_payload,
            )
        except asyncio.CancelledError:
            exc = ModelAccessException(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "model invocation was cancelled",
            )
            await self._finalize_failure(state, exc, span)
            raise
        except Exception as exc:
            wrapped = ModelAccessException(
                ErrorCode.INTERNAL_ERROR,
                "failed to persist provider task",
            )
            span.record_exception(exc, wrapped.code.value)
            await self._finalize_failure(state, wrapped, span)
            raise wrapped from exc
        latency_ms = int((time.monotonic() - state.started_at) * 1000)
        self._record_usage(state, None, latency_ms, InvocationStatus.ACCEPTED.value, None)
        # Async provider work is only accepted here. Keep the reservation active;
        # the poller/webhook must call ``finalize_async_quota`` with final usage.
        span.set_attribute("model_access.task.id", task_id)
        span.__exit__(None, None, None)
        return AsyncInvocationResult(
            invocation_id=state.request.context.invocation_id or "",
            session_id=state.request.context.session_id,
            query_id=state.request.context.query_id,
            task_id=task_id,
            result_type=response.result_type,
            estimated_wait_seconds=response.estimated_wait_seconds,
        )

    async def _stream_response(
        self,
        state: InvocationState,
        chunks: AsyncIterator[AdapterChunk],
        span: SpanHandle,
    ) -> AsyncIterator[StreamEvent]:
        invocation_id = state.request.context.invocation_id or ""
        usage: Usage | None = None
        finish_reason: str | None = None
        first_chunk_seen = False
        finalized = False
        try:
            yield StreamEvent(
                event="response.created",
                data={
                    "session_id": state.request.context.session_id,
                    "query_id": state.request.context.query_id,
                    "invocation_id": invocation_id,
                    "trace_id": state.trace_id,
                },
            )
            async for chunk in chunks:
                if chunk.delta:
                    if not first_chunk_seen:
                        first_chunk_seen = True
                        span.set_attribute(
                            "gen_ai.response.time_to_first_chunk",
                            time.monotonic() - state.started_at,
                        )
                    yield StreamEvent(
                        event="output.delta",
                        data={"index": chunk.index, "delta": chunk.delta},
                    )
                if chunk.usage:
                    usage = chunk.usage
                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason
            latency_ms = int((time.monotonic() - state.started_at) * 1000)
            await self._finalize_success(state, usage, latency_ms, span)
            finalized = True
            if usage:
                yield StreamEvent(event="usage", data=usage.model_dump(mode="json"))
            yield StreamEvent(
                event="response.completed",
                data={"finish_reason": finish_reason or "stop"},
            )
        except ModelAccessException as exc:
            await self._finalize_failure(
                state, exc, span, usage=usage, provider_billed=first_chunk_seen
            )
            finalized = True
            yield StreamEvent(event="error", data=exc.to_dict())
        except (GeneratorExit, asyncio.CancelledError):
            if not finalized:
                span.set_attribute("model_access.cancelled", True)
                exc = ModelAccessException(ErrorCode.PROVIDER_UNAVAILABLE, "stream was cancelled")
                await self._finalize_failure(
                    state, exc, span, usage=usage, provider_billed=first_chunk_seen
                )
            raise
        except Exception as exc:
            wrapped = ModelAccessException(
                ErrorCode.PROVIDER_BAD_RESPONSE,
                "provider stream failed",
                retryable=False,
            )
            span.record_exception(exc, wrapped.code.value)
            await self._finalize_failure(
                state, wrapped, span, usage=usage, provider_billed=first_chunk_seen
            )
            finalized = True
            yield StreamEvent(event="error", data=wrapped.to_dict())

    async def _store_artifacts(
        self,
        state: InvocationState,
        artifacts: list[AdapterArtifact],
    ) -> list[ArtifactRef]:
        result: list[ArtifactRef] = []
        for artifact in artifacts:
            if artifact.data is not None:
                result.append(
                    await self._artifact_store.put_bytes(
                        data=artifact.data,
                        media_type=artifact.media_type,
                        filename=artifact.filename,
                        tenant_id=state.request.context.tenant_id,
                        user_id=state.request.context.user_id,
                    )
                )
            elif artifact.uri:
                result.append(
                    await self._artifact_store.register_uri(
                        uri=artifact.uri,
                        media_type=artifact.media_type,
                        tenant_id=state.request.context.tenant_id,
                        user_id=state.request.context.user_id,
                    )
                )
        return result

    async def _finalize_success(
        self,
        state: InvocationState,
        usage: Usage | None,
        latency_ms: int,
        span: SpanHandle,
    ) -> None:
        self._record_usage(state, usage, latency_ms, InvocationStatus.SUCCEEDED.value, None)
        await self._quota.settle(
            invocation_id=state.request.context.invocation_id or "",
            usage=usage,
            succeeded=True,
        )
        self._observability.record_invocation(
            provider=state.invocation.provider.provider_id,
            model_type=state.invocation.model_type.value,
            status="succeeded",
            duration_seconds=latency_ms / 1000,
            usage=usage,
        )
        span.__exit__(None, None, None)

    async def _finalize_failure(
        self,
        state: InvocationState,
        exc: ModelAccessException,
        span: SpanHandle,
        *,
        usage: Usage | None = None,
        provider_billed: bool = False,
    ) -> None:
        latency_ms = int((time.monotonic() - state.started_at) * 1000)
        self._record_usage(state, None, latency_ms, InvocationStatus.FAILED.value, exc.code.value)
        await self._quota.settle(
            invocation_id=state.request.context.invocation_id or "",
            usage=usage,
            succeeded=provider_billed,
        )
        self._observability.record_invocation(
            provider=state.invocation.provider.provider_id,
            model_type=state.invocation.model_type.value,
            status="failed",
            duration_seconds=latency_ms / 1000,
            usage=None,
        )
        span.record_exception(exc, exc.code.value)
        span.__exit__(None, None, None)

    def _record_usage(
        self,
        state: InvocationState,
        usage: Usage | None,
        latency_ms: int,
        status: str,
        error_code: str | None,
    ) -> None:
        self._repository.record_usage(
            invocation_id=state.request.context.invocation_id or "",
            tenant_id=state.request.context.tenant_id,
            user_id=state.request.context.user_id,
            session_id=state.request.context.session_id,
            query_id=state.request.context.query_id,
            app_id=state.request.context.app_id,
            configured_model_id=state.resolved.model.configured_model_id,
            operation=state.request.operation.value,
            usage=usage,
            latency_ms=latency_ms,
            status=status,
            trace_id=state.trace_id,
            error_code=error_code,
        )

    def _start_invocation_span(self, invocation: AdapterInvocation) -> SpanHandle:
        return self._observability.start_span(
            f"{invocation.operation.value} {invocation.model}",
            {
                "gen_ai.operation.name": invocation.operation.value,
                "gen_ai.provider.name": invocation.provider.provider_id,
                "gen_ai.request.model": invocation.model,
                "gen_ai.request.stream": invocation.response_mode == ResponseMode.STREAMING,
                "gen_ai.conversation.id": invocation.context.session_id,
                "model_access.tenant.id": invocation.context.tenant_id,
                "model_access.session.id": invocation.context.session_id,
                "model_access.query.id": invocation.context.query_id,
                "model_access.invocation.id": invocation.context.invocation_id,
                "model_access.configured_model.id": invocation.configured_model_id,
                "model_access.credential.id": invocation.credential_id,
                "model_access.model.type": invocation.model_type.value,
                "model_access.response.mode": invocation.response_mode.value,
            },
            kind="client",
        )

    @staticmethod
    def _authorize_context(context: RuntimeContext, identity: CallerIdentity) -> None:
        if context.tenant_id != identity.tenant_id:
            raise ModelAccessException(ErrorCode.PERMISSION_DENIED, "tenant identity mismatch")
        if not context.user_id:
            raise ModelAccessException(
                ErrorCode.CONTEXT_INVALID,
                "user_id is required for per-user cost attribution, including service calls",
            )
        if not identity.is_service:
            if context.user_id != identity.user_id:
                raise ModelAccessException(ErrorCode.PERMISSION_DENIED, "user identity mismatch")
