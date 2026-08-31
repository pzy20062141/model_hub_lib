# 大模型统一接入 Python Lib

面向智能体、工作流、知识库和应用服务的多租户模型接入库。业务模块只依赖 `ModelRepositoryClient`，不直接依赖供应商 SDK、API Key 或供应商返回结构。

当前版本实现设计文档 v1.4 的三个核心接口：

1. `register_model` / `POST /api/v1/model-registrations`：凭据验证、加密入库、模型发现与配置。
2. `list_models` / `GET /api/v1/models`：按租户、用户、分类、模型类型和状态返回可用模型。
3. `invoke` / `POST /api/v1/model-invocations`：统一调用文本、视觉理解、向量化、重排序、语音、图片、视频和审核模型。

## 1. 核心特性

- 控制面、运行面和供应商 Adapter 解耦；新增供应商不修改业务调用协议。
- USER / TENANT / SYSTEM 三种模型可见范围，服务端校验权威租户和用户身份。
- API Key 使用 Fernet 认证加密；响应、异常和 OpenTelemetry 默认不记录明文凭据或 Prompt 正文。
- `operation + input` 判别联合，Pydantic 严格拒绝未知核心字段和模型类型不匹配。
- 阻塞、SSE 流式、异步任务三种响应模式。
- `session_id -> query_id -> invocation_id -> provider attempts` 标识链路和用量幂等。
- OpenAI / OpenAI-compatible Adapter，支持供应商 YAML 模型清单。
- 自部署模型支持本地、私有云和 Kubernetes Service；同一模型的多个端点使用平滑加权轮询和失败冷却。
- SQLAlchemy 持久化；开发环境支持 SQLite，生产环境配置 PostgreSQL / MySQL，Redis 作为可选缓存事实之外的加速层。
- Dify 风格托管额度：PAID → FREE → TRIAL 配额池优先级、SYSTEM → CUSTOM 自动降级、调用级原子预占与成功后结算。
- `preferred_provider_type` 与 `using_provider_type` 分离；模型列表动态返回 `ACTIVE`、`QUOTA_EXCEEDED` 或 `NO_CONFIGURE`。
- OpenTelemetry Trace / Metric 统一映射，默认只采集低敏感元数据。

## 2. 包结构

```text
src/model_access/
├── contracts/          # 稳定协议、领域对象、请求与响应
├── adapters/           # 供应商 Adapter、注册表、YAML 模型清单
├── persistence/        # SQLAlchemy 表、Repository、多数据库配置
├── control_plane.py    # 注册、发现、列表、权限和幂等
├── runtime.py          # 解析、调用、重试、流式、异步和用量结算
├── quota.py            # 托管规格、租户配额池、优先级仲裁、降级与版本化缓存
├── routing.py          # 自部署多端点路由与失败冷却
├── observability.py    # OpenTelemetry 属性和指标封装
├── security.py         # 凭据加密、脱敏和 URL/SSRF 策略
├── api.py              # 可选 FastAPI 三接口
└── client.py           # 供其他模块加载的稳定 Facade
```

## 3. 支持的统一 operation

| operation | model_type | 输入 | 输出 | 模式 |
| --- | --- | --- | --- | --- |
| `chat` / `text_completion` | `text_generation` | messages / prompt，可含图像和音频 Part | message / text / tool_calls | blocking / streaming |
| `embeddings` | `embedding` | texts、input_type、dimensions | vectors | blocking |
| `rerank` | `rerank` | query、documents、top_n | ranked_documents | blocking |
| `transcribe` | `speech_to_text` | file_id、language | text、segments | blocking / async |
| `synthesize` | `text_to_speech` | text、voice、format、speed | artifact / audio delta | blocking / streaming / async |
| `image_generate` | `image_generation` | prompt、size、count、参考文件 | artifacts | blocking / async |
| `video_generate` | `video_generation` | prompt、参考图、duration、resolution | task_id | async |
| `moderate` | `moderation` | input、policy | labels、scores、blocked | blocking |

视觉理解不是重复的模型类型：仍使用 `text_generation + chat`，通过 `input_modalities` 包含 `image` 和 `VISION_MODEL` 分类表达能力。

## 4. 安装

推荐 Python 3.11 或 3.12。

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

生产数据库按需安装：

```bash
uv sync --extra postgres --extra api --extra otel
```

生成稳定加密主密钥并注入密钥系统：

