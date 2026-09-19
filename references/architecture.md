# 六层架构详解（源自 gov_AP v3.0 + 企业级评审修订版）

> 与 gov_AP 原实现的差异以 **[修订]** 标注——这些是把"蓝图企业级"提升为"运行时企业级"的关键改动，
> 新项目直接按修订版实现，勿复刻原实现的单体痕迹。

## 0. 三大运行时原则（高于一切模块设计）

1. **无状态运行时**：api 与 worker 进程不持有任何业务可变状态；Graph 用官方 PostgresSaver 持久化，
   缓存进 Redis，指标进 prometheus_client 的全局 Registry——多副本水平扩展是默认形态，不是后期改造。
2. **重任务异步化**：请求线程只做轻量编排（设计目标 p95 <2s，为 `slo.md` §1 的 2.5s SLO 留裕度）；耗时 >5s 的任务（OCR / 批量索引 / 评测 /
   外部长调用）一律入 Redis 队列由 Worker 进程执行，靠 trace_id + checkpointer 回接主流程。
3. **生态优先**：checkpointer=官方 PostgresSaver、指标=prometheus_client、追踪=OTel SDK、队列=ARQ。
   自研预算只有一个名额，给 guardrail（业务差异化核心）；每引入一个自研件都必须在 ADR 里给出理由。

## 1. 分层职责与组成

### L1 接入层（backend/）
- `main.py`：`create_app()` 工厂。中间件注册顺序（FastAPI 后注册先执行，顺序不可错）：
  1. CORS（白名单制，默认 `localhost:<前端端口>,localhost:<grafana端口>`，禁 `*`）
  2. TracingMiddleware（OpenTelemetry SDK 真接入；仅本地开发无 collector 时允许 NoOp）
  3. RBACMiddleware（角色/权限校验，**必须在 Auth 之后注册**——LIFO 下后注册的先执行）
  4. AuthMiddleware（JWT 强制认证，无有效 Bearer Token 一律 401）
  5. RequestLoggingMiddleware（请求日志 + 注入 X-Trace-Id 响应头）
- `config.py`：pydantic-settings 读 .env，`get_settings()` lru_cache 单例。配置域：App / LLM / Embedding / PostgreSQL / Redis / Milvus / Agent Runtime / MCP / A2A / Queue / JWT / CORS。
- `api/routes.py`：核心端点 `POST /api/chat`（含护栏前置、trace、evidence）；`api/dependencies.py`：`execute_agent()` 是 HTTP → Graph 的唯一桥（护栏前置 → create_initial_state → graph.ainvoke）。

### L2 编排层（orchestration/langgraph/）
- `state.py`：AgentState（TypedDict）+ Pydantic 子模型 + 枚举（RiskLevel/TaskStatus/AgentName/NodeName）+ helper 函数（create_initial_state/set_intent/add_task/record_mcp_call/set_error...）
- `graph.py`：`build_graph(llm, mcp_client, a2a_connector, checkpointer)` 工厂，**[修订]** 纯函数无副作用——每次构建新实例或进程内缓存一个不可变编译图；`create_default_graph()` 无 LLM 纯 stub 模式（开发/CI 用）
- `nodes.py`：每个节点 = 一个 Agent 的包装（设 current_agent → 记 trace span → 调 Agent → 记 mcp_history/metrics，成功失败都记）
- `edges.py`：条件路由。关键规则：
  - 无 intent → intent_node；有 PENDING 任务 → 按 agent 分发；全完成 → 合成/governance
  - 意图识别后**必回 supervisor 二次规划**（静态边）
  - `risk_level=high/critical` → 跳过业务直接 governance
  - error 且 retry_count < 3 → 回 supervisor 重规划
