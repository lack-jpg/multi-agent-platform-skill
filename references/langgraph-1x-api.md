# LangGraph 1.x API 速查

> gov_AP 项目以 LangGraph 1.x 为基线。本文列出实际 API 签名、v0.x → v1.x 迁移差异、常见踩坑。

---

## 1. 核心概念与导入

```python
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver   # 官方 PostgresSaver
from langgraph.checkpoint.memory import MemorySaver                 # 单测用
```

---

## 2. StateGraph 构造

```python
from typing import TypedDict, Annotated
from operator import add
from langgraph.graph import add_messages

class State(TypedDict):
    messages: Annotated[list, add_messages]   # 消息列表：按 id 合并（见 §3.1）
    evidence: Annotated[list, add]            # 普通列表：拼接即可
    user_query: str                           # 标量字段默认覆盖更新

graph = StateGraph(State)
graph.add_node("supervisor", supervisor_node_func)
graph.add_node("policy", policy_node_func)

# 静态边
graph.add_edge(START, "supervisor")
graph.add_edge("policy", END)

# 条件边
graph.add_conditional_edges(
    "supervisor",
    routing_function,                    # (state) -> str
    {"intent": "intent", "policy": "policy", "end": END},
)

# 编译
app = graph.compile(checkpointer=saver)
```

---

## 3. State 字段的 reducer

### 3.1 `operator.add` 与 `add_messages` 不是一回事

> ⚠️ **更正**：本节曾写"0.x 用 `operator.add`、1.x 改用 `add`"——**这是虚构的**。
> LangGraph 从未导出过 `add`，`from langgraph.graph import add` 直接 ImportError
> （核实于 langgraph 1.1.6，`langgraph.graph` 只导出 `add_messages`）。
> `Annotated[list, reducer]` 的机制两代一致，**不存在这个版本差异**。
> 真正的区别是 **reducer 选哪个**：

| reducer | 来源 | 语义 |
|---|---|---|
| `operator.add` | Python 标准库 | 纯列表拼接，不认识 message id |
| `add_messages` | `langgraph.graph` | 按 message **id** 合并 |

行为差异（实测）：

| 场景 | `operator.add` | `add_messages` |
|---|---|---|
| 普通追加 | 拼接 | 拼接 |
| 新消息复用已有 id | 变成两条（旧 + 新） | 原地覆盖为一条 |
| 传 `RemoveMessage(id=...)` | 当成一条空消息追加 | 真删除该条 |

所以 **`messages` 字段必须用 `add_messages`**；非消息的普通列表
（`evidence` / `mcp_history` / `error_history`）用 `operator.add` 即可。

```python
from operator import add
from langgraph.graph import add_messages

class State(TypedDict):
    messages: Annotated[list, add_messages]   # 消息列表：按 id 合并
    evidence: Annotated[list, add]            # 普通列表：std lib operator.add
```

用错 `operator.add` 的后果不难发现却难定位：多轮对话里同 id 消息不断堆积
（token 越滚越大、成本悄悄上涨），且"删除某条记忆"静默失效——不报错，只是没删掉。

### 3.2 自定义 reducer（按 id 合并）

```python
def _task_plan_reducer(old: list, new: list) -> list:
    """按 id 合并：新 id 追加，已有 id 覆盖。"""
    merged = {t["id"]: t for t in (old or [])}
    for t in new or []:
        merged[t["id"]] = t
    return list(merged.values())

class State(TypedDict):
    task_plan: Annotated[list, _task_plan_reducer]
```

### 3.3 替换语义的字段

有些字段（如 a2a_tasks）应该是"整表替换"而不是追加：

```python
class State(TypedDict):
    # 不加 reducer → 默认覆盖（替换语义）
    a2a_tasks: list[dict]

    # A2A 恢复时必须传完整列表，否则会丢旧任务
```

---

## 4. Node 函数签名