```bash
uv run python -c "from model_access import FernetCredentialCipher; print(FernetCredentialCipher.generate_key())"
```

不要在每次进程启动时生成新密钥，否则已保存凭据无法解密。生产环境应从 KMS / Vault 或密钥注入机制提供 `MODEL_ACCESS_MASTER_KEY`。

## 5. 作为 Lib 直接加载

下面使用内置 Mock Adapter 完成完整闭环；真实供应商配置见 `examples/fastapi_app.py`。

```python
import os
from model_access import ModelRepositoryClient
from model_access.adapters import MockProviderAdapter

client = ModelRepositoryClient.sqlite(
    "model_access.db",
    encryption_key=os.environ["MODEL_ACCESS_MASTER_KEY"],
)
client.register_adapter(MockProviderAdapter(provider_descriptor, models))

registration = await client.register_model(
    registration_request,
    identity=authenticated_identity,
    idempotency_key="register-20260829-001",
)
models = await client.list_models(list_query, identity=authenticated_identity)
result_or_stream = await client.invoke(invocation, identity=authenticated_identity)
```

实际代码请直接参考可运行的 `examples/basic_usage.py`。初始化时应把环境变量中的完整 Fernet Key 字符串传入：

```python
import os
from model_access import ModelRepositoryClient

client = ModelRepositoryClient.sqlite(
    "model_access.db",
    encryption_key=os.environ["MODEL_ACCESS_MASTER_KEY"],
)
```

## 6. 给租户设置和使用模型配额

### 6.1 配额生效的前置条件

配额只约束使用平台托管凭据的 `SYSTEM` 模型。完整顺序如下：

1. 为租户注册 `SYSTEM` 范围的供应商凭据和模型；该操作要求身份包含 `system_admin`。
2. 为同一 `tenant + provider` 创建一个或多个 PAID、FREE、TRIAL 配额池。
3. 将租户的 `preferred_provider_type` 设置为 `SYSTEM`。
4. 业务仍使用统一的 `list_models` 和 `invoke`；运行时自动选择配额池、预占和结算。

```python
from model_access.contracts.entities import CallerIdentity, CredentialInput, ProviderRef
from model_access.contracts.enums import CredentialScope
from model_access.contracts.invocation import ModelRegistrationRequest

tenant_id = "tenant_001"
provider_ref = ProviderRef(plugin_id="builtin/openai", provider_id="openai")
system_admin = CallerIdentity(
    tenant_id=tenant_id,
    user_id="platform_admin",
    roles={"system_admin"},
)

# 供应商 Adapter 需要先通过 client.register_adapter(...) 注册。
system_registration = await client.register_model(
    ModelRegistrationRequest(
        tenant_id=tenant_id,
        user_id="platform_admin",
        provider=provider_ref,
        credential=CredentialInput(
            name="OpenAI 平台托管凭据",
            base_url="https://api.openai.com/v1",
            api_key=os.environ["OPENAI_SYSTEM_API_KEY"],
            scope=CredentialScope.SYSTEM,
        ),
    ),
    identity=system_admin,
    idempotency_key="tenant-001-openai-system-v1",
)
```

每个租户的 SYSTEM 模型和配额记录相互隔离。`HostingConfiguration` 只定义初始额度规格，不会自动创建供应商凭据。

### 6.2 方式一：通过全局 Hosting 配置初始化试用额度

适合云平台为新租户统一赠送试用额度。复制并修改 `config/hosting.example.yaml`：

```yaml
edition: CLOUD

quotas:
  - provider:
      plugin_id: builtin/openai
      provider_id: openai
    quota_type: TRIAL
    quota_unit: TOKENS
    quota_limit: 100000
    restrict_models:
      - gpt-4.1-mini
```

启动时加载配置：

```python
from model_access import HostingConfiguration, ModelRepositoryClient

hosting = HostingConfiguration.from_yaml("config/hosting.yaml")
client = ModelRepositoryClient.sqlite(
    "model_access.db",
    encryption_key=os.environ["MODEL_ACCESS_MASTER_KEY"],
    hosting=hosting,
)
```

只有 `edition: CLOUD` 生效。租户第一次执行 `list_models` 或 `invoke` 时，会按 provider 懒创建配额池。已经实例化的租户配额不会因为 YAML 上限变化而被静默覆盖，应使用下一节的管理方法显式调整。

### 6.3 方式二：管理员直接为指定租户设置额度

