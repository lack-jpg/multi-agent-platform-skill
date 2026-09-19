# 核心代码模式（骨架片段，直接复用改业务名）

## 1. AgentState（TypedDict + reducer）

```python
from typing import Annotated, TypedDict
from operator import add
from langgraph.graph import add_messages

def _task_plan_reducer(old: list, new: list) -> list:
    """按 id 合并：新 id 追加，已有 id 覆盖（防止重放时列表翻倍）"""
    merged = {t["id"]: t for t in (old or [])}
    for t in new or []:
        merged[t["id"]] = t
    return list(merged.values())

class AgentState(TypedDict):
    # 标量字段：覆盖更新
    trace_id: str
    user_query: str
    intent: str
    final_answer: str
    risk_level: str
    error: str | None
    retry_count: int
    # 列表字段：Annotated reducer
    task_plan: Annotated[list, _task_plan_reducer]
    messages: Annotated[list, add_messages]  # 消息列表必须用 add_messages（按 id 合并）
    mcp_history: Annotated[list, add]   # 每次 MCP 调用记录（trace_id/tool/latency/status）
    evidence: Annotated[list, add]
    error_history: Annotated[list, add]

def create_initial_state(user_query: str, trace_id: str) -> AgentState:
    return AgentState(trace_id=trace_id, user_query=user_query, intent="",
                      final_answer="", risk_level="low", error=None, retry_count=0,
                      task_plan=[], messages=[], mcp_history=[], evidence=[], error_history=[])
```

⚠️ 踩坑：A2A 恢复场景下 `a2a_tasks` 等"整表替换"语义的字段**不能**用 append reducer（恢复重放会翻倍），要替换语义。

⚠️ `messages` **不能**用 `operator.add`——它是纯拼接，不认识 message id：同 id 消息会堆积成两条、
`RemoveMessage(id=...)` 会被当成一条空消息追加（删除记忆静默失效）。必须用
`langgraph.graph.add_messages`（详见 `langgraph-1x-api.md §3.1`）。非消息列表用 `operator.add` 正确。

## 2. build_graph（依赖注入工厂）

```python
def build_graph(llm=None, mcp_client=None, a2a_connector=None, checkpointer=None):
    graph = StateGraph(AgentState)
    supervisor = SupervisorAgent(llm=llm)          # llm=None → 规则模式
    async def supervisor_node(state): return await supervisor.orchestrate(state)
    async def policy_node(state): ...              # 内部: mcp_client.call_tool(...) + 显式降级
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("intent", intent_node)          # ... 其余节点
    graph.add_edge(START, "supervisor")
    graph.add_edge("intent", "supervisor")         # 意图回环（静态边）
    graph.add_conditional_edges("supervisor", route_after_supervisor, {...})
    graph.add_conditional_edges("policy", route_after_specialist, {...})
    graph.add_conditional_edges("governance", route_after_governance,
                                {"retry": "supervisor", "end": END})
    return graph.compile(checkpointer=checkpointer)  # checkpointer 可选
```

## 3. 节点包装模式（trace + metrics + 降级）

> 前提（易错）：节点在 `build_graph()` **内部**定义，依赖经闭包注入，**签名只有 `(state)` 或 `(state, config)`**——**不接收** `mcp_client` 这类依赖参数（见 §2 与 `langgraph-1x-api.md` §4.1）。因此节点无法脱离 graph 单独调用；测试方式见 `testing-matrix.md` §2.1。

```python
async def policy_node(state: AgentState) -> dict:
    update_current_agent(state, AgentName.POLICY)
    t0 = time.perf_counter()
    success = False
    try:
        with AgentTracer.span(SpanKind.AGENT, "policy") as span:
            if mcp_client:
                result = await mcp_client.call_tool("search_policy", {"query": state["user_query"]})
                mode = "mcp"
            else:
                result = _stub_policy_search(state["user_query"])  # 显式降级
                mode = "stub"
        success = True
        return {"evidence": result["evidence"], ...}
    except Exception as e:
        return set_error(state, f"policy failed: {e}")
    finally:
        record_agent_call("policy", success, (time.perf_counter() - t0) * 1000, state["trace_id"])
```

