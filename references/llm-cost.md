# LLM 成本治理

> Token 失控是 LLM 平台最大的财务风险。本文给出**租户配额 + 复杂度路由 + 缓存 + 告警**四道闸门。

---

## 1. 四道闸门总览

```
用户请求
  ├─ 闸门1: 租户配额检查（Redis 令牌桶）           ← 控制"谁能用多少"
  ├─ 闸门2: 复杂度路由（小模型优先 / 大模型兜底）    ← 控制"每次花多少"
  ├─ 闸门3: 语义缓存（Redis 为主 + 内存 LRU 兜底）  ← 控制"重复调用"
  └─ 进入 LLM 调用（带 prometheus 指标 + 限流）      ← 控制"实时观察"
```

---

## 2. 闸门 1：租户配额（Redis 令牌桶）

### 2.1 配置结构（pydantic-settings）

```python
class TenantQuota(BaseModel):
    tenant_id: str
    monthly_token_limit: int       # 月度 token 上限
    rpm_limit: int                  # 每分钟请求数
    tpm_limit: int                  # 每分钟 token 数
    enabled_models: list[str]       # 白名单模型（防滥用 GPT-5 跑简单问答）
```

### 2.2 令牌桶实现

```python
# governance/quota.py
import redis.asyncio as redis_async

class TokenBucket:
    def __init__(self, redis_client: redis_async.Redis, key_prefix: str = "quota"):
        self.r = redis_client
        self.prefix = key_prefix

    async def acquire(self, tenant_id: str, estimated_tokens: int,
                      rpm: int, tpm: int) -> tuple[bool, str | None]:
        """返回 (allowed, reason)。Lua 脚本保证原子性。"""
        key_rpm = f"{self.prefix}:{tenant_id}:rpm"
        key_tpm = f"{self.prefix}:{tenant_id}:tpm"
        # Lua 脚本：检查 + 递增，超限回滚
        script = """
        local rpm_curr = tonumber(redis.call('GET', KEYS[1]) or '0')
        local tpm_curr = tonumber(redis.call('GET', KEYS[2]) or '0')
        if rpm_curr + 1 > tonumber(ARGV[1]) then return 0 end
        if tpm_curr + tonumber(ARGV[2]) > tonumber(ARGV[3]) then return 0 end
        redis.call('INCR', KEYS[1]); redis.call('EXPIRE', KEYS[1], 60)
        redis.call('INCRBY', KEYS[2], ARGV[2]); redis.call('EXPIRE', KEYS[2], 60)
        return 1
        """
        allowed = await self.r.eval(
            script, 2, key_rpm, key_tpm, rpm, estimated_tokens, tpm
        )
        return bool(allowed), None if allowed else "rate_limited"
```

### 2.3 月度配额检查（异步任务）

`tools/workers/tasks.py` 每日执行：

```python
async def check_monthly_quota(ctx):
    """每日扫一次超限租户，发送告警 / 自动降级。"""
    usage = await db.fetch("""
        SELECT tenant_id, sum(prompt_tokens + completion_tokens) as used
        FROM llm_call_log WHERE created_at > date_trunc('month', now())
        GROUP BY tenant_id
    """)
    for row in usage:
        quota = await get_tenant_quota(row['tenant_id'])
        ratio = row['used'] / quota.monthly_token_limit
        if ratio > 0.95:
            await disable_tenant(row['tenant_id'])
            await alert(f"租户 {row['tenant_id']} token 用量超限，已禁用")
        elif ratio > 0.80:
            await alert(f"租户 {row['tenant_id']} token 用量达 {ratio:.0%}")
```

---

## 3. 闸门 2：按 query 复杂度路由

### 3.1 三层模型选择

| 复杂度 | 模型 | 占比（经验） | 单价量级（**输入**，每 1M token） |
|---|---|---|---|
| 简单（意图识别 / 关键词兜底 / 短答案） | 小模型（Haiku / GPT-4o-mini） | 70-80% | ~$0.25 |
| 中等（RAG 问答 / 总结） | 中模型（Sonnet / GPT-4o） | 15-25% | ~$3 |
| 复杂（多步推理 / 长文生成） | 大模型（Opus / GPT-5） | 5% | ~$15 |

