# Grafana Dashboard 模板

> Skill 提到"用 Grafana"，但没给具体 panel。本文给出 8 个 Dashboard（含约 35 个核心 panel）
> 的 PromQL + JSON 片段，可直接 import 到 Grafana。所有指标已在 patterns.md / llm-cost.md /
> llm-security.md / llm-routing.md 中定义。

---

## 1. Dashboard 列表

| Dashboard | 用途 | 关键 Panel |
|---|---|---|
| API Overview | HTTP 层总览 | 请求量、错误率、p95/p99 延迟、活跃副本数 |
| Agent Performance | Agent 决策链 | 各 Agent 节点耗时、成功率、Token 用量 |
| LLM Cost | 成本治理 | Token 用量、配额利用率、缓存命中率、模型路由分布 |
| Worker & Queue | 异步任务 | 队列深度、任务成功率、Worker 心跳、重试率 |
| Security | 安全审计 | 护栏拦截率、注入检测、文档投毒、Prompt 泄露 |
| MCP Gateway | 工具调用 | Tool 调用量、错误率、未知 Tool、跨租户尝试 |
| Checkpoint & RAG | 数据层 | Checkpoint 写入延迟、Milvus 检索延迟、BM25 命中率 |
| LLM Routing & Failover | 多供应商容错 | 熔断状态、供应商健康度、路由决策速率、回退占比、全供应商失败 |

---

## 2. Dashboard JSON 模板

### 2.1 API Overview Dashboard

```json
{
  "title": "API Overview",
  "uid": "api-overview",
  "tags": ["multi-agent-platform"],
  "panels": [
    {
      "title": "API 请求量（RPS）",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
      "targets": [{
        "expr": "sum by (route) (rate(api_requests_total[1m]))",
        "legendFormat": "{{route}}"
      }]
    },
    {
      "title": "API 错误率（5xx）",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0},
      "targets": [{
        "expr": "sum by (route) (rate(api_requests_total{status_class=~\"5xx\"}[1m])) / sum by (route) (rate(api_requests_total[1m]))",
        "legendFormat": "{{route}}"
      }],
      "alert": {
        "name": "API 错误率 >5%",
        "conditions": [{"evaluator": {"params": [0.05]}, "operator": {"type": "gt"}}],
        "for": "2m"
      }
    },
    {
      "title": "API p95 延迟",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
      "targets": [{
        "expr": "histogram_quantile(0.95, sum by (route, le) (rate(api_request_duration_seconds_bucket[5m])))",
        "legendFormat": "{{route}} p95"
      }],
      "alert": {
        "name": "API p95 > 2.5s",
        "conditions": [{"evaluator": {"params": [2.5]}, "operator": {"type": "gt"}}],
        "for": "5m"
      }
    },
    {
      "title": "API p99 延迟",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 8},
      "targets": [{
        "expr": "histogram_quantile(0.99, sum by (route, le) (rate(api_request_duration_seconds_bucket[5m])))"
      }]
    }
  ]
}
```

### 2.2 Agent Performance Dashboard

```json
{
  "title": "Agent Performance",
  "uid": "agent-performance",
  "panels": [
    {
      "title": "Agent 节点 p95 延迟",
      "type": "graph",
      "gridPos": {"h": 8, "w": 24, "x": 0, "y": 0},
      "targets": [{
        "expr": "histogram_quantile(0.95, sum by (agent, le) (rate(agent_latency_seconds_bucket[5m])))",
        "legendFormat": "{{agent}}"
      }]
    },
    {
      "title": "Agent 调用成功率",
      "type": "stat",
      "gridPos": {"h": 6, "w": 24, "x": 0, "y": 8},
      "targets": [{
        "expr": "sum by (agent) (rate(agent_calls_total{status=\"success\"}[5m])) / sum by (agent) (rate(agent_calls_total[5m]))"
      }],
      "fieldConfig": {
        "defaults": {
          "unit": "percentunit",
          "thresholds": {
            "steps": [
              {"color": "red", "value": null},
              {"color": "yellow", "value": 0.95},
              {"color": "green", "value": 0.99}
            ]
          }
        }
      }
    },
    {
      "title": "Agent 调用分布（饼图）",
      "type": "piechart",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 14},
      "targets": [{
        "expr": "sum by (agent) (increase(agent_calls_total[1h]))"
      }]
    },
    {
      "title": "决策路径回环检测（同一 Agent 重复调用）",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 14},
      "targets": [{
        "expr": "sum by (agent) (rate(agent_calls_total[1m])) > 1",
        "legendFormat": "{{agent}}（每分钟超过 1 次需关注）"
      }],
      "description": "提示：supervisor / intent 调用频率异常高可能是回环"
    }
  ]
}
```