```python
async def policy_node(state: State) -> dict:
    """返回 dict 表示状态增量，会被 reducer 合并。"""
    result = await do_something(state["user_query"])
    return {
        "messages": [result],       # 合并（Annotated[list, add_messages]，按 id）
        "evidence": [...],           # 追加
        "intent": "policy",         # 覆盖（标量）
    }
```

### 4.1 依赖注入模式（不要用全局变量）

```python
# 工厂函数注入依赖
def build_graph(llm=None, mcp_client=None, checkpointer=None):
    async def policy_node(state):
        # 闭包捕获依赖
        if mcp_client:
            result = await mcp_client.call_tool("policy_search", {...})
        else:
            result = await _stub_search(state["user_query"])
        return {"evidence": result}
    ...
```

### 4.2 config 与 runtime context

```python
# 调用时传 config
config = {
    "configurable": {
        "thread_id": "tenant_123:trace_abc",   # PostgresSaver 用此隔离
        "user_id": "u-456",
    }
}

# node 中读取
async def policy_node(state: State, config: RunnableConfig) -> dict:
    thread_id = config["configurable"]["thread_id"]
    user_id = config["configurable"].get("user_id")
    ...
```

**注意**：v1.x 的 node 函数签名必须是 `(state)` 或 `(state, config)`，不能多个位置参数。

---

## 5. Checkpointer

### 5.1 官方 PostgresSaver

```python
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row

pool = AsyncConnectionPool(
    conninfo="postgresql://user:pwd@host:5432/db",
    min_size=2, max_size=10,
    kwargs={"autocommit": True, "row_factory": dict_row},   # 两个都必须
)
await pool.open()

saver = AsyncPostgresSaver(pool)
await saver.setup()    # 首次运行建表；之后幂等

app = graph.compile(checkpointer=saver)
```

**为什么必须 `autocommit=True`**：`setup()` 的建表 DDL 在非 autocommit 连接上会**静默失败**——不报错，但表不存在，直到第一次 `invoke` 才炸。

**为什么必须 `row_factory=dict_row`**：saver 内部按键取值，缺失会报 KeyError / TypeError。

**依赖是 Psycopg 3**（`psycopg>=3.2.0` + `psycopg-pool`），**不是** asyncpg / psycopg2。业务库若用 asyncpg，两套连接并存即可，互不影响。

**pool 与 `pipeline=True` 不兼容**：同时使用会抛 `ValueError`。

### 5.2 MemorySaver（仅用于单测）

```python
from langgraph.checkpoint.memory import MemorySaver

saver = MemorySaver()
app = graph.compile(checkpointer=saver)
# 禁止用于生产（重启即丢状态）
```

### 5.3 恢复执行

```python
config = {"configurable": {"thread_id": "tenant_123:trace_abc"}}
result = await app.ainvoke(state, config=config)
# 第二次调用相同 thread_id 时自动从 checkpoint 恢复
```

### 5.4 thread_id 命名（本 skill 的约定，**不是**版本差异）

> ⚠️ 本小节曾题为"v1.x 重要变化：thread_id 命名规范"——**标题是错的**。
> LangGraph 不校验 `thread_id` 格式，两代都接受任意字符串（实测 1.1.6：含冒号、空格、
> 中文、500 字符均可），**不存在"v1.x 才要求加 tenant 前缀"**。这是本 skill 的多租户
> 隔离约定，与版本无关。同文件 §10 已并列说明。

```python
# ❌ 不用：thread_id 直接用 trace_id，跨租户可能撞键
config = {"configurable": {"thread_id": "trace_abc"}}

# ✅ 约定：thread_id 包含 tenant_id 防跨租户
config = {"configurable": {"thread_id": "tenant_123:trace_abc"}}
```

前缀格式由本 skill 规定，LangGraph 不强制；换分隔符（如 `/`）不影响正确性，
但全项目必须统一。详见 `multi-tenant.md` §4。

---

## 6. 常见踩坑

### 6.1 列表字段忘记配 reducer（或配错 reducer）