> ⚠️ 上表是**量级估算**（输入 token 口径），用来说明"按复杂度路由能省钱"这个结论，
> **不是报价**——数字随厂商调价变动，登记于 `FACTS.md` F-018。
> 算真实预算、定 §2 的配额阈值时，请以厂商 pricing 页为准反推，**不要照抄本表**。

### 3.2 路由器实现

```python
# agents/router/model_router.py
from enum import Enum

class ModelTier(Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"

class ModelRouter:
    def __init__(self, llm_registry: dict[ModelTier, BaseChatModel]):
        self.registry = llm_registry

    async def route(self, query: str, state: AgentState) -> ModelTier:
        """根据 query 长度 + 复杂度信号选择模型。"""
        # 规则 1：长度 < 100 字且无代码块 → SMALL
        if len(query) < 100 and "```" not in query and "分析" not in query:
            return ModelTier.SMALL
        # 规则 2：检测到多步意图词（"先...然后..."、"对比..."、"规划"）→ LARGE
        if any(kw in query for kw in ["对比", "规划", "分析", "推导", "step by step"]):
            return ModelTier.LARGE
        # 规则 3：默认 MEDIUM
        return ModelTier.MEDIUM

    def get(self, tier: ModelTier) -> BaseChatModel:
        return self.registry[tier]
```

### 3.3 强制路由（防绕过）

`execute_agent()` 中**先**路由再调 LLM：

```python
async def execute_agent(user_query, graph, ...):
    tier = await model_router.route(user_query, state)
    llm = model_router.get(tier)
    state["model_tier"] = tier.value   # 记录用于 trace 与计费
    # ... 后续用 llm 而不是 settings.llm
