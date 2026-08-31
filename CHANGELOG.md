# Changelog

## 0.2.0 - 2026-08-29

- 新增 CLOUD 托管额度规格和租户首次访问懒初始化。
- 新增 PAID → FREE → TRIAL 多层配额池、模型白名单和 TIMES/TOKENS 两种单位。
- 新增 SYSTEM/CUSTOM 偏好与实际使用双字段；系统额度耗尽时自动降级到自定义凭据。
- 模型列表新增 `QUOTA_EXCEEDED`、`NO_CONFIGURE`、实际配置和剩余额度状态。
- 新增调用级原子预占、幂等结算、失败回滚、额度耗尽失效和并发容量条件更新。
- 新增数据库配置版本、Redis 版本化缓存实现和事务 Outbox 事件。
- SYSTEM 凭据注册限制为 `system_admin`。
- 补充租户配额查询方法；更新额度上限时默认保留历史用量和在途预占。
- 新增阿里百炼 SYSTEM 供应商注册、租户额度配置、模型列表，以及流式和非流式文本调用完整示例。

## 0.1.0 - 2026-08-29

- 实现模型注册、模型列表和统一模型调用三个核心接口。
- 实现多租户权限、凭据认证加密、幂等注册和用量账本。
- 实现文本、Embedding、Rerank、STT、TTS、图片、视频和 Moderation 契约。
- 实现阻塞、SSE 流式和异步任务返回。
- 实现 OpenAI-compatible Adapter、YAML 模型清单和自部署多端点路由。
- 实现 FastAPI 适配层、OpenTelemetry 封装、数据库配置与 PostgreSQL 迁移。
