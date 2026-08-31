# HTTP 接口示例

以下示例省略真实 JWT 内容。`tenant_id` 和 `user_id` 必须与身份解析结果一致。

## 注册

```bash
curl -X POST http://localhost:8087/api/v1/model-registrations \
  -H 'Authorization: Bearer <token>' \
  -H 'X-Tenant-ID: tenant_001' \
  -H 'X-User-ID: user_123' \
  -H 'X-Model-Protocol-Version: 1.1' \
  -H 'Idempotency-Key: registration-001' \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id":"tenant_001",
    "user_id":"user_123",
    "provider":{"plugin_id":"builtin/openai-compatible","provider_id":"openai_compatible"},
    "credential":{"name":"team model","base_url":"https://api.example.com/v1","api_key":"<secret>","scope":"USER"}
  }'
```

## 列表

```bash
curl 'http://localhost:8087/api/v1/models?tenant_id=tenant_001&user_id=user_123&category=TEXT_MODEL' \
  -H 'Authorization: Bearer <token>' \
  -H 'X-Tenant-ID: tenant_001' \
  -H 'X-User-ID: user_123'
```

## 流式调用

```bash
curl -N -X POST http://localhost:8087/api/v1/model-invocations \
  -H 'Authorization: Bearer <token>' \
  -H 'X-Tenant-ID: tenant_001' \
  -H 'X-User-ID: user_123' \
  -H 'Content-Type: application/json' \
  -d '{
    "protocol_version":"1.1",
    "context":{"tenant_id":"tenant_001","user_id":"user_123","session_id":"sess_1","query_id":"qry_1"},
    "model":{"configured_model_id":"cm_xxx","model_type":"text_generation"},
    "operation":"chat",
    "response_mode":"streaming",
    "input":{"messages":[{"role":"user","content":[{"type":"text","text":"总结设备告警"}]}]},
    "metadata":{"scene":"conversation"}
  }'
```