```

**禁止**让 Agent 自己选择模型——会被 prompt injection 操纵。

---

## 4. 闸门 3：语义缓存（Redis 主存储）

`patterns.md` §13 已给出 `CachingChatOpenAI` 模式。这里补充**多租户隔离**与**命中率指标**。

### 4.1 多租户隔离的缓存键

```python
def cache_key(messages: list[BaseMessage], tenant_id: str) -> str:
    # 必须把 tenant_id 纳入哈希，否则租户 A 命中租户 B 的回答（数据泄露）
    payload = {
        "tenant_id": tenant_id,
        "messages": [{"role": m.type, "content": m.content} for m in messages],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return f"llm:cache:{tenant_id}:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"
```

### 4.2 命中率指标（Prometheus）

```python
LLM_CACHE = Counter(
    "llm_cache_total",
    "LLM cache hits/misses",
    ["tenant_id", "result"],   # result: hit | miss
)

# 在 CachingChatOpenAI._generate 中：
if cached := await redis.get(cache_key):
    LLM_CACHE.labels(tenant_id=tenant_id, result="hit").inc()
    return cached
LLM_CACHE.labels(tenant_id=tenant_id, result="miss").inc()
```

### 4.3 缓存失效策略

| 触发 | 失效 |
|---|---|
| Prompt 版本变更（Registry 升级） | 该 prompt 的所有缓存 key |
| 模型升级 | 该模型的所有缓存 key |
| 租户数据更新（业务侧） | 业务方调用 `cache.invalidate_pattern(f"llm:cache:{tenant_id}:*")` |

**禁止**用 TTL 替代显式失效——会让"刚更新完策略"的用户看到旧答案。

---

## 5. 闸门 4：实时指标 + 告警

### 5.1 必接的 4 个指标

```python
LLM_CALLS = Counter(
    "llm_calls_total",
    "LLM API call count",
    ["tenant_id", "model", "status"],   # status: success | error | timeout | circuit_open
)
LLM_TOKENS = Counter(
    "llm_tokens_total",
    "Token usage",
    ["tenant_id", "model", "direction"],  # direction: prompt | completion
)
LLM_COST = Counter(
    "llm_cost_usd_total",
    "Estimated USD cost",
    ["tenant_id", "model"],
)
LLM_LATENCY = Histogram(
    "llm_call_duration_seconds",
    "LLM call latency",
    ["model", "status"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
)
LLM_ERRORS = Counter(
    "llm_errors_total",
    "LLM call errors",
    ["tenant_id", "model", "error_type"],
)
```

### 5.2 告警规则

```yaml
# deploy/prometheus/rules/llm_cost.yml
groups:
  - name: llm_cost
    rules:
      # 1) 月度配额：用 recording rule 预先聚合，再除以 quota（来自 tenant_quota 表）
      - record: llm_tokens_monthly:tenant
        expr: sum by (tenant_id) (increase(llm_tokens_total[30d]))
      - alert: TenantTokenQuotaWarning
        expr: >
          (llm_tokens_monthly:tenant
           / on(tenant_id) tenant_quota:monthly_limit) > 0.8
        for: 1h
        annotations:
          summary: "租户 {{ $labels.tenant_id }} token 月用量达配额 80%"
      # 2) LLM 错误率：用 llm_calls_total（定义见 §5.1）
      - alert: LLMErrorRateHigh
        expr: >
          sum by (model) (rate(llm_errors_total[5m]))
          / sum by (model) (rate(llm_calls_total[5m])) > 0.05
        for: 5m
        annotations:
          summary: "模型 {{ $labels.model }} 错误率 >5%"
      - alert: CacheHitRateLow
        expr: >
          sum(rate(llm_cache_total{result="hit"}[1h]))
          / sum(rate(llm_cache_total[1h])) < 0.4
        for: 2h
        annotations:
          summary: "LLM 缓存命中率 <40%，应排查 prompt 漂移或缓存键设计"
```

> **关于 `llm_calls_total` 与 `agent_calls_total`**：前者统计"实际调用 LLM API 的次数"（用于错误率/限流），后者统计"Agent 节点被调用次数"（用于决策链监控）。两个指标分开，**不要混用**。
>
> `tenant_quota:monthly_limit` 也是 recording rule，需在 rules 中同步定义：
> ```yaml
> - record: tenant_quota:monthly_limit
>   expr: tenant_quota_tokens_monthly_limit   # 从 governance 表注入
> ```

---

## 6. 成本看板（Grafana）

详见 `references/grafana-dashboards.md` 中"LLM 成本"面板组。

关键 panel：
1. **Token 用量趋势**（按租户 + 模型分线）
2. **月度配额利用率排行**（TOP 10 租户）
3. **缓存命中率热力图**（按小时 × 租户）
4. **每千次调用成本**（按模型分柱状图）
5. **路由器分布饼图**（small/medium/large 占比）

---

## 7. 供应商成本对比（季度评审）

每个季度评估一次"模型供应商价格 vs 性能"：

| 维度 | OpenAI | Anthropic | 国产替代 |
|---|---|---|---|
| 1M 输入 token | ~$3 | ~$3 | ~¥10 |
| 1M 输出 token | ~$15 | ~$15 | ~¥30 |
| 中文能力 | B | B+ | A |
| 长上下文（>100K） | A | A | B |
| 工具调用稳定性 | A | A | B |

> ⚠️ 上表数字是**参考量级**，不是报价（登记于 `FACTS.md` F-019，含核实日期）——
> 用之前先做本节的季度评审。等级列（中文能力 / 长上下文 / 工具调用稳定性）是主观判断，
> 会随模型迭代变化，评审时一并复核。

结论建议：**主供应商 + 备用供应商**双轨制（详见 `llm-routing.md`）。

---

## 8. 禁止事项

- ❌ **禁止**让 Agent 自己选择模型（防 prompt injection 操纵升级到贵模型）
- ❌ **禁止**用 TTL 替代显式缓存失效
- ❌ **禁止**把缓存键设计为不包含 tenant_id（数据泄露风险）
- ❌ **禁止**月度配额用进程内计数器（多副本会漂移，必须 Redis）
- ❌ **禁止**在没有限流的前提下开放新租户接入