# LLM 路由与多供应商容灾

> LLM 平台不能依赖单一供应商。本文给出主备降级、按复杂度路由、多供应商负载均衡、Circuit Breaker 完整方案。

---

## 1. 三种路由模式

| 模式 | 适用 | 实现成本 |
|---|---|---|
| **主备（Primary-Replica）** | 关键业务，要求 99.9%+ 可用 | 低 |
| **按复杂度分层** | 成本敏感，质量差异容忍度大 | 中 |
| **多供应商负载均衡** | 多供应商合规 / 容量扩展 | 高 |

本文档前两节已涉及按复杂度路由（见 `llm-cost.md` §3），本文档聚焦**主备 + 负载均衡 + 熔断**。

---

## 2. 主备架构（Primary-Replica with Circuit Breaker）

### 2.1 架构图

```
请求 → Router
         │
         ├─ Primary (OpenAI GPT-4o)   ← 正常情况 100%
         │
         ├─ Replica 1 (Anthropic Claude)  ← Primary 故障时启用
         │
         └─ Replica 2 (国产大模型)         ← Replica 1 也故障时启用
```

### 2.2 路由实现

```python
# governance/llm/router.py
from enum import Enum

class ProviderStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OPEN = "open"   # circuit breaker 触发

class LLMRouter:
    def __init__(self, providers: list[LLMProvider]):
        self.providers = providers   # 按优先级排序
        self.cb_states: dict[str, CircuitBreaker] = {
            p.name: CircuitBreaker(...) for p in providers
        }

    async def route(self, request: ChatRequest, tier: ModelTier) -> LLMResponse:
        """按优先级尝试，直到成功。"""
        last_error = None
        for provider in self.providers:
            cb = self.cb_states[provider.name]
            if not cb.allow_request():
                continue   # 熔断中，跳过
            try:
                response = await provider.complete(
                    request, model_for_tier=tier
                )
                cb.record_success()
                return response
            except Exception as e:
                cb.record_failure()
                last_error = e
                logger.warning(
                    "LLM provider failed, trying next",
                    extra={"provider": provider.name, "error": str(e)},
                )
        raise AllProvidersFailedError(last_error)
```

### 2.3 Circuit Breaker 配置

```python
# governance/llm/circuit_breaker.py
@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5           # 连续失败 5 次触发熔断
    recovery_timeout: float = 30.0      # 熔断后 30s 尝试恢复（半开）
    half_open_success_threshold: int = 2  # 半开状态连续 2 次成功才完全恢复
    rolling_window_seconds: int = 60     # 统计窗口

class CircuitBreaker:
    def __init__(self, name: str, cfg: CircuitBreakerConfig):
        self.name = name
        self.cfg = cfg
        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_at = None

    def allow_request(self) -> bool:
        if self.state == CircuitBreakerState.CLOSED:
            return True
        if self.state == CircuitBreakerState.OPEN:
            # 检查是否到达恢复时间
            if (time.time() - self.last_failure_at) > self.cfg.recovery_timeout:
                self.state = CircuitBreakerState.HALF_OPEN
                self.success_count = 0
                return True
            return False
        if self.state == CircuitBreakerState.HALF_OPEN:
            return True

    def record_success(self):
        if self.state == CircuitBreakerState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.cfg.half_open_success_threshold:
                self.state = CircuitBreakerState.CLOSED
                self.failure_count = 0
        elif self.state == CircuitBreakerState.CLOSED:
            self.failure_count = 0

    def record_failure(self):
        self.last_failure_at = time.time()
        self.failure_count += 1
        if self.failure_count >= self.cfg.failure_threshold:
            self.state = CircuitBreakerState.OPEN
            logger.error(f"Circuit breaker opened: {self.name}")
```

### 2.4 切换时的语义差异处理

主备切换最大的坑是**输出格式不一致**：

```python
class ProviderAdapter:
    """统一不同供应商的输入输出。"""

    async def complete(self, request: ChatRequest, model_for_tier: ModelTier) -> LLMResponse:
        if self.name == "openai":
            return await self._openai_complete(request, model_for_tier)
        elif self.name == "anthropic":
            # ★ Anthropic 与 OpenAI 的 system prompt 处理不同
            return await self._anthropic_complete(request, model_for_tier)
        elif self.name == "domestic":
            # ★ 国产模型可能不支持 tool_use / function_call
            return await self._domestic_complete(request, model_for_tier)
```