`tenant_admin`、`model_admin` 或 `system_admin` 可以管理自己所在租户的配额。下面给租户增加一个按调用次数扣减的 TRIAL 池和一个按 Token 扣减的 PAID 池：

```python
from model_access.contracts.entities import CallerIdentity
from model_access.contracts.enums import ProviderQuotaType, ProviderType, QuotaUnit
from model_access.contracts.quota import ProviderQuotaPoolInput

tenant_admin = CallerIdentity(
    tenant_id=tenant_id,
    user_id="tenant_admin_01",
    roles={"tenant_admin"},
)

trial_pool = client.configure_quota_pool(
    ProviderQuotaPoolInput(
        tenant_id=tenant_id,
        provider=provider_ref,
        quota_type=ProviderQuotaType.TRIAL,
        quota_unit=QuotaUnit.TIMES,
        quota_limit=500,
        restrict_models={"gpt-4.1-mini"},
    ),
    identity=tenant_admin,
)

paid_pool = client.configure_quota_pool(
    ProviderQuotaPoolInput(
        tenant_id=tenant_id,
        provider=provider_ref,
        quota_type=ProviderQuotaType.PAID,
        quota_unit=QuotaUnit.TOKENS,
        quota_limit=1_000_000,
        restrict_models={"gpt-4.1-mini", "gpt-4.1"},
    ),
    identity=tenant_admin,
)

client.set_provider_preference(
    tenant_id=tenant_id,
    provider=provider_ref,
    preferred_provider_type=ProviderType.SYSTEM,
    identity=tenant_admin,
)
```

配额池字段说明：

| 字段 | 含义 |
| --- | --- |
| `quota_type` | `PAID`、`FREE` 或 `TRIAL`；调用时固定按 PAID → FREE → TRIAL 选择 |
| `quota_unit` | `TIMES` 表示成功调用次数；`TOKENS` 表示供应商返回的实际 `total_tokens` |
| `quota_limit` | 配额上限；`-1` 表示无限额度 |
| `quota_used` | 可选；只建议在首次导入或管理员校正时显式传入，更新时省略会保留已有用量 |
| `restrict_models` | 可使用该池的模型 ID；空集合表示该 provider 的全部模型 |
| `is_valid` | 是否允许继续使用；设为 `False` 可立即停用该池 |
| `quota_id` | 可选；更新已有池时传原 ID。同类型需要多条独立配额记录时应自行指定不同 ID |

### 6.4 查询、调整和停用配额

```python
# 查询租户在该 provider 下的所有池，返回值不包含任何凭据明文。
pools = client.list_quota_pools(
    tenant_id=tenant_id,
    provider=provider_ref,
    identity=tenant_admin,
)
for pool in pools:
    print(
        pool.quota_id,
        pool.quota_type,
        pool.quota_used,
        pool.quota_reserved,
        pool.quota_remaining,
        pool.is_valid,
    )

# 给已有 PAID 池扩容。省略 quota_used，历史已用量和在途预占都会保留。
paid_pool = client.configure_quota_pool(
    ProviderQuotaPoolInput(
        quota_id=paid_pool.quota_id,
        tenant_id=tenant_id,
        provider=provider_ref,
        quota_type=ProviderQuotaType.PAID,
        quota_unit=QuotaUnit.TOKENS,
        quota_limit=2_000_000,
        restrict_models={"gpt-4.1-mini", "gpt-4.1"},
    ),
    identity=tenant_admin,
)

# 停用 TRIAL 池。
client.configure_quota_pool(
    ProviderQuotaPoolInput(
        quota_id=trial_pool.quota_id,
        tenant_id=tenant_id,
        provider=provider_ref,
        quota_type=ProviderQuotaType.TRIAL,
        quota_unit=QuotaUnit.TIMES,
        quota_limit=trial_pool.quota_limit,
        restrict_models=trial_pool.restrict_models,
        is_valid=False,
    ),
    identity=tenant_admin,
)
```

若要恢复一个已耗尽或停用的池，应同时提高 `quota_limit` 或显式校正 `quota_used`，并设置 `is_valid=True`。不要在仍有调用执行时随意重置 `quota_used`。

### 6.5 业务调用和状态判断

业务侧不需要传配额参数，继续调用原有模型列表和模型调用接口：

```python
models = await client.list_models(list_query, identity=authenticated_identity)
for item in models.items:
    print(
        item.status,
        item.preferred_provider_type,
        item.using_provider_type,
        item.quota_type,
        item.quota_remaining,
        item.fallback_reason,
    )

result = await client.invoke(invocation, identity=authenticated_identity)
```