- **[修订]** `checkpointer`：直接用 LangGraph 官方 `PostgresSaver`（`langgraph-checkpoint-postgres`，依赖 **Psycopg 3**——与业务库的 asyncpg 是两套连接，并存无冲突），禁止自研 BaseCheckpointSaver；A2A 挂起/恢复通过 checkpoint_id + thread_id 组合实现，不引入第二套持久化格式。
  **构造三要点**（缺任一则静默失败，完整示例见 `langgraph-1x-api.md` §5.1）：① 用 `AsyncConnectionPool` 且 `kwargs={"autocommit": True}`——**不开 autocommit 时 `setup()` 建表会静默失败**；② 连接需 `row_factory=dict_row`；③ **`await saver.setup()` 必须显式调用一次**（建 `checkpoints`/`checkpoint_blobs`/`checkpoint_writes`/`checkpoint_migrations` 四表，之后幂等）
- `runtime.py`：AgentRuntime 安全护栏——max_steps=10、LoopDetector（窗口6 + 连续3相同 tool → 中断重规划）、timeout、三类异常

### L3 专业 Agent 层（agents/）
每个 Agent 包结构固定：`__init__.py`（导出）+ `agent.py`（核心，提供 `process()` LangGraph 节点接口）+ `schema.py`（Pydantic）+ `prompts.py`（模板）+ 按需能力模块。
- `supervisor/`：agent.py（orchestrate 主循环）+ planner.py（LLM+规则混合规划，JSON 容错解析，intent 预置任务模板）+ router.py（4层路由：精确表→模糊→LLM→关键词推断）
- `intent/`：三级分类链——微调小模型（BERT，置信度>0.7）→ 关键词匹配（0.5-0.7）→ LLM fallback
- 业务 Agent（policy/material/workflow...）：按业务域裁剪，如 material = ocr.py + extractor.py + validator.py
- `governance/`：旁路安全 Agent，协调 security（PII/注入/敏感词/泄露）+ behavior（循环/异常行为）+ optimizer（自动优化建议），**不参与业务回答**

### L4 MCP 工具层（tools/mcp/）
- `schema.py`：每个 Tool 的 Input/Output Pydantic 模型 + TOOL_REGISTRY 全局注册表（先定义 Schema 再写实现）
- `client.py`：list_tools / call_tool，自动携带 `Authorization: Bearer`，仅经 Gateway，不可用**快速失败**
- `gateway.py`：FastAPI 网关，统一 JWT 认证 + RBAC（admin/agent 角色）+ 路由 + 审计；无有效 Token 一律拒绝，未知 tool 返回 403 并记审计
  ⚠️ **JWT 是本平台内部约定，不是 MCP 规范要求**。MCP 规范把 Server 定义为 **OAuth Resource Server**（强制 OAuth 2.1 + Protected Resource Metadata，RFC 9728 `/.well-known/oauth-protected-resource`；client 须校验 `iss` 并拒绝 audience 不匹配的 token），JWT 只是可接受的 token 格式之一。**自家生态内用 JWT 可行；一旦要对接第三方/外部 MCP（见 `mcp-platform-integration.md` 场景 B/D），Gateway 必须补 OAuth 2.1**，否则规范实现方连不进来
- `servers/<域>_server/`：server.py（入口，注册 tools）+ tools.py（实现，调用 rag/或业务逻辑）

