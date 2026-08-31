from __future__ import annotations

from model_access.contracts.entities import (
    CredentialInput,
    ProviderRef,
    RuntimeContext,
)
from model_access.contracts.enums import CredentialScope, ModelOperation, ModelType, ResponseMode
from model_access.contracts.invocation import (
    ChatInput,
    ModelInvocationRequest,
    ModelRegistrationRequest,
    ModelSelector,
    PromptMessage,
    TextContentPart,
)


def registration_request(
    provider: ProviderRef, *, api_key: str = "secret-key"
) -> ModelRegistrationRequest:
    return ModelRegistrationRequest(
        tenant_id="tenant_001",
        user_id="user_123",
        provider=provider,
        credential=CredentialInput(
            name="mock credential",
            base_url="https://mock.local/v1",
            api_key=api_key,
            scope=CredentialScope.USER,
        ),
    )


def chat_request(configured_model_id: str, *, mode: ResponseMode = ResponseMode.BLOCKING):
    return ModelInvocationRequest(
        context=RuntimeContext(
            tenant_id="tenant_001",
            user_id="user_123",
            session_id="sess_1",
            query_id="query_1",
        ),
        model=ModelSelector(
            configured_model_id=configured_model_id,
            model_type=ModelType.TEXT_GENERATION,
        ),
        operation=ModelOperation.CHAT,
        response_mode=mode,
        input=ChatInput(
            messages=[
                PromptMessage(
                    role="user",
                    content=[TextContentPart(type="text", text="hello")],
                )
            ]
        ),
        metadata={"scene": "conversation"},
    )