状态和降级规则：

| preferred | SYSTEM 有额度 | CUSTOM 有凭据 | using / status |
| --- | --- | --- | --- |
| SYSTEM | 是 | 任意 | SYSTEM / ACTIVE |
| SYSTEM | 否 | 是 | CUSTOM / ACTIVE，`fallback_reason=SYSTEM_QUOTA_EXHAUSTED` |
| SYSTEM | 否 | 否 | 无 / QUOTA_EXCEEDED |
| CUSTOM | 任意 | 是 | CUSTOM / ACTIVE |
| CUSTOM | 任意 | 否 | 无 / NO_CONFIGURE |

TIMES 配额在每次成功调用后扣 1。TOKENS 在调用前按输入和最大输出量保守预占，成功后按供应商实际 `total_tokens` 结算，失败则释放预占。`invocation_id` 是预占与结算的幂等键。

当前 FastAPI 仍只暴露模型注册、模型列表、模型调用三个业务接口。配额设置属于平台管理面，宿主系统应在受保护的管理员接口中调用 `configure_quota_pool`、`list_quota_pools` 和 `set_provider_preference`，不要把这些方法直接暴露给普通租户用户。

### 6.6 阿里百炼完整 Demo

`examples/aliyun_bailian_quota_demo.py` 演示以下完整流程：

1. 管理员用 `SYSTEM` 范围注册阿里百炼供应商，Base URL 为 `https://dashscope.aliyuncs.com/compatible-mode/v1`。
2. 为 `tenant_aliyun_demo` 配置 100,000 Token 的 TRIAL 租户额度，并将首选供应商类型设为 `SYSTEM`。
3. 普通用户获取当前配额可用的 `text_generation` 模型列表并选择第一个模型。
4. 先以非流式方式请求 `你好，请问你可以做哪些事情`，一次性读取完整响应。
5. 再以流式方式请求同一问题，逐个消费并实时输出 `output.delta` 文本事件。
6. 两种调用成功后都会按供应商返回的实际 Token 用量分别结算租户额度。

从项目根目录运行：

```bash
export DASHSCOPE_API_KEY='sk-实际百炼Key'
uv run python examples/aliyun_bailian_quota_demo.py
```

`sk-xxxx` 只作为占位符，示例不会硬编码或输出真实 Key。阿里百炼的 OpenAI 兼容端点不提供 `GET /models`，所以示例在注册时关闭远端模型发现，改用 `config/providers/aliyun-bailian.yaml` 显式声明 `qwen-plus`；如需增加模型，请先在该清单中声明其能力。

Demo 中两种请求方式分别封装为独立函数：

```python
# 非流式：response_mode=ResponseMode.BLOCKING，返回完整 InvocationResult。
blocking_answer = await invoke_blocking(
    client=client,
    configured_model_id=selected.configured_model_id,
    identity=tenant_user,
)

# 流式：response_mode=ResponseMode.STREAMING，返回异步 StreamEvent 序列。
streaming_answer = await invoke_streaming(
    client=client,
    configured_model_id=selected.configured_model_id,
    identity=tenant_user,
)
```

流式消费者会依次处理 `response.created`、`output.delta`、`usage`、`response.completed`；其中 `output.delta` 用于实时拼接回答，`usage` 用于展示最终用量。每次请求必须使用不同的 `query_id`，以保证调用追踪和配额结算可以独立识别。

示例为了便于直接运行使用内存 SQLite 和临时 Fernet 密钥。生产环境应改用持久化数据库及稳定的 `MODEL_ACCESS_MASTER_KEY`。当前配额模型按租户管理，同一租户内的普通用户共享该租户额度，不是每个 `user_id` 一份独立额度。

## 7. 注册 OpenAI 兼容供应商

供应商能力由 YAML 清单明确声明，不根据模型名称猜测。先修改 `config/providers/openai-compatible.yaml` 中的模型 ID 和能力：

```python
from model_access.adapters import OpenAICompatibleAdapter, load_provider_manifest

manifest = load_provider_manifest("config/providers/openai-compatible.yaml")
provider_descriptor, model_manifest = manifest.build()

client.register_adapter(
    OpenAICompatibleAdapter(
        descriptor=provider_descriptor,
        model_manifest=model_manifest,
    )
)
```

