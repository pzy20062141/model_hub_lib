# 大模型统一接入 Python Lib

面向智能体、工作流、知识库和应用服务的多租户模型接入库。业务模块只依赖 `ModelRepositoryClient`，不直接依赖供应商 SDK、API Key 或供应商响应结构。

当前提供三类核心接口：

1. `register_model` / `POST /api/v1/model-registrations`：注册凭据、验证并发现模型。
2. `list_models` / `GET /api/v1/models`：获取当前租户与子用户可用模型。
3. `invoke` / `POST /api/v1/model-invocations`：统一调用文本、向量、重排序、语音、图片、视频和审核模型。

另外支持租户级默认模型，以及 `tenant_id + user_id` 维度的子用户预算和成本台账。

## 1. 核心能力

- 控制面、运行面和供应商 Adapter 解耦，新增供应商不修改业务协议。
- USER / TENANT / SYSTEM 三种模型可见范围，凭据使用 Fernet 认证加密。
- 文本生成、Embedding、Rerank、STT、TTS、图片、视频和 Moderation 统一契约。
- 阻塞、SSE 流式和异步任务三种响应模式。
- 阿里百炼、OpenAI、DeepSeek、百度千帆、火山引擎方舟内置清单。
- 租户管理员按模型类型设置共享默认模型；子用户调用时可省略 `model`。
- 子用户配额按 `tenant_id + user_id` 隔离，支持租户默认模板、角色模板、用户覆盖、日/月周期。
- 统一积分计量；调用前原子预占，调用成功后按实际用量结算，失败释放预占。
- 每次成功调用写入逐用户成本台账，计费规则保存快照，后续调价不改历史成本。
- OpenTelemetry 统一追踪，默认不记录明文凭据或 Prompt 正文。

配额只用于企业租户控制子用户成本，不管理租户总池或供应商 PAID/FREE/TRIAL 额度，也不会因用户额度状态切换供应商或 API Key。

## 2. 安装

推荐 Python 3.11 或 3.12：

```bash
uv sync --extra api --extra test
```

作为其他项目的本地依赖：

```toml
[project]
dependencies = [
  "model-access-lib @ file:///absolute/path/to/model-access-lib"
]
```

生产环境应从 KMS、Vault 或密钥注入机制提供固定的 Fernet Key：

```bash
uv run python -c "from model_access import FernetCredentialCipher; print(FernetCredentialCipher.generate_key())"
```

不要在每次启动时生成新密钥，否则已保存凭据无法解密。

## 3. 初始化与三个核心接口

```python
import os
from model_access import ModelRepositoryClient

client = ModelRepositoryClient.sqlite(
    "model_access.db",
    encryption_key=os.environ["MODEL_ACCESS_MASTER_KEY"],
)
client.register_adapter(provider_adapter)

registration = await client.register_model(
    registration_request,
    identity=authenticated_identity,
    idempotency_key="register-001",
)

# 复用已加密保存的供应商凭据，显式注册额外模型时无需再次提交 API Key。
additional = await client.register_model_with_credential(
    existing_credential_model_request,
    identity=authenticated_identity,
)
models = await client.list_models(list_query, identity=authenticated_identity)
result_or_stream = await client.invoke(invocation, identity=authenticated_identity)
```

可运行示例见 `examples/basic_usage.py`、`examples/fastapi_app.py` 和 `examples/aliyun_bailian_quota_demo.py`。

## 4. 统一 operation

| operation | model_type | 响应模式 |
| --- | --- | --- |
| `chat` / `text_completion` | `text_generation` | blocking / streaming |
| `embeddings` | `embedding` | blocking |
| `rerank` | `rerank` | blocking |
| `transcribe` | `speech_to_text` | blocking / async |
| `synthesize` | `text_to_speech` | blocking / streaming / async |
| `image_generate` | `image_generation` | blocking / async |
| `video_generate` | `video_generation` | async |
| `moderate` | `moderation` | blocking |

视觉理解使用 `text_generation + chat`，由模型的 `input_modalities` 包含 `image` 表达能力。

## 5. 租户默认模型

默认模型按 `tenant_id + model_type` 保存，由租户内所有子用户共享。只有 `tenant_admin` 可以新增、修改或清空。默认模型必须来自 TENANT 或 SYSTEM 范围的有效模型；没有对应类型时返回 `None`。

```python
from model_access.contracts.entities import CallerIdentity
from model_access.contracts.enums import ModelType
from model_access.contracts.invocation import TenantDefaultModelUpdateRequest

tenant_admin = CallerIdentity(
    tenant_id="tenant_001",
    user_id="admin_001",
    roles={"tenant_admin"},
)

defaults = await client.set_default_model(
    TenantDefaultModelUpdateRequest(
        tenant_id="tenant_001",
        configured_model_id="cm_qwen_plus",
    ),
    model_type=ModelType.TEXT_GENERATION,
    identity=tenant_admin,
)
```