### 2.3 LLM Cost Dashboard

```json
{
  "title": "LLM Cost",
  "uid": "llm-cost",
  "panels": [
    {
      "title": "Token 用量趋势（按租户 × 模型）",
      "type": "graph",
      "gridPos": {"h": 8, "w": 24, "x": 0, "y": 0},
      "targets": [{
        "expr": "sum by (tenant_id, model) (rate(llm_tokens_total[5m]) * 60)",
        "legendFormat": "{{tenant_id}} - {{model}}"
      }]
    },
    {
      "title": "月度配额利用率 TOP 10",
      "type": "bargauge",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
      "targets": [{
        "expr": "topk(10, sum by (tenant_id) (rate(llm_tokens_total[1h])) * 720)",
        "legendFormat": "{{tenant_id}}"
      }],
      "fieldConfig": {
        "defaults": {
          "unit": "short",
          "thresholds": {
            "steps": [
              {"color": "green", "value": null},
              {"color": "yellow", "value": 0.8},
              {"color": "red", "value": 0.95}
            ]
          }
        }
      }
    },
    {
      "title": "LLM 缓存命中率",
      "type": "gauge",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 8},
      "targets": [{
        "expr": "sum(rate(llm_cache_total{result=\"hit\"}[1h])) / sum(rate(llm_cache_total[1h]))"
      }],
      "fieldConfig": {
        "defaults": {
          "unit": "percentunit",
          "min": 0, "max": 1,
          "thresholds": {
            "steps": [
              {"color": "red", "value": null},
              {"color": "yellow", "value": 0.4},
              {"color": "green", "value": 0.6}
            ]
          }
        }
      }
    },
    {
      "title": "模型路由分布（small / medium / large）",
      "type": "piechart",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 16},
      "targets": [{
        "expr": "sum by (model_tier) (increase(agent_calls_total[1h]))"
      }]
    },
    {
      "title": "成本估算（USD/小时）",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 16},
      "targets": [{
        "expr": "sum by (model) (rate(llm_cost_usd_total[5m]) * 3600)",
        "legendFormat": "{{model}}"
      }]
    }
  ]
}
```

### 2.4 Worker & Queue Dashboard

```json
{
  "title": "Worker & Queue",
  "uid": "worker-queue",
  "panels": [
    {
      "title": "ARQ 队列深度",
      "type": "graph",
      "gridPos": {"h": 8, "w": 24, "x": 0, "y": 0},
      "targets": [{
        "expr": "sum by (queue) (queue_depth)",
        "legendFormat": "{{queue}}"
      }],
      "alert": {
        "name": "Worker 队列深度 >200",
        "conditions": [{"evaluator": {"params": [200]}, "operator": {"type": "gt"}}],
        "for": "5m"
      }
    },
    {
      "title": "Worker 心跳（每副本）",
      "type": "stat",
      "gridPos": {"h": 6, "w": 12, "x": 0, "y": 8},
      "targets": [{
        "expr": "sum by (worker_id) (worker_heartbeat)"
      }]
    },
    {
      "title": "Worker 任务成功率",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 8},
      "targets": [{
        "expr": "sum by (task) (rate(worker_task_total{status=\"success\"}[5m])) / sum by (task) (rate(worker_task_total[5m]))"
      }]
    },
    {
      "title": "死信数量（按 task 类型）",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 16},
      "targets": [{
        "expr": "sum by (task) (increase(worker_dead_letter_total[1h]))"
      }]
    },
    {
      "title": "重试率",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 16},
      "targets": [{
        "expr": "sum by (task) (rate(worker_task_retries_total[5m])) / sum by (task) (rate(worker_task_total[5m]))"
      }]
    }
  ]
}
```

### 2.5 Security Dashboard

```json
{
  "title": "Security",
  "uid": "security",
  "panels": [
    {
      "title": "护栏拦截率（输入 / 输出 / 工具结果）",
      "type": "graph",
      "gridPos": {"h": 8, "w": 24, "x": 0, "y": 0},
      "targets": [{
        "expr": "sum by (event_type) (rate(security_audit_total[5m]))",
        "legendFormat": "{{event_type}}"
      }]
    },
    {
      "title": "Prompt 注入检测（按租户）",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
      "targets": [{
        "expr": "sum by (tenant_id) (rate(security_audit_total{event_type=\"prompt_injection_blocked\"}[5m]))"
      }]
    },
    {
      "title": "PII 检测（按 PII 类型）",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 8},
      "targets": [{
        "expr": "sum by (pii_type) (rate(security_audit_total{event_type=\"pii_detected\"}[5m]))"
      }]
    },
    {
      "title": "未授权工具调用尝试（403）",
      "type": "graph",
      "gridPos": {"h": 8, "w": 24, "x": 0, "y": 16},
      "targets": [{
        "expr": "sum by (tool_name) (rate(mcp_unauthorized_calls_total[5m]))",
        "legendFormat": "{{tool_name}}"
      }]
    }
  ]
}
```