## 4. MCP Client / Gateway 要点

```python
class MCPClient:
    async def call_tool(self, name: str, arguments: dict) -> dict:
        headers = {"Authorization": f"Bearer {self._token}"}   # JWT 自动携带
        # 仅经 Gateway 调用；不可用快速失败（不做静默直连 fallback）
```

Gateway（FastAPI）：
- `_verify_gateway_token`：校验 Bearer JWT（**平台内部约定**；MCP 规范要求 Server 作 OAuth Resource Server——对接第三方 MCP 时须补 OAuth 2.1 + RFC 9728 PRM，见 `architecture.md` L4）
- `_require_role("admin", "agent")`：RBAC 装饰器
- 端点：`/api/tools/list` + `/api/tools/call`；未知 tool → 403 + 审计

## 5. 三级降级模式（以意图分类为例）

```python
class IntentClassifier:
    def classify(self, text: str) -> "IntentResult":
        if self._model is not None:                    # 1) 微调小模型（置信度>0.7）
            r = self._bert_infer_sync(text)
            if r.confidence > 0.7:
                return r
        r = self._keyword_classify(text)               # 2) 关键词规则（0.5-0.7）
        if r.confidence >= 0.5:
            return r
        return self._llm_classify(text)                # 3) LLM fallback（最后手段）
```

## 6. 护栏前置（HTTP 桥处，LLM 之前）

```python
async def execute_agent(user_query: str, graph, ...) -> dict:
    guard = await guardrail_runner.run_input(user_query)     # 注入/CRITICAL敏感词 → 直接阻断
    if guard.blocked:
        return {"answer": "请求被安全策略拦截", "risk_level": guard.severity}
    state = create_initial_state(user_query, trace_id)
    try:
        result = await graph.ainvoke(state, config)
    except Exception as e:
        logger.exception("agent execution failed")
        raise HTTPException(500, "内部错误")                # 不透出异常细节
    return result
```

## 7. Runtime 安全护栏

```python
@dataclass
class RuntimeConfig:
    max_steps: int = 10          # 超限优雅终止
    loop_window: int = 6         # 滑动窗口
    loop_threshold: int = 3      # 窗口内连续 3 次相同 tool → 判定循环
    timeout: float = 30.0
    max_retries: int = 3         # error 时 retry<3 回 supervisor 重规划
```

## 8. A2A 异步挂起/恢复（[修订] 官方 PostgresSaver，禁自研）

> ✅ **协议状态更新（2026-09 核实）**：A2A **v1.0.0 已发布**（2026-03，首个稳定规范），并已于 2026-08-27 转入 **Agentic AI Foundation（AAIF，Linux Foundation 下辖）**。
> 原"0.x → 1.x 过渡中"的预警**已失效**——现在可按稳定规范实现。但注意 **v1.0 相对早期草案含破坏性变更**，其中**最容易踩的一条是 Task 状态名的写法**：
> - **v1.0 真值**按 ProtoJSON 约定写作 **SCREAMING_SNAKE + `TASK_STATE_` 前缀**：`TASK_STATE_SUBMITTED` / `TASK_STATE_WORKING` / `TASK_STATE_INPUT_REQUIRED` / `TASK_STATE_AUTH_REQUIRED` / `TASK_STATE_COMPLETED` / `TASK_STATE_FAILED` / `TASK_STATE_CANCELED` / `TASK_STATE_REJECTED`（另有 proto 零值 `TASK_STATE_UNSPECIFIED`，不是业务状态），**后四者为终态**、不可重启，需在同一 `contextId` 下新建 Task。
> - `submitted` / `working` / `input-required` / `auth-required` 那套**小写或连字符**写法是 **v0.3 的**。在官方 SDK 里它只活在兼容层（见 `a2a/compat/v0_3/`），**不是 v1.0 的线上取值**——拿它去比 `resp.status` 会恒不相等，且不报错，只是分支永不命中。
>
> 其余破坏性变更：新增**签名 Agent Card**（JWS + JCS）、多协议绑定（JSON-RPC 2.0 / gRPC / HTTP+REST）、`A2A-Version` 协商——照旧草案实现会不兼容。
> 本节给出的连接模式 + Checkpointer 选型相对稳定，但 **callback 端点的 schema、Task 状态枚举、HMAC 头命名** 仍可能随版本变更。
> 维护者责任：
> - 升级本 skill 前先 fetch `https://a2a-protocol.org/` 或其官方仓库确认差异；
> - 变更点必须在 `SKILL_MAINTENANCE.md` 留 ADR 或变更记录；
> - 若使用了具体实现（Hugging Face smolagents / LangGraph A2A / WorkBuddy A2A Connector 等），先看其发布说明再升级。

