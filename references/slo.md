# SLO / 灾备 / 容量规划

> 没有 SLO 就没有"企业级承诺"。本文档定义本架构必须满足的服务等级目标、
> 灾备策略与容量评估方法，作为 SLA 与告警配置的源头单一依据。

---

## 1. SLO 总览（必看）

| SLI | 目标值 | 测量窗口 | 优先级 |
|---|---|---|---|
| **可用性**（API + Worker） | 99.5%（月） | 30 天滚动 | P0 |
| **API p95 端到端延迟** | < 2.5s | 5 分钟滚动 | P0 |
| **API p99 端到端延迟** | < 8s | 5 分钟滚动 | P1 |
| **护栏拦截后错误率** | < 0.5% | 30 天 | P0 |
| **Worker 任务成功率** | > 99% | 24 小时 | P0 |
| **A2A 死信率** | < 0.1% | 24 小时 | P1 |
| **Checkpoint 写入 p95** | < 100ms | 5 分钟 | P1 |
| **Token 月度配额利用率** | < 80% | 月度 | P1 |

> **错误预算（Error Budget）**：99.5% 可用性 = 月度 3.6 小时停机额度。
> 当月消耗 >50% 预算，必须冻结非必要变更；>80% 触发事故复盘。

> **阈值层级（必须遵守）**：设计目标 2s（`architecture.md` §0）≤ 扩容触发 2s（§5.2）≤ **SLO 2.5s**（本节）≤ 告警阈值。
> **告警阈值不得松于 SLO**——告警是 SLO 的前哨，不是事后通知；比 SLO 更松的阈值等于没有告警。
> 进阶：延迟类 SLO 可改用多窗口燃烧率替代固定阈值（如 1h 窗口 14.4x / 6h 窗口 6x）。

---

## 2. SLI 的具体测量方法

### 2.1 可用性

```
可用性 = 1 - (5xx 响应数 / 总响应数)
```

排除项：
- 4xx 客户端错误（用户输入问题，不计入）
- `/health`、`/metrics`、`/api/a2a/callback`（运维端点）
- 主动维护窗口（需提前 24h 在 `governance/maintenance_windows` 表登记）

数据源：`prometheus_client` 中两个指标——
- Counter `api_requests_total{status_class}`：按状态码分类（`2xx`/`4xx`/`5xx`）
- Histogram `api_request_duration_seconds_bucket`：用于计算 p95/p99 延迟

### 2.2 API p95 延迟

端到端定义：从 FastAPI 接收请求到返回 Response 的全部时间。
**不**包含重试入队后的轮询延迟。

```python
# 已在 patterns.md §10 定义
API_LATENCY = Histogram(
    "api_request_duration_seconds",
    "End-to-end API latency",
    ["route", "method"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),  # 标准 bucket，p95/p99 可算
)
```

### 2.3 Token 月度配额利用率

按租户聚合 LLM 调用 token 数 vs 配置上限：

```sql
SELECT tenant_id, sum(prompt_tokens + completion_tokens) as used
FROM llm_call_log
WHERE created_at > date_trunc('month', now())
GROUP BY tenant_id;
```

告警阈值：利用率 >80% 触发 Warn，>95% 触发限流（拒绝该租户新请求直到下月）。

---

## 3. 告警分级与响应

### P0 告警（5 分钟响应）

| 指标 | 阈值 | 通知渠道 |
|---|---|---|
| API 错误率（5xx / 总数） | >5% 持续 2 分钟 | 钉钉/企微值班群 + 短信 |
| API p95 延迟 | >2.5s（= SLO 值）持续 5 分钟 | 同上 |
| Worker 队列深度 | >500 持续 3 分钟 | 同上 |
| PostgreSQL 连接池 | >85% 使用率 | 同上 |
| Redis 内存 | >80% 使用率 | 同上 |

> **为什么队列深度有 200 与 500 两个数**：§5.2 的 >200 是**扩容触发**（先加 worker 副本），>500 是**P0 告警**（说明扩容已跟不上）。两者层级不同、不冲突；但若只配了告警而没配 HPA，500 才响铃意味着积压已相当严重。

### P1 告警（30 分钟响应）

| 指标 | 阈值 |
|---|---|
| A2A 死信出现 | 任意数量 |
| Checkpoint 写入 p95 | >200ms 持续 10 分钟 |
| Token 配额利用率 | >80% |

### P2 告警（次日处理）

- 单个 Agent 节点成功率下降 >10%
- Milvus/BM25 命中率下降 >15%
- LLM 缓存命中率 <40%（应有 60%+）

---

## 4. 灾备策略

### 4.1 数据备份

| 数据 | 备份频率 | 保留时长 | RPO | RTO |
|---|---|---|---|---|
| PostgreSQL（业务+checkpoint+trace） | 全量每日 + 增量每 6h | 30 天 | 6h | 1h |
| Redis（缓存+队列） | 不备份（可重建） | — | — | — |
| Milvus（向量） | 全量每周 + 增量每日 | 90 天 | 24h | 4h |
| 模型权重 | 本地 + OSS 双备份 | 永久 | — | — |
| 配置 / .env | Git + Vault 双备份 | 永久 | — | — |

### 4.2 故障转移

**PostgreSQL 主从切换**：
- 使用流复制 + Patroni，自动选主
- 应用连接串走 PgBouncer，主从切换对应用透明

**Redis Sentinel**：
- 1 主 2 从 3 Sentinel
- 应用走 Sentinel 自动发现新主

**Milvus**：
- 多副本部署，etcd + MinIO 必须独立部署
- 单节点故障不丢数据，但需监控 etcd 健康

### 4.3 灾备演练

每季度一次完整演练：
1. 关闭主库 → 验证从库自动接管
2. 注入 Milvus 故障 → 验证降级到 BM25 内存检索
3. 杀光所有 worker → 验证任务持久化在 Redis，可恢复

---

## 5. 容量规划

### 5.1 基准假设

- 单 API 副本：~50 QPS（p95 <2s）
- 单 Worker 副本：~5 任务/分钟（OCR 类重型任务）
- PG 连接池：默认 20/副本
- Redis 内存：每租户配额 512MB + 共享 2GB

### 5.2 扩容触发

| 指标 | 阈值 | 动作 |
|---|---|---|
| API p95 延迟 | >2s 持续 10 分钟 | K8s HPA 加 1 副本（上限 20） |
| Worker 队列深度 | >200 持续 5 分钟 | K8s HPA 加 1 worker 副本 |
| PG 连接池使用率 | >70% | 排查慢查询 + 考虑读副本 |

### 5.3 容量评估 checklist（每月执行）

- [ ] 当前 QPS × 30% 安全裕度 vs 现有副本数
- [ ] PG / Redis / Milvus 存储增长率预测 6 个月容量
- [ ] Token 月用量增长率 vs 预算
- [ ] 模型权重 + RAG 语料总量 vs OSS 配额

---

## 6. 事故复盘模板（Postmortem）

每次 P0 告警触发后必须 24h 内出 Postmortem：

```markdown
# 事故 Postmortem — YYYY-MM-DD HH:MM

## 时间线
- HH:MM 告警触发
- HH:MM 第一响应人介入
- HH:MM 根因定位
- HH:MM 修复完成
- HH:MM 服务恢复

## 影响
- 用户影响范围（多少租户、多少请求）
- SLO 消耗（占月度错误预算的 X%）

## 根因
（直接原因 + 根本原因，5 Whys）

## 改进项
- [ ] 短期（24h）：...
- [ ] 中期（1 周）：...
- [ ] 长期（1 月）：...
```

Postmortem blame-free，目标是改进系统而不是追责个人。