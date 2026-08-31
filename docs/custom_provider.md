# 新增供应商 Adapter

新增供应商只需要实现 `ProviderAdapter`：

1. `descriptor`：声明供应商、模型类型、凭据表单和能力。
2. `validate_credentials`：执行最小鉴权请求，返回标准验证结果。
3. `discover_models`：把静态清单或远端发现结果转成 `ModelDescriptor`。
4. `invoke`：根据 operation 把标准输入转换成原生协议，并返回：
   - 阻塞：`AdapterResponse`
   - 流式：`AsyncIterator[AdapterChunk]`
   - 异步：`AdapterAsyncTask`

原始异常必须转换为 `ModelAccessException`。只有 `RATE_LIMITED`、`PROVIDER_TIMEOUT` 和 `PROVIDER_UNAVAILABLE` 等临时错误可设置 `retryable=True`。凭据错误、参数错误、模型不存在和权限错误不得自动重试。

## 契约检查清单

- 不打印或序列化 `credential_values`。
- 不在 Adapter 中选择跨模型降级策略或修改租户配额。
- 流式结果有序，最后一个 Chunk 尽量包含 usage 和 finish_reason。
- 供应商专用参数只读取 `invocation.provider_options`，不得覆盖 model、credential、tenant 或 trace。
- 大文件返回到 `AdapterArtifact`，由平台 ArtifactStore 生成受控 URI。
- 异步供应商任务只返回 provider_task_id；平台 task_id 由 TaskBackend 创建。
- 不根据模型名称猜测视觉、工具、结构化输出或异步能力。

可从 `examples/custom_adapter.py` 开始实现，并复用 `tests/test_openai_adapter.py` 的契约测试组织方式。