#### 关键差异清单

| 差异 | OpenAI | Anthropic | 国产 |
|---|---|---|---|
| System prompt | 第一条 system message | 独立 `system` 参数 | 视厂商而定 |
| Tool use | `tool_choice` 参数 | `tools` 数组 | 部分支持 |
| JSON 模式 | `response_format={"type":"json_object"}` | 需 prompt 强制 | 部分支持 |
| 多模态 | content 数组 | content 数组 | 视厂商而定 |
| 流式 | SSE | SSE | 视厂商而定 |

**统一抽象层必须把这些差异全部屏蔽**，否则切换供应商会破坏 Agent 决策链。

### 2.5 切换时的 Prompt 兼容性

主备切换前必须验证：

```python
# governance/llm/compatibility_check.py
async def verify_prompt_compatibility(prompt_name: str, target_provider: str):
    """验证某 prompt 在目标供应商上仍能正常工作。"""
    cases = load_golden_cases(prompt_name)
    failures = []
    for case in cases:
        result = await run_with_provider(prompt_name, case, target_provider)
        if not meets_quality_gate(result, case):
            failures.append({"case": case["id"], "reason": ...})
    if failures:
        raise IncompatibleProviderError(
            f"{target_provider} 不兼容 prompt {prompt_name}，{len(failures)} 用例失败"
        )
```

### 2.6 切换时的告警

```yaml
# deploy/prometheus/rules/llm_routing.yml
- alert: LLMProviderCircuitOpen
  # ⚠️ 别写成 `llm_circuit_breaker_state{state="open"} == 1`——这一行里两处都错，
  #    而且错得**没有任何报错**，告警只是永远不响：
  #    (1) 该指标的 label 只有 provider（定义见 §7），**状态是值不是标签**。
  #        `{state="open"}` 选中的是不存在的序列 → 表达式恒为空集 → 永不触发。
  #        Prometheus 不会因此报错，"无数据"与"一切正常"在告警列表里长得一样。
  #    (2) 取值是 0=closed / 1=half_open / 2=open，`== 1` 命中的是**半开**而非熔断。
  expr: llm_circuit_breaker_state == 2
  for: 1m
  annotations:
    summary: "LLM 供应商 {{ $labels.provider }} 已熔断，流量切到备选"

- alert: LLMAllProvidersFailing
  expr: >
    sum(rate(llm_errors_total{error_type="all_providers_failed"}[5m])) > 0
  annotations:
    summary: "所有 LLM 供应商均不可用！立即人工介入"
```

---

## 3. 多供应商负载均衡（合规场景）

### 3.1 场景

某些金融/政务场景要求**数据不出境**，或**必须使用国产模型**。可在租户级别强制：

```python
class TenantLLMConfig:
    primary_provider: str = "openai"
    fallback_providers: list[str] = ["anthropic"]
    force_domestic: bool = False           # 金融租户强制
    # ⚠️ 占位示例。`claude-sonnet` **不是有效的 model id**（真实 id 带版本号，
    #    如 `claude-sonnet-5`），它在这里只示意"填 Anthropic 的型号"——照抄去调 API 会失败。
    allowed_models: list[str] = ["gpt-4o", "claude-sonnet"]
```

### 3.2 权重负载均衡

```python
class WeightedLLMRouter:
    """按租户配置的权重分配流量到多个供应商。"""

    async def route(self, request: ChatRequest, tenant_cfg: TenantLLMConfig) -> LLMResponse:
        # 选择满足租户约束的供应商
        candidates = [p for p in self.providers if p.name in tenant_cfg.allowed_models]
        if tenant_cfg.force_domestic:
            candidates = [p for p in candidates if p.is_domestic]
        # 加权轮询
        provider = self.weighted_round_robin(candidates)
        return await provider.complete(request)
```

---

## 4. 模型降级策略（区别于供应商降级）

供应商健康但**模型太贵/太慢**时的降级：

```python
class ModelDegradationPolicy:
    """成本/延迟超阈值时降级到更便宜的模型。"""

    async def select_model(self, tier: ModelTier, current_metrics: Metrics) -> ModelTier:
        # 当前延迟已经超阈值 → 降级
        if current_metrics.latency_p95 > LATENCY_THRESHOLD:
            return self.degrade(tier)
        # 当前成本超阈值 → 降级
        if current_metrics.cost_per_request > COST_THRESHOLD:
            return self.degrade(tier)
        return tier

    def degrade(self, tier: ModelTier) -> ModelTier:
        return {
            ModelTier.LARGE: ModelTier.MEDIUM,
            ModelTier.MEDIUM: ModelTier.SMALL,
            ModelTier.SMALL: ModelTier.SMALL,   # 已到顶
        }[tier]
```