### L5 异步协同层（tools/a2a/ + tools/workers/）
- **[修订]** `tools/workers/`：ARQ 任务队列（复用 Redis）。api 进程 `await task_pool.enqueue_job("run_ocr", trace_id, ...)` → Worker 进程执行 → 结果写 PG → 需要回接主流程时按 thread_id 恢复 Graph。适用：OCR、批量索引、评测跑批、外部长调用。**禁止**在请求线程里做耗时 >5s 的工作。
- `tools/a2a/`：`protocol.py`（AgentCard/消息/任务模型，v1.0 支持**签名 Agent Card**：JWS RFC 7515 + JCS RFC 8785）→ `task.py`（**状态机对齐 A2A v1.0 规范的 8 态**：`TASK_STATE_SUBMITTED` / `TASK_STATE_WORKING` / `TASK_STATE_INPUT_REQUIRED` / `TASK_STATE_AUTH_REQUIRED` / `TASK_STATE_COMPLETED` / `TASK_STATE_FAILED` / `TASK_STATE_CANCELED` / `TASK_STATE_REJECTED`——v1.0 按 ProtoJSON 写成 SCREAMING_SNAKE + `TASK_STATE_` 前缀，**不是 v0.3 的 `submitted` / `input-required` 那种写法**（详见 `patterns.md §8`）；后四者为终态、不可重启，需在同一 `contextId` 下新建 Task。本地持久化如需 `CREATED` 等内部态，只在本表用，**不得出现在对外协议字段里**）→ `connector.py`（HTTP 对接，同步 completed / 异步 submitted 分流）→ `callback.py`（**自定约定**：HMAC-SHA256 签名 + ±300s 防重放 → 恢复 LangGraph。规范只定义 `PushNotificationConfig{url, token, authentication}` 且**重试策略未定义**，故 HMAC 与时间窗是我们的实现选择，非协议要求）→ `task_store.py`（PostgreSQL 写穿）→ `registry.py`（外部 Agent 注册中心）
- 挂起/恢复：send_task 异步 → 官方 PostgresSaver 挂起（记录 checkpoint_id + thread_id）→ END；外部完成 → POST /api/a2a/callback（HMAC）→ 按 thread_id 取 checkpoint → graph.ainvoke 续跑
- **[修订]** A2A 的 send_task 调用本身也应入队（外系统响应时间不可控），Worker 内做重试 + 死信，禁止在 api 线程同步等待外部 Agent

### L6 AgentOps 治理层（governance/）
- `trace.py`：全链路 Trace（root trace + 子 span 自动继承 parent_span_id，ContextVar 协程安全），PostgreSQL 持久化；**[修订]** 内存镜像仅作启动缓冲（带 TTL 上限），禁止作为长期存储路径
- `guardrail.py`（唯一保留的自研核心）：输入检测（PII/注入/敏感词）+ 输出过滤（错误泄露/密钥泄露/Prompt泄露）
- `pii.py`：四类脱敏——手机 `138****1234`、身份证、邮箱 `u***@domain.com`、银行卡
- **[修订]** `metrics.py`：直接用 `prometheus_client`（Counter/Gauge/Histogram 带 bucket，agent/tool/guardrail 维度），`/metrics` 端点用 `make_asgi_app()` 挂载——禁止自研文本格式导出（自研 histogram 无 bucket，Grafana p95 不可算）
- `evaluation/`：metrics（RAG：faithfulness/answer_relevance/context_recall；Agent：task_success_rate/tool_accuracy/latency/steps）→ evaluator → benchmark（Golden Dataset）→ runner（CLI，CI 集成；跑批任务入 Worker 队列）

## 2. 一次请求的完整链路

```
User → POST /api/chat
  ├─ JWT 认证 → RBAC → trace_id 生成（UUID7，时间排序）
  ├─ 输入护栏前置（GuardrailRunner.run_input，命中即阻断，不消耗 Token）
  ├─ create_initial_state(user_query, trace_id)
  └─ graph.ainvoke（AgentRuntime 安全护栏内）
       START → supervisor（Planner 拆任务 + Router 分发）
         ├─ 无intent → intent（BERT→关键词→LLM）→ 回 supervisor 二次规划
         ├─ policy（MCP→Milvus+BM25+Reranker→LLM 生成 evidence）
         ├─ material（轻量校验内联；OCR 等重活 → enqueue ARQ → Worker 执行 → 回写结果）
         ├─ workflow（MCP→create_case 落库办件）
         │    └─ 需跨域 → a2a_node（入队 send_task → 挂起→END / 同步完成直接续跑）
         └─ 全完成 → supervisor 合成 final_answer → governance
                         ├─ 输出护栏 + PII 脱敏 + risk_level
                         └─ blocked→END / error且retry<3→supervisor / 正常→END
  → ChatResponse{answer, evidence, intent, risk_level, execution_steps, elapsed_ms}
  （含异步任务时返回 task_id + 轮询/SSE 端点，不阻塞 HTTP 线程）
```

