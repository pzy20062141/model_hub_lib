from __future__ import annotations

from fastapi.testclient import TestClient

from model_access.api import create_app

HEADERS = {
    "Authorization": "Bearer test-token",
    "X-Tenant-ID": "tenant_001",
    "X-User-ID": "user_123",
    "X-Model-Protocol-Version": "1.1",
}


def registration_payload() -> dict:
    return {
        "tenant_id": "tenant_001",
        "user_id": "user_123",
        "provider": {"plugin_id": "builtin/mock", "provider_id": "mock"},
        "credential": {
            "name": "api mock",
            "base_url": "https://mock.local/v1",
            "api_key": "api-secret",
            "scope": "USER",
        },
    }


def test_three_http_interfaces(client) -> None:
    app = create_app(client)
    with TestClient(app) as http:
        response = http.post(
            "/api/v1/model-registrations",
            json=registration_payload(),
            headers={**HEADERS, "Idempotency-Key": "api-registration"},
        )
        assert response.status_code == 201, response.text
        assert "api-secret" not in response.text

        response = http.get(
            "/api/v1/models",
            params={
                "tenant_id": "tenant_001",
                "user_id": "user_123",
                "model_type": "text_generation",
            },
            headers=HEADERS,
        )
        assert response.status_code == 200, response.text
        configured_model_id = response.json()["data"]["items"][0]["configured_model_id"]

        invocation = {
            "protocol_version": "1.1",
            "context": {
                "tenant_id": "tenant_001",
                "user_id": "user_123",
                "session_id": "sess_api",
                "query_id": "qry_api",
            },
            "model": {
                "configured_model_id": configured_model_id,
                "model_type": "text_generation",
            },
            "operation": "chat",
            "response_mode": "blocking",
            "input": {
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
            },
            "metadata": {"scene": "conversation"},
        }
        response = http.post("/api/v1/model-invocations", json=invocation, headers=HEADERS)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "SUCCEEDED"


def test_api_protocol_mismatch_returns_stable_error(client) -> None:
    app = create_app(client)
    with TestClient(app) as http:
        response = http.post(
            "/api/v1/model-registrations",
            json=registration_payload(),
            headers={
                **HEADERS,
                "X-Model-Protocol-Version": "2.0",
                "Idempotency-Key": "wrong-protocol",
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "PROTOCOL_VERSION_UNSUPPORTED"
