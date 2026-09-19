# Prompt 版本治理与 A/B 测试

> Prompt 是 Agent 时代的"代码"，变更频率远高于传统代码。本文档给出
> 版本号管理、A/B 分流、评测准入、线上指标回写完整闭环。

---

## 1. Prompt Registry 数据模型

```sql
CREATE TABLE prompt (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL DEFAULT '__shared__',
    name VARCHAR(128) NOT NULL,             -- e.g. "policy_qa.system"
    version INT NOT NULL,
    content TEXT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'draft',  -- draft|canary|active|retired
    model_target VARCHAR(64),               -- 适用模型（防止把 Claude prompt 用到 GPT）
    parent_version INT,                     -- 版本谱系
    created_at TIMESTAMPTZ DEFAULT NOW(),
    created_by VARCHAR(64),
    retired_at TIMESTAMPTZ,
    UNIQUE(tenant_id, name, version)
);

CREATE TABLE prompt_metric (
    id BIGSERIAL PRIMARY KEY,
    prompt_id BIGINT REFERENCES prompt(id),
    sample_size INT,
    avg_faithfulness FLOAT,
    avg_relevance FLOAT,
    task_success_rate FLOAT,
    avg_latency_ms FLOAT,
    avg_token_usage INT,
    user_thumbs_up_rate FLOAT,              -- 用户显式反馈
    task_completion_rate FLOAT,             -- 任务完成率（从 trace 推导）
    measured_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 2. 版本号规范

```
<major>.<minor>.<patch>-<stage>
```

- **major**：架构级变化（输出 schema 改了、tool 列表变了）→ 强制回归
- **minor**：逻辑优化（角色定义改了、few-shot 例子改了）→ 评测准入
- **patch**：文案微调（措辞、错别字）→ 可快速上线
- **stage**：`draft` / `canary`（10% 流量）/ `active` / `retired`

例：`v3.2.1-canary`、`v3.2.0-active`、`v2.5.0-retired`

---

## 3. A/B 分流机制

### 3.1 路由表

```python
# prompts/router.py
class PromptRouter:
    def __init__(self):
        # 配置：每个 (tenant, prompt_name) 的分流策略
        # 格式：{"canary_pct": 0.1, "canary_version": 12, "active_version": 11}
        self.strategy: dict[tuple[str, str], dict] = {}

    def resolve(self, tenant_id: str, prompt_name: str, user_id: str) -> int:
        """返回应使用的 prompt version。"""
        cfg = self.strategy.get((tenant_id, prompt_name))
        if not cfg:
            # 无策略：取 active 版本
            return self.get_active(tenant_id, prompt_name)
        # 用 user_id 哈希决定分流，保证同一用户始终走同一分支
        bucket = int(hashlib.md5(f"{tenant_id}:{user_id}".encode()).hexdigest(), 16) % 100
        if bucket < cfg["canary_pct"] * 100:
            return cfg["canary_version"]
        return cfg["active_version"]
```

### 3.2 分流保证

- **同一用户始终走同一版本**：用 `hash(user_id) % 100` 分桶，避免体验跳跃
- **灰度比例逐步提升**：10% → 25% → 50% → 100%
- **出现问题秒级回滚**：把 `canary_version` 设为 None，所有流量回 active

### 3.3 强制回滚开关（Redis 共享，多副本生效）

```python
# governance/feature_flag.py —— Prompt 紧急回滚
import redis.asyncio as redis_async

class PromptFeatureFlag:
    """回滚列表必须用 Redis 共享，否则多副本下 A 回滚了 B 还在跑新版本。"""

    def __init__(self, redis_client: redis_async.Redis):
        self.r = redis_client
        self.key = "prompt:rollback_list"

    async def is_rolled_back(self, prompt_name: str) -> bool:
        return bool(await self.r.sismember(self.key, prompt_name))

    async def rollback(self, prompt_name: str) -> None:
        """把 prompt_name 加入回滚集合，所有副本立即生效。"""
        await self.r.sadd(self.key, prompt_name)

    async def resume(self, prompt_name: str) -> None:
        """解除回滚。"""
        await self.r.srem(self.key, prompt_name)

    async def resolve(self, tenant_id, prompt_name, user_id):
        if await self.is_rolled_back(prompt_name):
            # 紧急回滚：所有流量走上一稳定版本
            return self.get_stable(tenant_id, prompt_name)
        return self.router.resolve(tenant_id, prompt_name, user_id)
```

运维侧：`redis-cli SADD prompt:rollback_list "policy_qa.system"` 立即全量回滚，无需重启；恢复用 `SREM`。

---

## 4. 评测准入门槛

新 Prompt 版本从 `draft` → `canary` 必须满足：

### 4.1 自动评测指标门槛

```python
# governance/prompt_evaluation/gates.py
@dataclass
class PromptQualityGate:
    min_sample_size: int = 100
    min_faithfulness: float = 0.85
    min_relevance: float = 0.85
    max_latency_p95_increase_pct: float = 0.20   # 不能比旧版本慢 20% 以上
    max_token_usage_increase_pct: float = 0.30   # 不能比旧版本贵 30% 以上
    max_task_failure_rate: float = 0.05

async def check_canary_eligibility(
    tenant_id: str, name: str, new_prompt_id: int
) -> tuple[bool, list[str]]:
    """返回 (是否可进入 canary, 失败原因列表)。"""
    new_metrics = await get_metrics(new_prompt_id)
    old_metrics = await get_active_metrics(tenant_id, name)
    failures = []

    if new_metrics.sample_size < 100:
        failures.append(f"样本量不足: {new_metrics.sample_size}")

    if new_metrics.avg_faithfulness < 0.85:
        failures.append(f"faithfulness 不足: {new_metrics.avg_faithfulness}")

    if (new_metrics.avg_latency_ms - old_metrics.avg_latency_ms) / old_metrics.avg_latency_ms > 0.20:
        failures.append("延迟上升 >20%")

    if new_metrics.task_failure_rate > 0.05:
        failures.append(f"任务失败率过高: {new_metrics.task_failure_rate}")

    return (len(failures) == 0, failures)