## 3. 降级容错链（三级，真实优先）

| 层级 | 降级对象 | 触发 | 行为 |
|---|---|---|---|
| L1 基础设施 | PostgreSQL / Redis / Milvus | 连接失败 | **[修订]** 启动期快速失败（拒绝以降级状态启动）；运行期 Milvus 降级 BM25 内存检索、Redis 降级无缓存直查 PG |
| L2 模型层 | LLM API / BERT / OCR | 无 Key / 模型缺失 / 失败 | 规则模板回答 / 关键词匹配 / **显式报错（禁静默 mock）** |
| L3 协议层 | MCP Server / A2A Connector | 不可达 | 显式降级 stub（结果标注 mode=stub）/ 开关控制的显式降级 + 队列内有限重试后进死信 |

原则：**降级必须显式**——结果带 mode 字段 + logger.warning，生产禁止静默返回假结果。

## 4. 可观测性接线

```
supervisor_node（首节点）: start_trace(user_query) → 建 root trace
每个节点: AgentTracer.span(AGENT, node) → 子 span；record_agent_call()（成功+失败都记）
Worker 任务: 同一 trace_id 开子 span → trace 跨进程连续
governance_node（末节点）: 输出护栏 + end_trace() 收口写库
```

**[修订]** 指标全部走 `prometheus_client`：Counter（agent_tool_calls_total{agent,tool,status}）、Histogram（agent_latency_seconds{agent}，标准 bucket）、Gauge（queue_depth / a2a_pending）。`/metrics` 用 `make_asgi_app()` 免 JWT 挂载（网络层限内网），Prometheus 15s 抓取，Grafana 自动加载看板。**告警阈值与分级以 `slo.md` §3 为唯一来源**（P0/P1/P2 三级 + 响应时效 + 升级路径），此处不重复具体数字——双份维护必然导致阈值打架（本处曾与 `slo.md` 出现 2s/5s、100/500 两套数字并存）。

## 5. 规模化部署形态（[修订] 新增）

```
              LB
       ┌───────┴───────┐
    api 副本 ×N      worker 副本 ×M        （同镜像不同入口命令，无状态可随意扩缩）
       └───────┬───────┘
         PG（checkpoint/trace/业务）  Redis（队列/缓存）  Milvus（向量）
```

验收标准（企业级定义）：
- 杀掉任一 api/worker 副本，在途请求不丢失（checkpoint 可恢复）、服务不中断
- api 与 worker 只经 Redis 队列 + PG 通信，不存在共享内存态
- 每个容器有健康检查（/health + worker 心跳）；滚动发布不丢流量

---

## 6. 关联文档（按问题跳转）

| 关注点 | 看哪个文档 |
|---|---|
| 架构选型为什么这么定 / 何时改架构 | `adr-template.md` |
| 出问题了谁背锅 / 怎么恢复 | `slo.md` |
| Token 费用爆了怎么办 | `llm-cost.md` |
| 安全事件 / 注入攻击 | `llm-security.md` |
| 跨租户数据隔离 | `multi-tenant.md` |
| MCP 工具来源选型 | `mcp-platform-integration.md` |
| 测试覆盖与 LLM 评测 | `testing-matrix.md` |
| 监控指标 / 看板 / 告警 | `grafana-dashboards.md` |
| Prompt 改坏了怎么办 / A/B | `prompt-versioning.md` |
| 供应商故障 / 主备切换 | `llm-routing.md` |
| LangGraph API 用法 | `langgraph-1x-api.md` |
| 端到端最小可跑 demo | `../scripts/demo_end_to_end.py` |