```python
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver   # 官方件
from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row

# 挂起（a2a_node 内，send_task 已入队由 Worker 执行）
# config 必须带 thread_id，且**必须带租户前缀**：
#   config = {"configurable": {"thread_id": f"{tenant_id}:{trace_id}"}}
# ⚠️ 这里不能只写 trace_id。PostgresSaver 用 thread_id 做 namespace，裸 trace_id 会让
#    跨租户撞键——而这个恢复入口是**外部可达的**（A2A 回调来自别的 Agent），漏前缀
#    等于把别人的挂起现场暴露在一次回调里。约定详见 multi-tenant.md §4.2、
#    langgraph-1x-api.md §5.4（那是本 skill 的隔离约定，不是 LangGraph 的版本要求）。
resp = await a2a_connector.send_task(skill, payload, callback_url=settings.a2a_callback_url)
if resp.status == "TASK_STATE_SUBMITTED":            # 异步：本轮直接结束
    return {"waiting_task_id": resp.task_id}          # 路由到 END；状态已在 PostgresSaver 里
# 同步 TASK_STATE_COMPLETED → 直接带 artifact 继续流程
# ⚠️ 取值是 v1.0 的 TASK_STATE_* 写法，不是 v0.3 的 "submitted"（见本节顶部说明）

# 恢复（callback.py，HMAC 校验通过后）
# ⚠️ source_trace_id 单独用还不够：回调端必须先从签名载荷里取出 tenant_id 再拼。
thread_id = f"{task.tenant_id}:{task.source_trace_id}"   # 租户维度隔离 + 全链路同一个 trace
# ⚠️ 三要点缺一不可：pool + autocommit=True + 显式 setup()（漏掉会静默失败，详见 langgraph-1x-api.md §5.1）
pool = AsyncConnectionPool(conninfo=settings.postgres_dsn, min_size=2, max_size=10,
                           kwargs={"autocommit": True, "row_factory": dict_row})
await pool.open()
saver = AsyncPostgresSaver(pool)
await saver.setup()                                  # 首次运行建表；之后幂等
graph = build_graph(checkpointer=saver)
result = await graph.ainvoke(
    {"external_result": artifact},                   # 增量输入
    {"configurable": {"thread_id": thread_id}},      # PostgresSaver 自动取 checkpoint 续跑
)
```

要点：**trace_id 一份贯穿** HTTP → Graph → checkpoint → A2A → trace，检索恢复零映射；
但 **checkpoint 的 `thread_id` 一律写成 `{tenant_id}:{trace_id}`**——trace_id 是"哪一次请求"，
tenant_id 是"谁的数据"，两者缺一，跨租户隔离就只写在文档里、没落在键上
（`multi-tenant.md` §4.2 为强制约定；LangGraph 本身不校验格式，写错了不会报错，只会静默撞键）。

⚠️ **恢复能否成功取决于持久化契约——这是本方案最脆弱的假设**（2026-09 核实）：LangGraph **不把边（edge）拓扑持久化进 checkpoint**，因此增改 `add_conditional_edges` 对在途线程安全，但**重命名或删除节点 / state 字段会导致挂起线程永久无法恢复**（详见 `langgraph-1x-api.md` §6.7）。两条硬约束：

