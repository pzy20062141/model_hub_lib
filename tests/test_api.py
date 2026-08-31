from __future__ import annotations

from fastapi.testclient import TestClient

from model_access.api import create_app

HEADERS = {
    "Authorization": "Bearer test-token",
    "X-Tenant-ID": "tenant_001",
    "X-User-ID": "user_123",
    "X-Model-Protocol-Version": "1.1",
}
ADMIN_HEADERS = {**HEADERS, "X-Roles": "tenant_admin"}
CHILD_HEADERS = {**HEADERS, "X-User-ID": "child_001"}


def registration_payload() -> dict:
    return {
        "tenant_id": "tenant_001",
        "user_id": "user_123",
        "provider": {"plugin_id": "builtin/mock", "provider_id": "mock"},
        "credential": {
            "name": "api mock",
            "base_url": "https://mock.local/v1",
            "api_key": "api-secret",
            "scope": "TENANT",
        },
    }


def test_http_registration_defaults_catalog_and_invocation(client) -> None:
    app = create_app(client)
    with TestClient(app) as http:
        response = http.post(
            "/api/v1/model-registrations",
            json=registration_payload(),
            headers={**ADMIN_HEADERS, "Idempotency-Key": "api-registration"},
        )
        assert response.status_code == 201, response.text
        assert "api-secret" not in response.text

        response = http.get(
            "/api/v1/models",
            params={
                "tenant_id": "tenant_001",
                "user_id": "child_001",
                "model_type": "text_generation",
            },
            headers=CHILD_HEADERS,
        )
        assert response.status_code == 200, response.text
        configured_model_id = response.json()["data"]["items"][0]["configured_model_id"]

        response = http.put(
            "/api/v1/user-quotas/model-rates",
            json={
                "tenant_id": "tenant_001",
                "configured_model_id": configured_model_id,
                "per_request_credits": "1",
            },
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 200, response.text

        response = http.put(
            "/api/v1/user-quotas/users",
            json={
                "tenant_id": "tenant_001",
                "user_id": "child_001",
                "override_mode": "LIMITED",
                "credit_limit": "10",
            },
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 204, response.text

        response = http.put(
            "/api/v1/model-defaults/text_generation",
            json={
                "tenant_id": "tenant_001",
                "configured_model_id": configured_model_id,
            },
            headers=HEADERS,
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"

        response = http.put(
            "/api/v1/model-defaults/text_generation",
            json={
                "tenant_id": "tenant_001",
                "configured_model_id": configured_model_id,
            },
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["defaults"]["text_generation"] == configured_model_id

        response = http.get(
            "/api/v1/model-defaults",
            params={"tenant_id": "tenant_001"},
            headers=CHILD_HEADERS,
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["defaults"]["text_generation"] == configured_model_id

        response = http.get(
            "/api/v1/models",
            params={
                "tenant_id": "tenant_001",
                "user_id": "child_001",
                "model_type": "text_generation",
            },
            headers=CHILD_HEADERS,
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["items"][0]["is_default"] is True

        invocation = {
            "protocol_version": "1.1",
            "context": {
                "tenant_id": "tenant_001",
                "user_id": "child_001",
                "session_id": "sess_api",
                "query_id": "qry_api",
            },
            "operation": "chat",
            "response_mode": "blocking",
            "input": {
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
            },
            "metadata": {"scene": "conversation"},
        }
        response = http.post(
            "/api/v1/model-invocations",
            json=invocation,
            headers=CHILD_HEADERS,
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "SUCCEEDED"

        response = http.get(
            "/api/v1/user-quotas/users/child_001",
            params={"tenant_id": "tenant_001"},
            headers=CHILD_HEADERS,
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["credits_used"] == "1.000000"

        response = http.get(
            "/api/v1/user-costs",
            params={"tenant_id": "tenant_001", "user_id": "child_001"},
            headers=CHILD_HEADERS,
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["total_credits"] == "1.000000"


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