```

### 4.2 人工评审门槛

重大变更（major 版本）必须：
- 2 名以上资深工程师 review
- 在 staging 环境跑完所有 golden 用例
- 业务方签字（如金融/政务场景）

### 4.3 灰度发布节奏

| 阶段 | 流量比例 | 持续时间 | 通过条件 |
|---|---|---|---|
| canary | 10% | 24h | 无 P1 告警 + 指标稳定 |
| 半量 | 50% | 48h | 指标无回退 |
| 全量 | 100% | — | 旧版本标 retired |

每个阶段不通过 → 自动回滚到上一个 stable 版本。

---

## 5. 线上指标回写（feedback loop）

### 5.1 收集的三类信号

1. **隐式信号**：从 trace 自动推导
   - `task_completion_rate`：是否生成最终答案
   - `retry_count`：是否被重规划（说明第一次规划失败）
   - `tool_error_rate`：tool 调用错误率
   - `latency_p95`：响应延迟

2. **显式信号**：用户反馈
   - 点赞 / 点踩
   - "重新生成"按钮点击（说明对答案不满意）
   - 主动 follow-up 问题

3. **LLM Judge**：异步评测
   - 抽样 5% 的请求，用 GPT-4 评测输出质量

### 5.2 写入 Prompt Metric

```python
# prompts/feedback_collector.py
async def collect_metrics():
    """每 10 分钟聚合一次，写入 prompt_metric 表。"""
    while True:
        for prompt_id in get_active_prompt_ids():
            stats = await aggregate_trace_metrics(prompt_id, window_minutes=10)
            thumbs = await aggregate_thumbs_up(prompt_id, window_minutes=10)
            await db.execute(
                "INSERT INTO prompt_metric (prompt_id, sample_size, ...) VALUES (...)",
                prompt_id=prompt_id, sample_size=stats.count, ...,
            )
        await asyncio.sleep(600)
```

### 5.3 自动告警

```yaml
# deploy/prometheus/rules/prompt.yml
- alert: PromptCanaryRegression
  expr: >
    (avg(rate(prompt_relevance_score[1h])) 
     / avg(rate(prompt_relevance_score[1h] offset 1d))) < 0.95
  for: 30m
  annotations:
    summary: "Prompt 质量回退 >5%（对比昨日同期），需关注"
- alert: PromptUserFeedbackDrop
  expr: >
    (rate(prompt_thumbs_up_total[1h])
     / rate(prompt_impression_total[1h])) < 0.6
  for: 1h
  annotations:
    summary: "Prompt 用户点赞率 <60%"
```

---

## 6. 回滚流程

### 6.1 紧急回滚（5 分钟内生效）

```bash
# 1. 立即把 canary 流量回 active（FeatureFlag）
redis-cli SADD prompt_rollback_list "policy_qa.system"

# 2. 标记 canary 为 retired
psql -c "UPDATE prompt SET status='retired', retired_at=now() WHERE name='policy_qa.system' AND status='canary';"

# 3. 触发告警（自动）
# 4. 通知相关人
```

### 6.2 标准回滚（灰度中发现问题）

灰度阶段发现 P1 问题：
1. 把 canary 流量立即降到 0%（FeatureFlag）
2. 复盘 → 修 prompt → 重启灰度流程
3. 旧版本保持 active 不动

---

## 7. CLI 工具

```bash
# 注册新 prompt 版本
python -m prompts.cli register \
  --name policy_qa.system \
  --version v3.2.1 \
  --file prompt_v3.2.1.txt \
  --model claude-sonnet

# 提交评测
python -m prompts.cli evaluate \
  --name policy_qa.system --version v3.2.1 --datasets policy_qa_golden

# 进入 canary（必须先通过评测门槛）
python -m prompts.cli promote \
  --name policy_qa.system --version v3.2.1 --to canary --pct 10

# 灰度比例调整
python -m prompts.cli canary --name policy_qa.system --pct 50

# 全量
python -m prompts.cli promote --name policy_qa.system --version v3.2.1 --to active

# 紧急回滚
python -m prompts.cli rollback --name policy_qa.system

# 列出当前状态
python -m prompts.cli list
```

---

## 8. 与 CI 集成

```yaml
# .github/workflows/prompt_change.yml
on:
  pull_request:
    paths:
      - 'prompts/**'

jobs:
  prompt_eval:
    runs-on: ubuntu-latest
    steps:
      - name: Eval new prompt version
        run: |
          python -m prompts.cli register --file ${{ github.event.pull_request.head.ref }}/prompts/policy_qa.v*.txt
          python -m prompts.cli evaluate --gate
      - name: Comment on PR
        if: always()
        run: |
          python -m prompts.cli comment-pr --pr ${{ github.event.number }}
```

PR 上自动看到："新版本 faithfulness 0.88（+0.02 vs active）✅ 延迟上升 8% ✅ 通过准入门槛"

---

## 9. 禁止事项

- ❌ **禁止**在线上直接编辑 prompt 并期望"下次重启生效"——必须走注册流程
- ❌ **禁止**同一 tenant 同一 name 有两个 `active` 版本（CI 检查）
- ❌ **禁止**不通过评测就把 prompt 推到 `active`
- ❌ **禁止**A/B 分流时按时间分桶（不同时间进入的用户体验不一致）
- ❌ **禁止**Prompt Metric 不带 tenant_id 聚合（数据隔离）