# Changelog

## 0.4.1 - 2026-09-03

- 修复阻塞模型调用丢失 `finish_reason` 和 `response_model` 的问题，确保 Adapter 响应元数据完整传递到 `InvocationResult`。

## 0.4.0 - 2026-09-01

- 新增租户级供应商总开关与单模型开关，状态持久化且仅模型管理员可修改。
- 供应商关闭时统一阻止目录展示为可用、默认模型选择和实际调用，同时保留各模型自身开关状态。
- 新增 Client、FastAPI 可用性接口、审计/Outbox 事件和 PostgreSQL `006_model_availability.sql`。

## 0.3.3 - 2026-09-01

- 修复 PostgreSQL 注册供应商凭据和目录模型时的外键写入顺序，先显式持久化 `provider_credential`，再插入 `configured_model`。
- 新增开启 SQLite 外键约束的注册回归测试，并使用 PostgreSQL 临时 schema 验证真实事务行为。

## 0.3.2 - 2026-08-31

- 新增基于已加密供应商凭据注册额外模型的 Client 与 FastAPI 接口，避免重复提交 API Key。
- 已有凭据注册模型时校验 tenant、用户、凭据范围与管理员权限，并保证同一凭据下模型注册幂等。

## 0.3.1 - 2026-08-31

- 未配置用户配额时改为平台默认每月 100 积分，并保留显式 `UNLIMITED` 策略。
- 新增租户与单用户模型调用次数、积分成本汇总接口，供宿主管理中心展示租户和个人视图。

## 0.3.0 - 2026-08-31

- 配额业务边界改为 `tenant_id + user_id` 子用户预算，移除供应商 PAID/FREE/TRIAL 池、SYSTEM/CUSTOM 配额仲裁及自动降级公开能力。
- 新增模型积分换算规则，支持按次、输入 Token、输出 Token 和 billable unit；使用 Decimal/NUMERIC(20,6) 并保存费率快照。
- 新增租户默认模板、角色模板、用户 LIMITED/UNLIMITED/INHERIT 覆盖，以及 DAY/MONTH 周期。
- 新增调用前原子预占、实际用量结算、失败释放、流式已计费异常处理和异步显式最终结算。
- 新增逐用户成本台账、用户额度汇总、租户管理员跨用户查询和管理变更审计。
- 模型列表返回当前子用户额度状态；服务身份调用也必须提供 on-behalf-of `user_id`。
- 新增六个子用户配额 FastAPI 管理/查询接口和 PostgreSQL `005_child_user_quota.sql`。
- 更新阿里百炼流式/非流式示例、README 和配额设计文档。

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
- 默认模型调整为 `tenant_id + model_type` 维度，仅管理员可配置，所有子用户共享。
- 模型调用未提供 `model` 时，根据 operation 推导类型并自动加载租户默认模型；显式选模仍优先。
- 模型调用省略 `model`、传 `null` 或 `{}` 时输出带原因码和低敏感定位字段的结构化 WARNING 日志。
- 新增 `004_tenant_default_model.sql`；旧用户级配置不自动升级为租户默认，保留 legacy 表供审计。
- 新增 DeepSeek、百度智能云千帆、火山引擎方舟 OpenAI 兼容供应商清单。

## 0.1.0 - 2026-08-29

- 实现模型注册、模型列表和统一模型调用三个核心接口。
- 实现多租户权限、凭据认证加密、幂等注册和用量账本。
- 实现文本、Embedding、Rerank、STT、TTS、图片、视频和 Moderation 契约。
- 实现阻塞、SSE 流式和异步任务返回。
- 实现 OpenAI-compatible Adapter、YAML 模型清单和自部署多端点路由。
- 实现 FastAPI 适配层、OpenTelemetry 封装、数据库配置与 PostgreSQL 迁移。
