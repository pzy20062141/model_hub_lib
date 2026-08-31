from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from ..contracts.entities import (
    CredentialSet,
    CredentialValidationResult,
    ModelDescriptor,
    ProviderDescriptor,
    ProviderRef,
    RuntimeContext,
)
from ..contracts.enums import ErrorCode, ModelOperation, ResponseMode
from ..contracts.invocation import (
    AdapterInvocation,
    AudioContentPart,
    ChatInput,
    CompletionInput,
    EmbeddingInput,
    ImageContentPart,
    ImageGenerationInput,
    ModerationInput,
    RerankInput,
    SynthesisInput,
    TextContentPart,
    TranscriptionInput,
    VideoGenerationInput,
)
from ..contracts.responses import (
    AdapterArtifact,
    AdapterAsyncTask,
    AdapterChunk,
    AdapterResponse,
    Usage,
)
from ..errors import ModelAccessException, sanitize_message
from ..protocols import FileResolver


class OpenAICompatibleAdapter:
    """Adapter for OpenAI and explicitly OpenAI-compatible endpoints.

    Capability inference is deliberately avoided. `model_manifest` is the
    authoritative map from remote model IDs to model type/modalities/features.
    """

    DEFAULT_PATHS = {
        ModelOperation.CHAT: "/chat/completions",
        ModelOperation.TEXT_COMPLETION: "/completions",
        ModelOperation.EMBEDDINGS: "/embeddings",
        ModelOperation.RERANK: "/rerank",
        ModelOperation.TRANSCRIBE: "/audio/transcriptions",
        ModelOperation.SYNTHESIZE: "/audio/speech",
        ModelOperation.IMAGE_GENERATE: "/images/generations",
        ModelOperation.VIDEO_GENERATE: "/videos/generations",
        ModelOperation.MODERATE: "/moderations",
    }
    PROTECTED_OPTIONS = {
        "model",
        "messages",
        "prompt",
        "input",
        "stream",
        "api_key",
        "authorization",
        "base_url",
    }

    def __init__(
        self,
        *,
        descriptor: ProviderDescriptor,
        model_manifest: Sequence[ModelDescriptor] = (),
        client: httpx.AsyncClient | None = None,
        file_resolver: FileResolver | None = None,
        operation_paths: dict[ModelOperation, str] | None = None,
        request_timeout_seconds: float = 120.0,
    ):
        self._descriptor = descriptor.model_copy(update={"models": list(model_manifest)})
        self._models = {item.model: item for item in model_manifest}
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None
        self._file_resolver = file_resolver
        self._paths = {**self.DEFAULT_PATHS, **(operation_paths or {})}
        self._request_timeout = request_timeout_seconds

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def validate_credentials(
        self,
        *,
        context: RuntimeContext,
        provider: ProviderRef,
        credentials: CredentialSet,
    ) -> CredentialValidationResult:
        del context
        if provider.key != self.descriptor.provider.key:
            return CredentialValidationResult(
                valid=False,
                error_code=ErrorCode.PROVIDER_NOT_FOUND.value,
                message="adapter provider mismatch",
            )
        try:
            response = await self._client.get(
                self._url(credentials.values, "/models"),
                headers=self._headers(credentials.values),
                timeout=self._request_timeout,
            )
            self._raise_for_status(response, provider=provider, model=None)
            return CredentialValidationResult(
                valid=True,
                normalized_credentials={
                    "base_url": credentials.values.get("base_url", "").rstrip("/")
                },
            )
        except ModelAccessException as exc:
            return CredentialValidationResult(
                valid=False,
                error_code=exc.code.value,
                message=exc.message,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return CredentialValidationResult(
                valid=False,
                error_code=ErrorCode.PROVIDER_UNAVAILABLE.value,
                message=sanitize_message(str(exc)),
            )

    async def discover_models(
        self,
        *,
        context: RuntimeContext,
        provider: ProviderRef,
        credentials: CredentialSet,
        deployment: dict[str, Any] | None = None,
    ) -> Sequence[ModelDescriptor]:
        del context, deployment
        response = await self._client.get(
            self._url(credentials.values, "/models"),
            headers=self._headers(credentials.values),
            timeout=self._request_timeout,
        )
        self._raise_for_status(response, provider=provider, model=None)
        payload = self._json(response)
        remote_ids = {
            str(item["id"])
            for item in payload.get("data", [])
            if isinstance(item, dict) and item.get("id")
        }
        # Unknown remote models are intentionally not guessed. Administrators
        # can add an explicit ManualModelRegistration or update the manifest.
        return [item for model_id, item in self._models.items() if model_id in remote_ids]

    async def invoke(
        self,
        invocation: AdapterInvocation,
    ) -> AdapterResponse | AdapterAsyncTask | AsyncIterator[AdapterChunk]:
        if invocation.operation == ModelOperation.TRANSCRIBE:
            return await self._transcribe(invocation)
        if invocation.operation == ModelOperation.SYNTHESIZE:
            return await self._synthesize(invocation)

        payload = await self._payload(invocation)
        path = self._paths[invocation.operation]
        if invocation.response_mode == ResponseMode.STREAMING:
            return self._stream_json(invocation, path, payload)
        response = await self._post_json(invocation, path, payload)
        return self._normalize_json(invocation, response)

    async def _payload(self, invocation: AdapterInvocation) -> dict[str, Any]:
        data: dict[str, Any]
        value = invocation.input
        if isinstance(value, ChatInput):
            data = {
                "model": invocation.model,
                "messages": [await self._message(item, invocation) for item in value.messages],
            }
            if value.tools:
                data["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": item.name,
                            "description": item.description,
                            "parameters": item.parameters,
                        },
                    }
                    for item in value.tools
                ]
            if value.tool_choice is not None:
                data["tool_choice"] = value.tool_choice
            if value.stop:
                data["stop"] = value.stop
        elif isinstance(value, CompletionInput):
            data = {"model": invocation.model, "prompt": value.prompt, "stop": value.stop}
        elif isinstance(value, EmbeddingInput):
            data = {"model": invocation.model, "input": value.texts}
            if value.dimensions:
                data["dimensions"] = value.dimensions
            data["input_type"] = value.input_type
        elif isinstance(value, RerankInput):
            data = {
                "model": invocation.model,
                "query": value.query,
                "documents": value.documents,
                "top_n": value.top_n,
            }
            if value.score_threshold is not None:
                data["score_threshold"] = value.score_threshold
        elif isinstance(value, ImageGenerationInput):
            data = {
                "model": invocation.model,
                "prompt": value.prompt,
                "n": value.count,
                "size": value.size,
            }
            if value.negative_prompt:
                data["negative_prompt"] = value.negative_prompt
            if value.reference_file_ids:
                data["reference_file_ids"] = value.reference_file_ids
        elif isinstance(value, VideoGenerationInput):
            data = {
                "model": invocation.model,
                "prompt": value.prompt,
                "image_file_id": value.image_file_id,
                "reference_file_ids": value.reference_file_ids,
                "duration": value.duration,
                "resolution": value.resolution,
            }
        elif isinstance(value, ModerationInput):
            data = {"model": invocation.model, "input": value.input}
            if value.policy:
                data["policy"] = value.policy
        else:
            raise ModelAccessException(
                ErrorCode.MODEL_UNSUPPORTED,
                "input type is not supported by OpenAI-compatible adapter",
            )
        data.update(invocation.parameters)
        provider_options = self._safe_provider_options(invocation.provider_options)
        data.update(provider_options)
        data = {key: val for key, val in data.items() if val is not None}
        if invocation.response_mode == ResponseMode.STREAMING:
            data["stream"] = True
            data.setdefault("stream_options", {"include_usage": True})
        return data

    async def _message(self, message, invocation: AdapterInvocation) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        parts: list[dict[str, Any]] = []
        for part in message.content:
            if isinstance(part, TextContentPart):
                parts.append({"type": "text", "text": part.text})
            elif isinstance(part, ImageContentPart):
                if part.uri:
                    uri = part.uri
                else:
                    resolved = await self._resolve_file(part.file_id or "")
                    uri = f"data:{resolved.media_type};base64,{base64.b64encode(resolved.data).decode('ascii')}"
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": uri, "detail": part.detail or "auto"},
                    }
                )
            elif isinstance(part, AudioContentPart):
                resolved = await self._resolve_file(part.file_id)
                parts.append(
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": base64.b64encode(resolved.data).decode("ascii"),
                            "format": part.format or resolved.filename.rsplit(".", 1)[-1],
                        },
                    }
                )
        result: dict[str, Any] = {"role": message.role, "content": parts}
        if message.name:
            result["name"] = message.name
        if message.tool_call_id:
            result["tool_call_id"] = message.tool_call_id
        return result

    async def _transcribe(
        self, invocation: AdapterInvocation
    ) -> AdapterResponse | AdapterAsyncTask:
        value = invocation.input
        assert isinstance(value, TranscriptionInput)
        resolved = await self._resolve_file(value.file_id)
        form = {"model": invocation.model}
        if value.language:
            form["language"] = value.language
        form.update({key: str(val) for key, val in invocation.parameters.items()})
        try:
            response = await self._client.post(
                self._url(invocation.credential_values, self._paths[invocation.operation]),
                headers=self._headers(invocation.credential_values),
                data=form,
                files={"file": (resolved.filename, resolved.data, resolved.media_type)},
                timeout=self._timeout(invocation),
            )
        except httpx.TimeoutException as exc:
            raise self._transport_error(invocation, ErrorCode.PROVIDER_TIMEOUT, exc) from exc
        except httpx.NetworkError as exc:
            raise self._transport_error(invocation, ErrorCode.PROVIDER_UNAVAILABLE, exc) from exc
        self._raise_for_status(response, provider=invocation.provider, model=invocation.model)
        payload = self._json(response)
        if invocation.response_mode == ResponseMode.ASYNC:
            return self._async_task(payload, "transcription")
        return AdapterResponse(
            output={
                "type": "transcription",
                "text": payload.get("text", ""),
                "segments": payload.get("segments"),
            },
            usage=Usage.from_provider(payload.get("usage")),
            provider_request_id=response.headers.get("x-request-id"),
        )

    async def _synthesize(
        self,
        invocation: AdapterInvocation,
    ) -> AdapterResponse | AdapterAsyncTask | AsyncIterator[AdapterChunk]:
        value = invocation.input
        assert isinstance(value, SynthesisInput)
        payload = {
            "model": invocation.model,
            "input": value.text,
            "voice": value.voice,
            "response_format": value.format,
            "speed": value.speed,
            **invocation.parameters,
            **self._safe_provider_options(invocation.provider_options),
        }
        if invocation.response_mode == ResponseMode.STREAMING:
            return self._stream_binary(invocation, self._paths[invocation.operation], payload)
        response = await self._post_raw(invocation, self._paths[invocation.operation], payload)
        if invocation.response_mode == ResponseMode.ASYNC:
            try:
                return self._async_task(response.json(), "audio")
            except json.JSONDecodeError as exc:
                raise ModelAccessException(
                    ErrorCode.PROVIDER_BAD_RESPONSE,
                    "async speech provider did not return a task",
                ) from exc
        media_type = response.headers.get("content-type", f"audio/{value.format}").split(";", 1)[0]
        return AdapterResponse(
            output={"type": "audio"},
            artifacts=[
                AdapterArtifact(
                    media_type=media_type,
                    data=response.content,
                    filename=f"speech.{value.format}",
                )
            ],
            provider_request_id=response.headers.get("x-request-id"),
        )

    async def _post_json(
        self,
        invocation: AdapterInvocation,
        path: str,
        payload: dict[str, Any],
    ) -> httpx.Response:
        return await self._post_raw(invocation, path, payload)

    async def _post_raw(
        self,
        invocation: AdapterInvocation,
        path: str,
        payload: dict[str, Any],
    ) -> httpx.Response:
        try:
            response = await self._client.post(
                self._url(invocation.credential_values, path),
                headers=self._headers(invocation.credential_values),
                json=payload,
                timeout=self._timeout(invocation),
            )
        except httpx.TimeoutException as exc:
            raise self._transport_error(invocation, ErrorCode.PROVIDER_TIMEOUT, exc) from exc
        except httpx.NetworkError as exc:
            raise self._transport_error(invocation, ErrorCode.PROVIDER_UNAVAILABLE, exc) from exc
        self._raise_for_status(response, provider=invocation.provider, model=invocation.model)
        return response

    async def _stream_json(
        self,
        invocation: AdapterInvocation,
        path: str,
        payload: dict[str, Any],
    ) -> AsyncIterator[AdapterChunk]:
        try:
            async with self._client.stream(
                "POST",
                self._url(invocation.credential_values, path),
                headers=self._headers(invocation.credential_values),
                json=payload,
                timeout=self._timeout(invocation),
            ) as response:
                if not response.is_success:
                    await response.aread()
                self._raise_for_status(
                    response, provider=invocation.provider, model=invocation.model
                )
                index = 0
                async for line in response.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        item = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise ModelAccessException(
                            ErrorCode.PROVIDER_BAD_RESPONSE,
                            "provider returned invalid SSE JSON",
                        ) from exc
                    choices = item.get("choices") or []
                    choice = choices[0] if choices else {}
                    delta = choice.get("delta") or (
                        {"type": "text", "text": choice.get("text")} if choice.get("text") else None
                    )
                    if isinstance(delta, dict) and "content" in delta:
                        delta = {
                            "type": "text",
                            "text": delta.get("content", ""),
                            **{key: val for key, val in delta.items() if key != "content"},
                        }
                    yield AdapterChunk(
                        index=index,
                        delta=delta,
                        usage=Usage.from_provider(item.get("usage")),
                        finish_reason=choice.get("finish_reason"),
                        provider_request_id=item.get("id") or response.headers.get("x-request-id"),
                        response_model=item.get("model"),
                    )
                    index += 1
        except httpx.TimeoutException as exc:
            raise self._transport_error(invocation, ErrorCode.PROVIDER_TIMEOUT, exc) from exc
        except httpx.NetworkError as exc:
            raise self._transport_error(invocation, ErrorCode.PROVIDER_UNAVAILABLE, exc) from exc

    async def _stream_binary(
        self,
        invocation: AdapterInvocation,
        path: str,
        payload: dict[str, Any],
    ) -> AsyncIterator[AdapterChunk]:
        try:
            async with self._client.stream(
                "POST",
                self._url(invocation.credential_values, path),
                headers=self._headers(invocation.credential_values),
                json=payload,
                timeout=self._timeout(invocation),
            ) as response:
                if not response.is_success:
                    await response.aread()
                self._raise_for_status(
                    response, provider=invocation.provider, model=invocation.model
                )
                index = 0
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield AdapterChunk(
                            index=index,
                            delta={
                                "type": "audio",
                                "base64": base64.b64encode(chunk).decode("ascii"),
                                "media_type": response.headers.get("content-type", "audio/mpeg"),
                            },
                        )
                        index += 1
                yield AdapterChunk(index=index, finish_reason="stop")
        except httpx.TimeoutException as exc:
            raise self._transport_error(invocation, ErrorCode.PROVIDER_TIMEOUT, exc) from exc
        except httpx.NetworkError as exc:
            raise self._transport_error(invocation, ErrorCode.PROVIDER_UNAVAILABLE, exc) from exc

    def _normalize_json(
        self,
        invocation: AdapterInvocation,
        response: httpx.Response,
    ) -> AdapterResponse | AdapterAsyncTask:
        payload = self._json(response)
        request_id = payload.get("id") or response.headers.get("x-request-id")
        usage = Usage.from_provider(payload.get("usage"))
        if invocation.response_mode == ResponseMode.ASYNC:
            result_type = (
                "video" if invocation.operation == ModelOperation.VIDEO_GENERATE else "image"
            )
            return self._async_task(payload, result_type)
        if invocation.operation == ModelOperation.CHAT:
            choice = self._first_choice(payload)
            message = choice.get("message", {})
            output = {
                "type": "message",
                "role": message.get("role", "assistant"),
                "content": [{"type": "text", "text": message.get("content", "")}],
                "tool_calls": message.get("tool_calls"),
                "finish_reason": choice.get("finish_reason"),
            }
        elif invocation.operation == ModelOperation.TEXT_COMPLETION:
            choice = self._first_choice(payload)
            output = {
                "type": "text",
                "text": choice.get("text", ""),
                "finish_reason": choice.get("finish_reason"),
            }
        elif invocation.operation == ModelOperation.EMBEDDINGS:
            output = {
                "type": "vectors",
                "vectors": [item.get("embedding", []) for item in payload.get("data", [])],
            }
        elif invocation.operation == ModelOperation.RERANK:
            output = {
                "type": "ranked_documents",
                "ranked_documents": payload.get("results", payload.get("data", [])),
            }
        elif invocation.operation == ModelOperation.MODERATE:
            results = payload.get("results", [])
            output = {
                "type": "moderation",
                "results": results,
                "blocked": any(
                    bool(item.get("flagged")) for item in results if isinstance(item, dict)
                ),
            }
        elif invocation.operation == ModelOperation.IMAGE_GENERATE:
            artifacts: list[AdapterArtifact] = []
            for item in payload.get("data", []):
                if item.get("b64_json"):
                    artifacts.append(
                        AdapterArtifact(
                            media_type="image/png",
                            data=base64.b64decode(item["b64_json"]),
                            filename="generated.png",
                        )
                    )
                elif item.get("url"):
                    artifacts.append(AdapterArtifact(media_type="image/*", uri=item["url"]))
            return AdapterResponse(
                output={"type": "image", "count": len(artifacts)},
                artifacts=artifacts,
                usage=usage,
                provider_request_id=request_id,
                response_model=payload.get("model"),
            )
        else:
            output = {"type": "provider_response", "data": payload}
        return AdapterResponse(
            output=output,
            usage=usage,
            provider_request_id=request_id,
            response_model=payload.get("model"),
            finish_reason=output.get("finish_reason"),
        )

    @staticmethod
    def _async_task(payload: dict[str, Any], result_type: str) -> AdapterAsyncTask:
        provider_task_id = payload.get("task_id") or payload.get("id")
        if not provider_task_id:
            raise ModelAccessException(
                ErrorCode.PROVIDER_BAD_RESPONSE,
                "async provider response did not include task_id",
            )
        return AdapterAsyncTask(
            provider_task_id=str(provider_task_id),
            result_type=result_type,
            estimated_wait_seconds=payload.get("estimated_wait_seconds"),
            provider_payload={
                key: value
                for key, value in payload.items()
                if key in {"status", "poll_url", "created_at", "expires_at"}
            },
        )

    @staticmethod
    def _first_choice(payload: dict[str, Any]) -> dict[str, Any]:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelAccessException(
                ErrorCode.PROVIDER_BAD_RESPONSE,
                "provider response does not contain choices",
            )
        return choices[0]

    async def _resolve_file(self, file_id: str):  # type: ignore[no-untyped-def]
        if not self._file_resolver:
            raise ModelAccessException(
                ErrorCode.REQUEST_INVALID,
                "file_resolver is required for file-based model input",
            )
        return await self._file_resolver.resolve(file_id)

    @staticmethod
    def _url(credentials: dict[str, Any], path: str) -> str:
        base_url = str(credentials.get("base_url", "")).rstrip("/")
        if not base_url:
            raise ModelAccessException(ErrorCode.CREDENTIAL_INVALID, "credential has no base_url")
        return f"{base_url}/{path.lstrip('/')}"

    @staticmethod
    def _headers(credentials: dict[str, Any]) -> dict[str, str]:
        headers = {"accept": "application/json"}
        api_key = credentials.get("api_key")
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        return headers

    def _timeout(self, invocation: AdapterInvocation) -> float:
        if invocation.deployment:
            return invocation.deployment.request_timeout_ms / 1000
        return self._request_timeout

    def _safe_provider_options(self, options: dict[str, Any]) -> dict[str, Any]:
        forbidden = {key.lower() for key in options} & self.PROTECTED_OPTIONS
        if forbidden:
            raise ModelAccessException(
                ErrorCode.REQUEST_INVALID,
                f"provider_options contains protected keys: {', '.join(sorted(forbidden))}",
            )
        return dict(options)

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ModelAccessException(
                ErrorCode.PROVIDER_BAD_RESPONSE,
                "provider returned invalid JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise ModelAccessException(
                ErrorCode.PROVIDER_BAD_RESPONSE,
                "provider response must be a JSON object",
            )
        return payload

    @staticmethod
    def _raise_for_status(
        response: httpx.Response,
        *,
        provider: ProviderRef,
        model: str | None,
    ) -> None:
        if response.is_success:
            return
        status = response.status_code
        if status in {401, 403}:
            code, retryable = ErrorCode.CREDENTIAL_INVALID, False
        elif status == 404:
            code, retryable = ErrorCode.MODEL_NOT_FOUND, False
        elif status == 429:
            code, retryable = ErrorCode.RATE_LIMITED, True
        elif status in {408, 504}:
            code, retryable = ErrorCode.PROVIDER_TIMEOUT, True
        elif status >= 500:
            code, retryable = ErrorCode.PROVIDER_UNAVAILABLE, True
        else:
            code, retryable = ErrorCode.REQUEST_INVALID, False
        provider_code = None
        message = f"provider request failed with HTTP {status}"
        try:
            payload = response.json()
            error = payload.get("error", payload) if isinstance(payload, dict) else {}
            if isinstance(error, dict):
                provider_code = error.get("code") or error.get("type")
                provider_message = error.get("message")
                if provider_message:
                    message = sanitize_message(str(provider_message))
        except json.JSONDecodeError:
            pass
        raise ModelAccessException(
            code,
            message,
            retryable=retryable,
            provider=provider.key,
            model=model,
            provider_error_code=str(provider_code) if provider_code else None,
        )

    @staticmethod
    def _transport_error(
        invocation: AdapterInvocation,
        code: ErrorCode,
        exc: Exception,
    ) -> ModelAccessException:
        return ModelAccessException(
            code,
            sanitize_message(str(exc)) or code.value,
            retryable=True,
            provider=invocation.provider.key,
            model=invocation.model,
            model_type=invocation.model_type,
        )