```python
# ❌ 错误：重放时 messages 会丢失或翻倍
class State(TypedDict):
    messages: list

# ⚠️ 半错：配上 reducer 了，但 messages 用 operator.add 会按"拼接"处理，
#    同 id 消息堆积、RemoveMessage 静默失效（详见 §3.1）
class State(TypedDict):
    messages: Annotated[list, add]

# ✅ 正确：消息列表用 add_messages，普通列表用 operator.add
class State(TypedDict):
    messages: Annotated[list, add_messages]
    evidence: Annotated[list, add]
```

### 6.2 node 返回 None

```python
# ❌ 错误：return None 等于"清空 state"
async def bad_node(state):
    return

# ✅ 正确：返回空 dict 表示无更新
async def good_node(state):
    return {}
```

### 6.3 条件边函数必须返回合法 key

```python
# ❌ 错误：返回的 key 不在 path_map 中
graph.add_conditional_edges(
    "supervisor",
    lambda s: "policy",
    {"policy": "policy_node", "end": END},   # 少了 policy 对应 key
)
# → 抛 KeyError 或卡住

# ✅ 正确：所有返回 key 都在 path_map 中
graph.add_conditional_edges(
    "supervisor",
    lambda s: "policy",
    {"policy": "policy", "intent": "intent", "end": END},
)
```

### 6.4 Checkpoint 未 setup

```python
saver = AsyncPostgresSaver(pool)
# ❌ 忘记 setup() → 第一次 invoke 报 "relation does not exist"
await saver.setup()
```

### 6.5 并发调用相同 thread_id

```python
# ❌ 错误：同一 thread_id 并发 invoke 会互相覆盖 checkpoint
await asyncio.gather(
    app.ainvoke(state, config={"configurable": {"thread_id": "t-1"}}),
    app.ainvoke(state, config={"configurable": {"thread_id": "t-1"}}),
)

# ✅ 正确：每个请求用唯一 thread_id
```

### 6.6 自研 checkpointer

```python
# ❌ 严禁：自研 BaseCheckpointSaver 抽象（详见 ADR-0003）
# ✅ 必须：直接用官方 AsyncPostgresSaver
```

### 6.7 节点 / state 字段重命名 → 挂起线程永久无法恢复（最隐蔽的坑）

LangGraph **不把边（edge）拓扑持久化进 checkpoint**。官方向后兼容文档明确：

- ✅ 增改 `add_conditional_edges` 对在途线程**安全**（边不落盘）
- ❌ **重命名或删除节点、state 字段会导致已挂起线程恢复失败**

这对 A2A 挂起/恢复（`patterns.md` §8）与 human-in-the-loop 是致命的：一次看似无害的节点重命名，会让所有处于挂起状态的任务永久卡死——**而且报错发生在很久之后的 callback 恢复时**，排查成本极高。

```python
# ❌ 危险：把 policy 改名为 policy_v2
graph.add_node("policy_v2", policy_node)
# → 所有挂起在 "policy" 节点的线程再也无法恢复
```

**约束**：
- 节点名与 state 键是**持久化契约的一部分**——重命名前必须排空在途挂起任务；
- callback 侧必须实现"恢复失败"分支（置任务 `failed` + 告警），不能假定恢复必然成功；
- 破坏性改名应走 ADR（`adr-template.md`）。

---

## 7. 1.x 新特性（升级建议）

| 特性 | 用途 | 是否启用 |
|---|---|---|
| `astream_events` | 流式输出 token 级别事件 | 推荐（用户体验好） |
| `get_state(config)` | 查询当前 thread 的状态 | 推荐（异步任务查询） |
| `update_state(config, values)` | 手动修改 state（A2A 恢复） | 推荐 |
| `interrupt_before` / `interrupt_after` | 人在环路（human-in-the-loop） | 按需 |
| 自定义 retry policy | 节点级重试 | 按需 |

### 7.1 流式输出示例