模型调用省略 `model`、传 `null` 或 `{}` 时，系统根据 `operation` 推导模型类型并加载租户默认模型，同时输出结构化 WARNING 日志，便于发现上游漏参。显式选择模型始终优先。

## 6. 子用户配额与成本管理

### 6.1 业务边界与权限

- 配额归属键是 `tenant_id + user_id`，不会建立 tenant 或 provider 总额度池。
- 只有目标租户的 `tenant_admin` 可以配置费率、模板、角色绑定和用户覆盖。
- 子用户可查询自己的额度和成本；`tenant_admin` 可查询租户内任意用户或汇总列表。
- 用户与角色主数据仍由宿主权限系统管理，本库只读取 `CallerIdentity.roles`。
- 未配置任何策略时使用平台默认月度 100 积分，调用按默认每次 1 积分记入成本台账。

### 6.2 配置模型积分换算规则

```python
from decimal import Decimal
from model_access.contracts.quota import ModelCreditRateInput

rate = client.configure_model_credit_rate(
    ModelCreditRateInput(
        tenant_id="tenant_001",
        configured_model_id="cm_qwen_plus",
        per_request_credits=Decimal("1"),
        input_credits_per_1k=Decimal("0.5"),
        output_credits_per_1k=Decimal("1.5"),
        billable_unit_credits=Decimal("0"),
    ),
    identity=tenant_admin,
)
```

积分计算公式：

```text
实际积分 = 每次请求积分
         + 输入 token / 1000 × 输入积分单价
         + 输出 token / 1000 × 输出积分单价
         + billable_units × 单位积分单价
```

预占记录保存模型费率快照。即使管理员后续调价，历史调用仍保留当时的积分和规则版本。

### 6.3 配置租户默认额度模板

```python
from model_access.contracts.enums import QuotaPeriodType
from model_access.contracts.quota import UserQuotaTemplateInput

default_template = client.configure_user_quota_template(
    UserQuotaTemplateInput(
        tenant_id="tenant_001",
        name="全员月度默认额度",
        period_type=QuotaPeriodType.MONTH,
        credit_limit=Decimal("1000"),  # None 表示无限
        soft_limit_percent=80,
        is_default=True,
    ),
    identity=tenant_admin,
)
```

`DAY` 按 UTC 自然日重置，`MONTH` 按 UTC 自然月重置。修改模板不会清空当前周期已用和在途积分，只更新当前有效上限和策略来源。

### 6.4 配置角色模板

```python
from model_access.contracts.quota import RoleQuotaBindingInput

developer_template = client.configure_user_quota_template(
    UserQuotaTemplateInput(
        tenant_id="tenant_001",
        name="开发者日额度",
        period_type=QuotaPeriodType.DAY,
        credit_limit=Decimal("200"),
    ),
    identity=tenant_admin,
)
client.bind_quota_template_to_role(
    RoleQuotaBindingInput(
        tenant_id="tenant_001",
        role_code="developer",
        template_id=developer_template.template_id,
        priority=100,
    ),
    identity=tenant_admin,
)
```

一个用户命中多个角色时选择 `priority` 最大的模板；相同优先级按 `role_code` 排序，保证结果确定。

### 6.5 单独覆盖某个子用户

```python
from model_access.contracts.enums import QuotaOverrideMode
from model_access.contracts.quota import UserQuotaAssignmentInput

client.assign_user_quota(
    UserQuotaAssignmentInput(
        tenant_id="tenant_001",
        user_id="user_123",
        override_mode=QuotaOverrideMode.LIMITED,
        credit_limit=Decimal("50"),
    ),
    identity=tenant_admin,
)
```

`LIMITED` 设置硬限额，`UNLIMITED` 对该用户取消阻断但继续记账，`INHERIT` 可指定模板或继续继承角色/租户默认策略。设置 `enabled=False` 可立即停用该用户的模型调用。

有效策略优先级为：用户覆盖 → 用户指定模板 → 最高优先级角色模板 → 租户默认模板 → 平台默认月度 100 积分。

### 6.6 查询额度与逐用户成本

```python
summary = client.get_user_quota(
    tenant_id="tenant_001",
    user_id="user_123",
    roles={"developer"},
    identity=tenant_admin,
)
print(summary.status, summary.credits_used, summary.credits_remaining)

costs = client.query_user_costs(
    tenant_id="tenant_001",
    user_id="user_123",  # tenant_admin 传 None 可查询整个租户
    identity=tenant_admin,
)
for item in costs.items:
    print(item.user_id, item.configured_model_id, item.credits, item.usage)

aggregate = client.summarize_user_costs(
    tenant_id="tenant_001",
    user_id=None,  # tenant_admin 汇总整个租户；子用户只能传自己的 user_id
    identity=tenant_admin,
)
print(aggregate.invocation_count, aggregate.total_credits, aggregate.by_user)
```