注册时 Adapter 调用 `GET {base_url}/models` 验证凭据，只启用远端返回且已在清单声明的模型。没有模型发现接口的自研服务可在注册请求中传 `model` 手工声明。

## 8. 可选 FastAPI 三接口

```python
from model_access.api import create_app

app = create_app(client, identity_resolver=platform_jwt_identity_resolver)
```

启动示例：

```bash
export MODEL_ACCESS_MASTER_KEY='...'
uv run uvicorn examples.fastapi_app:app --host 0.0.0.0 --port 8087
```

默认 `HeaderIdentityResolver` 只适合已由 API Gateway 完成认证的内部演示环境。生产环境必须注入平台 JWT / Service Token 解析器，并以解析结果作为 `tenant_id`、`user_id` 和角色的权威来源。

接口请求示例见 `docs/api_examples.md`；OpenAPI 文件为 `openapi/model_access-v1.1.json`。

## 9. 自部署模型

仍调用同一个注册接口，增加 `deployment`：

```json
{
  "provider": {"plugin_id": "builtin/self-hosted", "provider_id": "self_hosted"},
  "credential": {
    "name": "私有云推理集群",
    "base_url": "https://llm-gateway.internal/v1",
    "api_key": "token-value",
    "scope": "TENANT"
  },
  "deployment": {
    "deployment_mode": "KUBERNETES_SERVICE",
    "protocol": "OPENAI_COMPATIBLE",
    "model_name": "qwen-enterprise-32b",
    "endpoints": [
      {"endpoint_id": "az-a", "base_url": "https://llm-a.internal/v1", "weight": 100},
      {"endpoint_id": "az-b", "base_url": "https://llm-b.internal/v1", "weight": 50}
    ],
    "discovery_mode": "MANUAL"
  },
  "model": {
    "model": "qwen-enterprise-32b",
    "model_type": "text_generation",
    "input_modalities": ["text", "image"],
    "output_modalities": ["text"],
    "operations": ["chat"],
    "features": ["streaming", "tool_calling"]
  }
}
```

URL 安全策略默认阻止私网、Loopback、链路本地和云元数据地址。自部署场景需由管理员显式配置 `allowed_hosts` 或 `allowed_cidrs`，不要全局关闭 SSRF 防护。

## 10. 数据库与迁移

- SQLAlchemy 模型位于 `src/model_access/persistence/models.py`。
- PostgreSQL 初始迁移位于 `migrations/postgresql/001_initial.sql`，配额迁移为 `002_provider_quota.sql`。
- 本地 / 云集群配置示例位于 `config/databases.example.yaml`。
- `DatabaseRegistry` 按 `catalog / usage / audit / cache` 逻辑名称隔离配置和凭据引用。

SQLite 只用于开发和测试。生产环境应使用独立迁移步骤，不要让多个应用实例并发执行 `create_schema()`。

## 11. OpenTelemetry

运行面创建供应商 CLIENT Span，记录 `gen_ai.*` 与 `model_access.*` 的低敏感属性；不记录 API Key、Authorization、Prompt、文件正文或完整输出。Collector 示例在 `config/otel-collector.example.yaml`。

若要导出 OTLP：

```bash
uv sync --extra otel
```

然后在宿主应用初始化 OpenTelemetry SDK 和 OTLP exporter。本库只消费全局 Provider；Exporter 或 Collector 不可用时不会阻断模型调用。

## 12. 测试

```bash
uv run pytest
```

测试覆盖：严格契约、条件上下文、凭据脱敏与加密、注册幂等、权限、模型列表、阻塞/流式/异步调用、调用标识约束、OpenAI 兼容协议转换、三 HTTP 接口和自部署端点路由。

## 13. 生产接入前必须替换的组件

- `HeaderIdentityResolver` → 平台 JWT / Service Identity 解析器。
- `InMemoryArtifactStore` → BOS / S3 / 企业文件服务。
- `InMemoryTaskBackend` → 任务执行引擎，用于轮询、回调和任务结果落库。
- 单实例内存配置缓存 → `RedisConfigurationSourceCache`；数据库版本号和配额记录仍是事实源。
- SQLite → PostgreSQL / MySQL；Redis 只做缓存，不做事实源。
- Fernet 环境密钥 → KMS / Vault 托管的密钥版本与轮换实现。
- 默认 URL 规则 → 企业域名、CIDR、端口、DNS 重绑定和出口网络策略。

详细设计映射和扩展方法见 `docs/design_mapping.md` 与 `docs/custom_provider.md`。