```python
async def stream_chat(user_query: str):
    config = {"configurable": {"thread_id": thread_id}}
    async for event in app.astream_events(
        create_initial_state(user_query, trace_id),
        config=config,
        version="v2",
    ):
        if event["event"] == "on_chat_model_stream":
            yield {"type": "token", "content": event["data"]["chunk"].content}
```

### 7.2 人工审批节点

```python
graph.add_node("human_review", human_review_node)
graph.add_edge("policy", "human_review")
graph.add_edge("human_review", "workflow")

# 编译时：
app = graph.compile(
    checkpointer=saver,
    interrupt_before=["human_review"],   # 在此节点前暂停
)

# 恢复：
await app.ainvoke(None, config=config)   # 用户点"通过"后调用
```

---

## 8. 与 LangChain 1.x 关系

LangGraph 1.x 与 LangChain 1.x **解耦**：

```python
# 可以只用 LangGraph，不引入 LangChain
from langgraph.graph import StateGraph

# 也可以混用 LangChain 组件
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

llm = ChatOpenAI(model="gpt-4o")
prompt = ChatPromptTemplate.from_messages([...])
# 在 node 里组合使用
```

---

## 9. 官方文档引用

> ⚠️ **文档站已迁移（2026-09 核实）**：LangChain 官方文档现以 `docs.langchain.com` 为准，旧 `langchain-ai.github.io/langgraph/` 可能重定向或失效。
> 其中 **`backward-compatibility` 页是 §6.7 边拓扑约束的出处**，接口契约类问题优先查它。

- LangGraph v1 发布说明：https://docs.langchain.com/oss/python/releases/langgraph-v1
- 0.x → 1.x 迁移指南：https://docs.langchain.com/oss/python/migrate/langgraph-v1
- 向后兼容 / 接口契约（**§6.7 出处**）：https://docs.langchain.com/oss/python/langgraph/backward-compatibility
- Checkpointer 参考：https://reference.langchain.com/python/langgraph/checkpoint/postgres
- LangGraph 主页（旧站，可能重定向）：https://langchain-ai.github.io/langgraph/
- Human-in-the-loop：https://langchain-ai.github.io/langgraph/concepts/human_in_the_loop/

---

## 10. 升级检查清单

从 LangGraph 0.x → 1.x 必须做的改动：

- [ ] `messages` 字段的 reducer 换成 `add_messages`（见 §3.1。
      ⚠️ **不存在**「`operator.add` → `add`」这种改动——`langgraph.graph` 从未导出 `add`，
      本清单曾这么写，是虚构的）
- [ ] checkpointer 升级到官方 `AsyncPostgresSaver`
- [ ] node 函数签名规范（只接受 `state` / `(state, config)`）
- [ ] 条件边 path_map 必须覆盖所有返回 key
- [ ] 移除所有自研 checkpointer 实现
- [ ] 清理**既有代码里**的 `AgentExecutor`（该类在 LangChain 1.x 中已被移除、import 不到，
      见 `FACTS.md` F-004）。⚠️ 这是迁移项，不是对**新代码**的约束——新代码里根本写不出来
- [ ] 升级测试到 v1.x API —— **async 节点必须用 `ainvoke`**，同步 `.invoke()` 会
      `TypeError: No synchronous function provided`
- [ ] 跑回归 golden 用例集

**以下不是 0.x → 1.x 的 API 变更，是本 skill 的项目约定**（曾混在上面的清单里，容易被读成版本差异）：

- **thread_id 加 tenant 前缀**（如 `tenant_123:trace_abc`）—— LangGraph 两代都接受任意字符串，
  不存在"v1.x 才要求"。这是多租户隔离约定，见 `conventions.md §9`、`multi-tenant.md`。

---

## 11. 与本 Skill 其他文件的交叉引用

- Checkpointer 选型依据：见 `adr-template.md` §2 ADR-0003
- A2A 挂起恢复：见 `patterns.md` §8
- 多租户 thread_id：见 `multi-tenant.md` §4
- Checkpoint 监控：见 `grafana-dashboards.md` §2.7
- Checkpoint 性能告警：见 `slo.md` §3 P1 告警