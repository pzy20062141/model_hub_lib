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

列表响应中的每个模型包含 `is_default`，`data.default_models` 则返回全部模型类型的默认配置 ID 或 `null`。

## 设置和查询租户默认模型

租户管理员从 TENANT / SYSTEM 范围模型中取得 `configured_model_id` 后，可按模型类型保存租户共享默认配置。普通子用户只能查询和使用：

```bash
curl -X PUT http://localhost:8087/api/v1/model-defaults/text_generation \
  -H 'Authorization: Bearer <token>' \
  -H 'X-Tenant-ID: tenant_001' \
  -H 'X-User-ID: admin_001' \
  -H 'X-Roles: tenant_admin' \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id":"tenant_001",
    "configured_model_id":"cm_xxx"
  }'

curl 'http://localhost:8087/api/v1/model-defaults?tenant_id=tenant_001' \
  -H 'Authorization: Bearer <token>' \
  -H 'X-Tenant-ID: tenant_001' \
  -H 'X-User-ID: user_123'
```

把 `configured_model_id` 设为 `null` 可清空该类型默认模型。未设置或没有仍然可用的配置时，该类型返回 `null`。USER 范围模型不能设置为租户默认。

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
    "operation":"chat",
    "response_mode":"streaming",
    "input":{"messages":[{"role":"user","content":[{"type":"text","text":"总结设备告警"}]}]},
    "metadata":{"scene":"conversation"}
  }'
```

上例省略 `model` 字段，系统根据 `operation=chat` 推导出 `text_generation`，再加载该租户的文本生成默认模型。若显式传入 `configured_model_id` 或 `provider + model`，则使用显式选择。

省略 `model`、传 `null`、传 `{}` 都会触发租户默认模型兜底，并分别输出原因码为 `model_omitted`、`model_null`、`model_empty_object` 的 `WARNING` 日志。事件字段 `model_access_event` 固定为 `tenant_default_model_fallback`，可据此在日志平台建立监控或告警；日志只包含 operation 和请求定位 ID，不包含输入内容或凭据。