### 2.6 MCP Gateway Dashboard

```json
{
  "title": "MCP Gateway",
  "uid": "mcp-gateway",
  "panels": [
    {
      "title": "Tool 调用量（按 tool）",
      "type": "graph",
      "gridPos": {"h": 8, "w": 24, "x": 0, "y": 0},
      "targets": [{
        "expr": "sum by (tool_name) (rate(mcp_tool_calls_total[5m]))"
      }]
    },
    {
      "title": "Tool 错误率",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
      "targets": [{
        "expr": "sum by (tool_name) (rate(mcp_tool_calls_total{status=\"error\"}[5m])) / sum by (tool_name) (rate(mcp_tool_calls_total[5m]))"
      }]
    },
    {
      "title": "Tool 延迟 p95",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 8},
      "targets": [{
        "expr": "histogram_quantile(0.95, sum by (tool_name, le) (rate(mcp_tool_latency_seconds_bucket[5m])))"
      }]
    },
    {
      "title": "第三方 MCP 调用（成本敏感）",
      "type": "graph",
      "gridPos": {"h": 8, "w": 24, "x": 0, "y": 16},
      "targets": [{
        "expr": "sum by (tenant_id, provider) (rate(thirdparty_mcp_usage_total[5m]))"
      }]
    }
  ]
}
```

### 2.7 Checkpoint & RAG Dashboard

```json
{
  "title": "Checkpoint & RAG",
  "uid": "checkpoint-rag",
  "panels": [
    {
      "title": "Checkpoint 写入 p95",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
      "targets": [{
        "expr": "histogram_quantile(0.95, sum by (le) (rate(checkpoint_write_duration_seconds_bucket[5m])))"
      }],
      "alert": {
        "name": "Checkpoint 写入 p95 > 100ms",
        "conditions": [{"evaluator": {"params": [0.1]}, "operator": {"type": "gt"}}],
        "for": "10m"
      }
    },
    {
      "title": "Milvus 检索 p95",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0},
      "targets": [{
        "expr": "histogram_quantile(0.95, sum by (le) (rate(milvus_search_duration_seconds_bucket[5m])))"
      }]
    },
    {
      "title": "BM25 命中率（降级路径）",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
      "targets": [{
        "expr": "sum(rate(bm25_search_total{result=\"hit\"}[5m])) / sum(rate(bm25_search_total[5m]))"
      }]
    },
    {
      "title": "Reranker 调用成功率",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 8},
      "targets": [{
        "expr": "sum(rate(reranker_calls_total{status=\"success\"}[5m])) / sum(rate(reranker_calls_total[5m]))"
      }]
    }
  ]
}
```

### 2.8 LLM Routing & Failover Dashboard

> 为什么单列一个：`llm-routing.md` §2.6 定义了熔断告警、§7 定义了三个路由指标，
> 但此前**没有任何 dashboard 覆盖它们**——路由切了、熔断了、主备全挂了，看板上什么都看不到。
> 指标定义见 `llm-routing.md` §7。