状态含义：

| 状态 | 行为 |
| --- | --- |
| `ACTIVE` | 未达到软阈值，可调用 |
| `SOFT_LIMIT` | 已达到软阈值，仍可调用 |
| `EXCEEDED` | 达到硬上限，在发往供应商前拒绝 |
| `DISABLED` | 管理员停用，在发往供应商前拒绝 |
| `UNLIMITED` | 不阻断，但继续记录成本 |

模型列表返回顶层 `user_quota`，每个模型同时带 `user_quota_status` 和 `user_quota_remaining`。额度耗尽时可用模型标记为 `QUOTA_EXCEEDED`。

### 6.7 预占、结算与异常

- 调用前根据输入和最大输出量估算积分，以 `invocation_id` 幂等、原子预占。
- 成功后按供应商实际 Usage 结算，实际值可高于预估；超额会如实记账并阻断后续请求。
- 供应商调用失败释放预占，不写成功成本台账。
- 流式已经输出内容、或供应商已完成但本地制品保存失败时，按实际用量或预估用量记账。
- 异步任务受理后保持预占，任务回调或轮询器完成后必须显式结算：

```python
await client.finalize_async_quota(
    invocation_id=invocation_id,
    usage=final_usage,
    succeeded=True,
)
```

## 7. 阿里百炼流式与非流式 Demo

`examples/aliyun_bailian_quota_demo.py` 演示：

1. 管理员注册阿里百炼，Base URL 为 `https://dashscope.aliyuncs.com/compatible-mode/v1`，Key 使用 `sk-xxxx` 占位。
2. 租户管理员配置 qwen-plus 积分换算规则，为 `user_001` 设置月度 1,000 积分。
3. 获取该用户可用的文本生成模型列表，选择第一个并保存为租户默认模型。
4. 子用户省略 `model`，以非流式方式请求“你好，请问你可以做哪些事情”。
5. 再以流式方式请求并逐个消费 `output.delta`。
6. 查询该子用户的累计模型成本。

```bash
export DASHSCOPE_API_KEY='sk-实际百炼Key'
uv run python examples/aliyun_bailian_quota_demo.py
```

示例不会输出真实 Key。阿里百炼兼容端点不提供 `GET /models`，因此使用内置 YAML 清单声明 `qwen-plus`。

## 8. FastAPI 接口

核心接口：

- `POST /api/v1/model-registrations`
- `POST /api/v1/credential-model-registrations`
- `GET /api/v1/models`
- `POST /api/v1/model-invocations`
- `GET /api/v1/model-defaults`
- `PUT /api/v1/model-defaults/{model_type}`

子用户配额管理接口：

- `PUT /api/v1/user-quotas/model-rates`
- `PUT /api/v1/user-quotas/templates`
- `PUT /api/v1/user-quotas/role-bindings`
- `PUT /api/v1/user-quotas/users`
- `GET /api/v1/user-quotas/users/{user_id}`
- `GET /api/v1/user-costs`
- `GET /api/v1/user-cost-summary`

启动示例：

```bash
uv run uvicorn examples.fastapi_app:app --reload
```

生产环境必须替换示例 Header 鉴权，注入平台 JWT 或服务身份解析器。

## 9. 数据库与迁移

- SQLite：开发和测试可使用 `ModelRepositoryClient.sqlite()` 自动建表。
- PostgreSQL：依次执行 `migrations/postgresql/001_initial.sql` 到 `005_child_user_quota.sql`。
- v0.3 运行时不再读取旧 `provider_quota`、`provider_preference` 和 `quota_reservation`。升级时先停止 v0.2 实例，再部署 v0.3；确认无回滚需求后可另行删除旧表。
- `user_quota_reservation` 是在途额度事实源，`user_cost_ledger` 是已结算成本事实源。
- 财务金额不使用二进制 Float；积分字段统一为 `NUMERIC(20,6)` / `Decimal`。

## 10. 项目结构

```text
src/model_access/
├── contracts/          # 稳定协议、请求与响应
├── adapters/           # 供应商 Adapter、注册表、模型清单
├── persistence/        # SQLAlchemy 表和 Repository
├── control_plane.py    # 注册、列表、权限和默认模型
├── runtime.py          # 调用、重试、流式、异步和结算
├── user_quota.py       # 子用户预算、预占结算和成本台账
├── routing.py          # 自部署多端点路由
├── observability.py    # OpenTelemetry
├── security.py         # 凭据加密、脱敏和 URL 策略
├── api.py              # FastAPI 适配层
└── client.py           # 稳定 Facade
```

## 11. 验证

```bash
uv run ruff check src tests examples
uv run pytest
uv build
```

更详细的子用户配额设计见 `docs/quota_management.md`，接口示例见 `docs/api_examples.md`。