---

## 5. 健康检查

每个供应商必须有**主动健康检查**（不依赖真实流量）：

```python
# governance/llm/health_check.py
class ProviderHealthCheck:
    async def check(self, provider: LLMProvider):
        try:
            await asyncio.wait_for(
                provider.complete(ChatRequest(messages=[
                    HumanMessage(content="ping"),
                ])),
                timeout=5.0,
            )
            return True
        except Exception as e:
            return False

# Worker 定期跑
async def health_check_loop(self):
    while True:
        for provider in self.providers:
            healthy = await self.health_check.check(provider)
            self.cb_states[provider.name].update_health(healthy)
        await asyncio.sleep(30)
```

主动检查 vs 被动熔断：
- **被动熔断**：等真实流量失败才熔断（响应慢，但准确）
- **主动检查**：定时 ping（提前发现问题，但有额外成本）

**建议**：两者并用，主动检查发现问题立即标 `DEGRADED`，被动失败达阈值触发 `OPEN`。

---

## 6. 配置示例（settings.py）

```python
class Settings(BaseSettings):
    # LLM 路由
    llm_primary_provider: str = "openai"
    llm_replica_providers: list[str] = ["anthropic"]
    llm_force_domestic: bool = False

    # 模型路由（按复杂度）
    # ⚠️ 下面是**占位示例**，不是推荐型号：model id 随厂商发版与下线上线变动，
    #    且各型号单价需自行核价（见 FACTS.md F-020 / F-018）。
    #    上线前替换为当前可用、且已确认价格的型号。
    llm_small_model: str = "gpt-4o-mini"
    llm_medium_model: str = "gpt-4o"
    llm_large_model: str = "gpt-5"

    # Circuit Breaker
    llm_cb_failure_threshold: int = 5
    llm_cb_recovery_timeout_sec: int = 30
    llm_cb_half_open_success_threshold: int = 2

    # 健康检查
    llm_health_check_interval_sec: int = 30
    llm_health_check_timeout_sec: int = 5

    # 模型降级
    llm_latency_degrade_threshold_sec: float = 5.0
    llm_cost_degrade_threshold_per_req_usd: float = 0.05
```

---

## 7. 监控指标

```python
LLM_ROUTING = Counter(
    "llm_routing_total",
    "LLM routing decisions",
    ["primary_provider", "fallback_provider", "reason"],  # reason: primary_failed|degraded|weighted
)
LLM_PROVIDER_HEALTH = Gauge(
    "llm_provider_healthy",
    "Provider health (1=healthy, 0=down)",
    ["provider"],
)
LLM_CIRCUIT_BREAKER_STATE = Gauge(
    "llm_circuit_breaker_state",
    "CB state (0=closed, 1=half_open, 2=open)",
    ["provider"],
)
```

Grafana 面板见 `grafana-dashboards.md` §2.8（LLM Routing & Failover）——本节的三个指标
（`llm_routing_total` / `llm_provider_healthy` / `llm_circuit_breaker_state`）在那里全部有面板；
告警规则文件是 `deploy/prometheus/rules/llm_routing.yml`（见该文 §3 的目录树）。

---

## 8. 灾备演练

每季度必须演练一次主备切换：

```bash
# 演练剧本
1. 把 primary 标记为 OPEN（手动触发熔断）
2. 观察 replica 是否正常承接流量
3. 验证 replica 的 prompt 兼容性（回归 golden 用例）
4. 检查 trace / metrics 是否完整
5. 恢复 primary 状态
6. 出具演练报告
```

演练不通过 → 修复 Prompt 兼容性问题或供应商适配层。

---

## 9. 禁止事项

- ❌ **禁止**只配一个供应商（单点故障）
- ❌ **禁止**Prompt 不做跨供应商兼容性测试就启用备选
- ❌ **禁止**熔断器不做主动健康检查（响应太慢）
- ❌ **禁止**切换供应商时不同步切换 Prompt Registry 状态
- ❌ **禁止**模型降级策略无回退机制（降级后再无路可走）