```json
{
  "title": "LLM Routing & Failover",
  "uid": "llm-routing",
  "panels": [
    {
      "title": "熔断状态（0=closed / 1=half_open / 2=open）",
      "description": "供应商熔断器的当前状态。**状态是值不是标签**——所以按 provider 画线、用 value mappings 把 0/1/2 显示成文字，不要写成 {state=\"open\"} 那种选择器（选不中任何序列，面板会静默变空）。正常时全为 0；出现 2 表示该供应商已熔断、流量已切备选。",
      "type": "stat",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
      "targets": [{
        "expr": "llm_circuit_breaker_state",
        "legendFormat": "{{provider}}"
      }],
      "fieldConfig": {
        "defaults": {
          "min": 0, "max": 2,
          "thresholds": {
            "steps": [
              {"color": "green", "value": null},
              {"color": "yellow", "value": 1},
              {"color": "red", "value": 2}
            ]
          },
          "mappings": [
            {"type": "value", "options": {"0": {"text": "closed"}, "1": {"text": "half_open"}, "2": {"text": "open"}}}
          ]
        }
      }
    },
    {
      "title": "供应商健康度（1=healthy / 0=down）",
      "description": "主动探活结果。与熔断状态的区别：健康度是探活探测出来的，熔断状态是被动失败累计触发的，两者不一致本身就是线索（探活正常但持续熔断 → 多半是特定 prompt/模型的问题，不是供应商挂了）。",
      "type": "stat",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0},
      "targets": [{
        "expr": "llm_provider_healthy",
        "legendFormat": "{{provider}}"
      }],
      "fieldConfig": {
        "defaults": {
          "min": 0, "max": 1,
          "thresholds": {
            "steps": [
              {"color": "red", "value": null},
              {"color": "green", "value": 1}
            ]
          }
        }
      }
    },
    {
      "title": "路由决策速率（按原因）",
      "description": "reason 取值：primary_failed（主供应商失败触发回退）/ degraded（降级路由）/ weighted（合规场景配比分流）。primary_failed 持续 > 0 表示主供应商有问题，即使整体请求还在成功。",
      "type": "graph",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
      "targets": [{
        "expr": "sum by (primary_provider, fallback_provider, reason) (rate(llm_routing_total[5m]) * 60)",
        "legendFormat": "{{primary_provider}}→{{fallback_provider}} ({{reason}})"
      }]
    },
    {
      "title": "回退占比",
      "description": "回退到备选供应商的请求比例。稳态应接近 0；持续高于 5% 说明主供应商长期不健康，或熔断阈值设得太敏感。",
      "type": "gauge",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 8},
      "targets": [{
        "expr": "sum(rate(llm_routing_total{reason=\"primary_failed\"}[1h])) / sum(rate(llm_routing_total[1h]))"
      }],
      "fieldConfig": {
        "defaults": {
          "unit": "percentunit",
          "min": 0, "max": 1,
          "thresholds": {
            "steps": [
              {"color": "green", "value": null},
              {"color": "yellow", "value": 0.05},
              {"color": "red", "value": 0.2}
            ]
          }
        }
      }
    },
    {
      "title": "全供应商失败（P0）",
      "description": "所有供应商都不可用——此时所有 LLM 请求都在失败，必须人工介入。这个面板对应 llm-routing.md §2.6 的 LLMAllProvidersFailing 告警：**告警负责叫人，面板负责让人一眼看到范围和历史**。",
      "type": "graph",
      "gridPos": {"h": 8, "w": 24, "x": 0, "y": 16},
      "targets": [{
        "expr": "sum(rate(llm_errors_total{error_type=\"all_providers_failed\"}[5m]) * 60)"
      }],
      "fieldConfig": {
        "defaults": {
          "unit": "short",
          "thresholds": {
            "steps": [
              {"color": "green", "value": null},
              {"color": "red", "value": 0.01}
            ]
          }
        }
      }
    }
  ]
}
```

---

## 3. 告警阈值建议（写入 deploy/prometheus/rules/）

详见 `slo.md` §3 各 P0/P1 告警的 PromQL 配置。本节给出文件组织结构：

```
deploy/prometheus/
├── prometheus.yml                          # 主配置
├── rules/
│   ├── api.yml                             # API 层告警
│   ├── llm_cost.yml                        # 成本告警
│   ├── llm_security.yml                    # 安全告警
│   ├── llm_routing.yml                     # 熔断/回退告警（见 llm-routing.md §2.6）
│   ├── queue.yml                           # 队列告警
│   └── checkpoint.yml                      # Checkpoint 告警
└── alerts/
    └── alertmanager.yml                    # 通知路由

deploy/grafana/
├── dashboards/
│   ├── api-overview.json
│   ├── agent-performance.json
│   ├── llm-cost.json
│   ├── worker-queue.json
│   ├── security.json
│   ├── mcp-gateway.json
│   ├── checkpoint-rag.json
│   └── llm-routing.json
└── provisioning/
    ├── dashboards/dashboards.yml           # 自动加载 dashboard
    └── datasources/datasources.yml         # 数据源配置
```

---

## 4. 在 scaffold 脚本中自动生成

在 `scripts/scaffold_project.py` 中新增目录与默认 dashboard：

```python
PLAIN_DIRS = [
    # ... 已有目录
    "deploy/grafana/dashboards",
    "deploy/grafana/provisioning",
    "deploy/prometheus/rules",
]

# scaffold 时复制本 references/grafana-dashboards.md 中的 JSON 片段到 deploy/grafana/dashboards/
```

让新项目一上来就有完整可观测性，无需每个项目手写 JSON。

---

## 5. 自定义 dashboard 的方法论

新增面板时遵循：

1. **先有指标**：在 `governance/metrics.py` 用 `prometheus_client` 定义（必须有 `tenant_id` label）
2. **再写 PromQL**：在 `deploy/prometheus/rules/` 加告警规则
3. **最后做面板**：在 dashboard JSON 里引用
4. **看板描述必填**：`description` 字段说明"这个指标代表什么、什么值是正常的"

**禁止**：在 Grafana 里写 hardcode 的 sub-query 拼装而不定义 metric label——会让 dashboard 失去跨项目复用性。