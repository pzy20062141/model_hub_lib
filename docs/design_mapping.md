# 设计文档 v1.4 到代码的映射

| 设计职责 | 代码位置 | 已实现行为 |
| --- | --- | --- |
| ProviderRegistry | `adapters/registry.py` | Adapter 注册、启停检查、供应商发现 |
| ModelCatalog | `control_plane.py`、`persistence/repository.py` | 模型发现、目录写入、分类过滤、游标分页 |
| TenantModelConfiguration | `persistence/models.py`、`runtime.py` | USER/TENANT/SYSTEM 可见范围、凭据引用解析 |
| ResolvedModel | `persistence/repository.py::ResolvedModelRecord` | 配置模型、凭据、状态和部署信息绑定 |
| ProviderAdapterRuntime | `protocols.py`、`adapters/` | 标准请求转供应商协议、错误和响应归一化 |
| 模型注册接口 | `control_plane.py`、`api.py` | 鉴权核对、URL 校验、凭据验证、加密、发现、幂等 |
| 模型列表接口 | `control_plane.py`、`api.py` | 权限集合、分类/类型/供应商/状态过滤、脱敏返回 |
| 租户默认模型 | `tenant_default_model`、`control_plane.py`、`runtime.py`、`contracts/invocation.py` | 按 tenant/type 保存、管理员写权限、子用户继承、列表标记、隐式默认解析和缺参原因告警 |
| 统一调用接口 | `runtime.py`、`api.py` | blocking / streaming / async、operation 联合校验 |
| 自部署模型 | `entities.py`、`routing.py` | 部署对象、多端点加权路由、失败冷却、稳定 Service URL |
| 凭据安全 | `security.py` | Fernet 认证加密、Secret 脱敏、SSRF / 私网 URL 策略 |
| 用量账本 | `model_invocation_usage`、`runtime.py` | invocation_id 幂等、session/query/app/model 归集 |
| 子用户策略装配 | `user_quota.py`、`user_quota_template`、`user_quota_assignment` | 用户覆盖 → 角色模板 → 租户默认 → 平台月度 100 积分默认 |
| 模型积分规则 | `model_credit_rate`、`UserQuotaManager._calculate` | 按次、输入/输出 Token、billable unit 统一换算，费率快照 |
| 调用级额度 | `user_quota_period`、`user_quota_reservation` | tenant/user 隔离、日/月周期、原子预占、实际结算、失败释放 |
| 子用户成本 | `user_cost_ledger`、`query_user_costs`、`summarize_user_costs` | 按调用、用户、模型和 Usage 查询或汇总成本，Decimal 精度 |
| OpenTelemetry | `observability.py` | GenAI 和 model_access 属性、CLIENT Span、低基数 Metric |
| 数据库连接 | `persistence/database.py` | 逻辑数据库、凭据引用、本地/云配置、连接池 |
| 生态兼容边界 | `adapters/openai_compatible.py` | OpenAI-compatible 仅存在于 Adapter，不污染核心契约 |

## 有意保留为宿主平台扩展点的能力

- JWT、角色和服务身份解析：通过 `identity_resolver` 注入。
- KMS / Vault：实现 `CredentialCipher` 或在其外层接入 envelope encryption。
- BOS / S3：实现 `ArtifactStore`。
- 任务执行引擎：实现 `TaskBackend`，承接视频、图片和语音异步状态。
- 财务金额与发票：当前记录内部统一积分；货币、税率、发票和付款仍由财务域处理。
- Outbox 发布 Worker：事务内已写入事件，宿主平台负责投递、重试和更新 `published_at`。
- 模型训练或经验挖掘：消费 `model_invocation_usage` 与 OpenTelemetry 轨迹，不反向侵入模型调用协议。

这些能力保留接口而不绑定具体平台实现，确保本项目可以作为 lib 被不同 Runtime 加载，也可以在规模提升后将同一 Facade 暴露为独立服务。