- **节点名与 state 键是持久化契约的一部分**——重命名前必须先排空所有在途挂起任务；
- **callback 侧必须实现"checkpoint 恢复失败"分支**（当前设计隐含假设恢复必然成功）——失败时应把 A2A 任务置为 `TASK_STATE_FAILED` 并告警，而非静默丢弃。

## 9. 重任务入队（[修订] ARQ 模式，api 与 Worker 只经 Redis+PG 通信）

```python
# tools/workers/tasks.py —— Worker 侧任务定义
from arq import create_pool
from arq.connections import RedisSettings

async def run_ocr_job(ctx, trace_id: str, file_path: str) -> dict:
    with tracer.start_as_current_span("worker.run_ocr"):    # 同 trace_id，跨进程连续
        result = await ocr_engine.recognize(file_path)
        await save_result_to_pg(trace_id, result)           # 结果落库，不进进程内存
        return {"status": "ok"}

class WorkerSettings:
    functions = [run_ocr_job]
    redis_settings = RedisSettings(host=..., port=...)
    max_jobs = 10; job_timeout = 300; health_check_interval = 30

# api / Agent 节点侧投递
redis = await create_pool(RedisSettings(...))
job = await redis.enqueue_job("run_ocr_job", trace_id, file_path, _job_id=f"ocr:{trace_id}")
# _job_id 幂等键：同 trace 重投不重复执行；结果轮询 GET /api/task/{job_id}
```

适用判断：预估耗时 >5s、或依赖外部系统响应时间不可控 → 入队；否则内联。

## 10. 指标（[修订] prometheus_client，禁自研文本导出）

```python
from prometheus_client import Counter, Histogram, Gauge

AGENT_CALLS = Counter("agent_calls_total", "Agent invocations", ["agent", "status"])
AGENT_LATENCY = Histogram("agent_latency_seconds", "Agent latency", ["agent"],
                          buckets=(0.1, 0.5, 1, 2, 5, 10, 30))   # 标准 bucket，p95 可算
QUEUE_DEPTH = Gauge("queue_depth", "Pending jobs", ["queue"])

# FastAPI 挂载（免 JWT，内网限访问）
from prometheus_client import make_asgi_app
app.mount("/metrics", make_asgi_app())
```

## 11. RAG 混合检索（RRF 融合）

```python
class HybridRetriever:
    async def hybrid_search(self, query: str, top_k: int = 5) -> list:
        dense = await self._milvus_search(query, top_k * 3)    # Milvus HNSW
        sparse = self._bm25.score(query, top_k * 3)            # BM25
        return self._rrf_fusion(dense, sparse, top_k)          # Reciprocal Rank Fusion
# Reranker 精排 → LLM 生成 → evidence 取 top-3 {source, excerpt, score}
# [修订] 必须配套 ingest 管道：documents/ 目录 → 解析 → 切分 → embedding → 入 Milvus
#        （增量按 content_hash 去重；重建索引入 Worker 队列执行）
```

## 12. 评测指标口径

- RAG：faithfulness（bigram Jaccard）/ answer_relevance（token overlap）/ context_recall（bigram overlap）；LLM Judge 可选
- Agent：task_success_rate / tool_accuracy / average_latency / average_step_count / intent_accuracy
- 综合评分权重：task_success 0.25 + tool_accuracy 0.15 + faithfulness 0.15 + answer_relevance 0.15 + context_recall 0.10 + intent_accuracy 0.10 + efficiency 0.10
- CLI：`python -m governance.evaluation.runner run|list|compare`（全量跑批走 Worker 队列）

## 13. LLM 缓存（性能优化，实测 18.4s → 0.26s）

`CachingChatOpenAI` 包装 BaseChatModel：按渲染消息哈希为键，**Redis 为主存储**（内存 TTL 仅作 Redis 不可用时的短暂回退，且必须有容量上限）。在构图处一处替换，覆盖全部 LLM 调用点。
