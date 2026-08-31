# 模型配额管理

## 1. 状态与优先级

| preferred | SYSTEM 有有效额度 | CUSTOM 有有效凭据 | using | 模型状态 |
| --- | --- | --- | --- | --- |
| SYSTEM | 是 | 任意 | SYSTEM | ACTIVE |
| SYSTEM | 否 | 是 | CUSTOM | ACTIVE，`fallback_reason=SYSTEM_QUOTA_EXHAUSTED` |
| SYSTEM | 否 | 否 | 无 | QUOTA_EXCEEDED |
| CUSTOM | 任意 | 是 | CUSTOM | ACTIVE |
| CUSTOM | 任意 | 否 | 无 | NO_CONFIGURE |

SYSTEM 内部始终按 `PAID → FREE → TRIAL` 选择，并继续检查：

- `is_valid=true`；
- `quota_limit=-1` 或 `quota_used + quota_reserved < quota_limit`；
- `restrict_models` 为空，或包含当前模型 ID。

偏好 CUSTOM 时不会自动反向使用 SYSTEM，以避免产生租户未选择的平台成本。

## 2. 数据与一致性

- `provider_quota` 是租户配额事实源，保存上限、已用、已预占、有效标志和乐观版本。
- `quota_reservation` 以 `invocation_id` 为主键，保证预占/结算幂等。
- 预占使用带容量条件的原子 SQL UPDATE，不依赖进程锁；多实例不会因为本地缓存而超卖。
- 成功调用把预占转换为实际用量；失败调用释放预占；达到上限后设置 `is_valid=false`。
- `configuration_source_version` 在凭据、偏好、额度和用量变化时推进版本。
- 缓存键包含配置源版本。旧 Redis 值无需主动删除，TTL 到期后回收；版本不匹配时重新查询数据库装配。
- `model_access_outbox` 与关键写操作同事务落库，供宿主平台发布失效/审计事件。

## 3. 部署配置

生产环境推荐：

```python
from redis.asyncio import Redis
from model_access import RedisConfigurationSourceCache

redis = Redis.from_url(redis_url)
cache = RedisConfigurationSourceCache(redis)
client = ModelRepositoryClient(
    repository=repository,
    cipher=cipher,
    hosting=HostingConfiguration.from_yaml("config/hosting.yaml"),
    configuration_cache=cache,
)
```

Redis 不是配额事实源；Redis 故障时可以切换为短 TTL 缓存或直接装配，但配额预占和结算必须继续走共享数据库。

## 4. 运维边界

- 全局 hosting 配置只负责新租户池的初始规格；已实例化租户池应通过管理面显式变更，避免环境变量变化静默重置已用量。
- 配额与模型凭据按 tenant/provider 归属检查，SYSTEM 凭据只有 `system_admin` 可注册。
- Outbox 的投递 Worker、过期预占清理任务和财务计费不在三个模型业务接口内，应由宿主控制面运行。
