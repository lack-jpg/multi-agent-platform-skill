"""Scaffold a multi-agent platform project following the gov_AP six-layer architecture.

Usage:
    python scaffold_project.py --name my_agent_app --domain finance [--dir /path/to/parent]

Generates a directory skeleton + a **runnable** stub-mode core chain + CLAUDE.md +
.env.example + requirements/ + CI per the multi-agent-platform skill conventions.

验收标准（不要放宽）：生成后执行下面三步必须全绿——
    cd <project> && pip install -r requirements/requirements.txt -r requirements/requirements-dev.txt
    python -m orchestration.langgraph.graph      # graph 冒烟测试
    pytest tests/ -q                             # 离线测试，无需 LLM/DB
回归：`python scripts/test_scaffold.py` 会自动跑这三步（依赖缺失时降级为静态检查）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Windows GBK 控制台下输出 ✓ ⚠ ✅ 这类符号会 UnicodeEncodeError（中文本身 GBK 编得出，
# 崩的是符号）。**scripts/ 下每个可执行脚本都要有这一段**——CI 跑在 Linux 上永远发现不了。
# stdout 与 stderr 都要处理——错误信息走 stderr，只改 stdout 会让报错反而乱码。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass

INIT = '"""{pkg} package."""\n'

# Python packages: get __init__.py (parents are expanded automatically)
PY_PACKAGES = [
    "agents/supervisor",
    "agents/intent",
    "agents/policy",
    "agents/governance",
    "orchestration/langgraph",
    "tools/mcp/servers/policy_server",
    "tools/a2a/mock_agents",
    "tools/workers",
    "rag",
    "governance/evaluation",
    "database/migrations/versions",
    "backend/api",
    "backend/middleware",
    "backend/services",
    "prompts",
    "tests",
]

# Plain directories: NOT Python packages (config / data / artifacts), no __init__.py
PLAIN_DIRS = [
    "cases",             # evaluation case JSON files
    "frontend/pages",    # Streamlit demo pages (optional)
    "models",            # local model weights (gitignored)
    "deploy/prometheus/rules",     # 告警规则（详见 references/grafana-dashboards.md §3）
    "deploy/grafana/dashboards",   # Dashboard JSON（详见 references/grafana-dashboards.md）
    "deploy/grafana/provisioning", # Grafana 数据源/看板自动加载配置
    "deploy/k8s",
    "requirements",
    "logger",            # log output dir
    "docs/adr",          # ADR 文档目录（详见 references/adr-template.md）
]


def _with_parents(dirs: list[str]) -> list[str]:
    """Expand package dirs to include every ancestor dir (complete __init__.py chains)."""
    out: set[str] = set()
    for d in dirs:
        parts = d.split("/")
        for i in range(1, len(parts) + 1):
            out.add("/".join(parts[:i]))
    return sorted(out)


CONFIG_PY = '''"""Application settings (pydantic-settings). All config comes from .env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_name: str = "__APP_NAME__"
    debug: bool = False

    # LLM (None -> stub mode)
    llm_api_key: str | None = None
    llm_api_url: str = "http://localhost:8000/v1"
    llm_model: str = "default"

    # Storage
    postgres_host: str = "localhost"
    postgres_port: int = 12221
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"
    postgres_db: str = "__DB_NAME__"
    redis_host: str = "localhost"
    redis_port: int = 12201
    milvus_host: str = "localhost"
    milvus_port: int = 12211

    # Security
    jwt_secret_key: str = "change-me"
    cors_origins: str = "localhost:12345,localhost:12421"

    # Queue (heavy jobs run in worker process, not in request thread)
    redis_queue_db: int = 1

    # Agent runtime safeguards
    runtime_max_steps: int = 10
    runtime_timeout: float = 30.0
    runtime_max_retries: int = 3

    @property
    def postgres_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
'''

STATE_PY = '''"""AgentState: shared TypedDict for all LangGraph nodes."""
from operator import add
from typing import Annotated, TypedDict

from langgraph.graph import add_messages


def _task_plan_reducer(old: list, new: list) -> list:
    """Merge task plans by task id (append new ids, overwrite existing)."""
    merged = {t["id"]: t for t in (old or [])}
    for t in new or []:
        merged[t["id"]] = t
    return list(merged.values())


class AgentState(TypedDict):
    # Scalar fields: overwrite semantics
    trace_id: str
    user_query: str
    intent: str
    final_answer: str
    risk_level: str
    error: str | None
    retry_count: int
    # List fields: reducer semantics
    task_plan: Annotated[list, _task_plan_reducer]
    messages: Annotated[list, add_messages]   # 消息列表必须用 add_messages（按 id 合并）
    mcp_history: Annotated[list, add]
    evidence: Annotated[list, add]
    error_history: Annotated[list, add]


def create_initial_state(user_query: str, trace_id: str) -> AgentState:
    return AgentState(
        trace_id=trace_id,
        user_query=user_query,
        intent="",
        final_answer="",
        risk_level="low",
        error=None,
        retry_count=0,
        task_plan=[],
        messages=[],
        mcp_history=[],
        evidence=[],
        error_history=[],
    )
'''

GOVERNANCE_METRICS_PY = '''"""Agent call metrics.

⚠️ 这是**骨架实现**，不是生产方案。铁律 #5（生态优先）要求生产用
`prometheus_client` 的 Counter/Histogram（标准 bucket，Grafana 才能算 p95）；
铁律 #4 要求指标不进进程内单例——多副本各自计数会漂移，必须由 Prometheus
抓取后聚合。Phase 4 替换方式见 references/patterns.md §10。

此处保留内存实现，只为让 graph 在零外部依赖下可跑（开发 / CI）。
指标是**有界**的（只存计数与聚合值，不存样本列表）——内存缓存必须有界。
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock


@dataclass
class _AgentStat:
    """有界聚合：不保留延迟样本列表，避免无界增长。"""

    ok: int = 0
    error: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0

    @property
    def avg_ms(self) -> float:
        calls = self.ok + self.error
        return self.total_ms / calls if calls else 0.0


class MetricsRegistry:
    def __init__(self) -> None:
        self._stats: dict[str, _AgentStat] = {}
        self._lock = Lock()

    def record(self, agent: str, success: bool, latency_ms: float) -> None:
        with self._lock:
            stat = self._stats.setdefault(agent, _AgentStat())
            if success:
                stat.ok += 1
            else:
                stat.error += 1
            stat.total_ms += latency_ms
            stat.max_ms = max(stat.max_ms, latency_ms)

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {
                name: {
                    "ok": s.ok,
                    "error": s.error,
                    "avg_ms": round(s.avg_ms, 2),
                    "max_ms": round(s.max_ms, 2),
                }
                for name, s in self._stats.items()
            }


REGISTRY = MetricsRegistry()


def record_agent_call(
    agent: str, success: bool, latency_ms: float, trace_id: str = ""
) -> None:
    """记录一次 Agent 调用。成功与失败都必须记（铁律 #2）。"""
    REGISTRY.record(agent, success, latency_ms)
'''

GOVERNANCE_TRACE_PY = '''"""Trace spans with ContextVar (coroutine-safe).

⚠️ 骨架实现。生产用 OpenTelemetry SDK **真接入**（挂 NoOp 不算接入，见
references/architecture.md §4）；此处在内存里建 span 并打印，让子 span 能继承
parent_span_id，保证 trace_id 贯穿 HTTP → Graph → Worker → A2A。

Phase 4 替换：把 `_finish` 里的落库改为 OTel exporter + PostgreSQL trace 表。
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from time import perf_counter
from uuid import uuid4

from loguru import logger


class SpanKind(str, Enum):
    AGENT = "agent"
    TOOL = "tool"
    LLM = "llm"


@dataclass
class Span:
    name: str
    kind: SpanKind
    trace_id: str
    span_id: str = field(default_factory=lambda: uuid4().hex[:16])
    parent_span_id: str | None = None
    attributes: dict = field(default_factory=dict)
    start: float = field(default_factory=perf_counter)

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    @property
    def elapsed_ms(self) -> float:
        return (perf_counter() - self.start) * 1000


_current_span: ContextVar[Span | None] = ContextVar("current_span", default=None)


def current_trace_id() -> str:
    span = _current_span.get()
    return span.trace_id if span else ""


class AgentTracer:
    """子 span 自动继承 parent_span_id；ContextVar 保证协程安全。"""

    @staticmethod
    @contextlib.contextmanager
    def span(kind: SpanKind, name: str, trace_id: str = "") -> Iterator[Span]:
        parent = _current_span.get()
        span = Span(
            name=name,
            kind=kind,
            trace_id=trace_id or (parent.trace_id if parent else uuid4().hex),
            parent_span_id=parent.span_id if parent else None,
        )
        token = _current_span.set(span)
        try:
            yield span
        finally:
            _current_span.reset(token)
            logger.debug(
                "span {} kind={} trace_id={} elapsed_ms={:.1f} attrs={}",
                span.name,
                span.kind.value,
                span.trace_id,
                span.elapsed_ms,
                span.attributes,
            )
            # TODO Phase 4: 写入 PostgreSQL trace 表（governance/trace.py）
'''

GRAPH_PY = '''"""LangGraph StateGraph factory (L2 orchestration).

依赖注入：所有依赖 Optional，缺失时走**显式降级**（结果标注 mode=stub），
因此 graph 在零基础设施下即可运行（开发 / CI）。

节点在 build_graph() **内部**定义、依赖经闭包注入 —— 所以节点签名只有
`(state)` 或 `(state, config)`，**不接收** mcp_client 这类参数。测试方式见
skill 的 references/testing-matrix.md §2.1。
"""
from __future__ import annotations

import asyncio
import time

from langgraph.graph import END, START, StateGraph
from loguru import logger

from governance.metrics import record_agent_call
from governance.trace import AgentTracer, SpanKind
from orchestration.langgraph.state import AgentState, create_initial_state

CONFIG = {"configurable": {"thread_id": "smoke"}}


def build_graph(llm=None, mcp_client=None, a2a_connector=None, checkpointer=None):
    """构建并编译 graph。纯函数语义：无全局副作用，可随时重建。"""
    graph = StateGraph(AgentState)

    async def supervisor_node(state: AgentState) -> dict:
        """规划 + 路由。TODO Phase 1: SupervisorAgent.orchestrate(state)。"""
        t0 = time.perf_counter()
        success = False
        try:
            with AgentTracer.span(SpanKind.AGENT, "supervisor", state["trace_id"]) as span:
                if not state.get("intent"):
                    span.set_attribute("route", "intent")
                    return {}                      # 无 intent -> 先分类
                span.set_attribute("route", "policy")
                success = True
                return {"task_plan": [{"id": "t1", "agent": "policy", "status": "pending"}]}
        finally:
            record_agent_call(
                "supervisor", success, (time.perf_counter() - t0) * 1000, state["trace_id"]
            )

    async def intent_node(state: AgentState) -> dict:
        """TODO Phase 2: 三级分类（微调小模型 -> 关键词 -> LLM fallback）。"""
        t0 = time.perf_counter()
        success = False
        try:
            with AgentTracer.span(SpanKind.AGENT, "intent", state["trace_id"]) as span:
                query = state["user_query"]
                intent = "policy" if ("政策" in query or "怎么" in query) else "chitchat"
                span.set_attribute("intent", intent)
                success = True
                return {"intent": intent}
        finally:
            record_agent_call(
                "intent", success, (time.perf_counter() - t0) * 1000, state["trace_id"]
            )

    async def policy_node(state: AgentState) -> dict:
        """TODO Phase 2: 经 MCP 检索 + RAG 混合检索。"""
        t0 = time.perf_counter()
        success = False
        try:
            with AgentTracer.span(SpanKind.AGENT, "policy", state["trace_id"]) as span:
                if mcp_client is None:
                    # 显式降级：标注 mode=stub。生产禁止静默 mock（铁律 #3）。
                    logger.warning("mcp_client 不可用，policy 检索降级为 stub")
                    span.set_attribute("mode", "stub")
                    success = True
                    return {"evidence": [{"source": "stub", "excerpt": "", "score": 0.0,
                                          "mode": "stub"}]}
                result = await mcp_client.call_tool(
                    "search_policy", {"query": state["user_query"]}
                )
                span.set_attribute("mode", "mcp")
                span.set_attribute("evidence_count", len(result.get("evidence", [])))
                success = True
                return {"evidence": result.get("evidence", [])}
        finally:
            record_agent_call(
                "policy", success, (time.perf_counter() - t0) * 1000, state["trace_id"]
            )

    async def governance_node(state: AgentState) -> dict:
        """旁路治理，不参与业务回答。TODO Phase 4: 输出护栏 + PII 脱敏。"""
        t0 = time.perf_counter()
        success = False
        try:
            with AgentTracer.span(SpanKind.AGENT, "governance", state["trace_id"]) as span:
                answer = state.get("final_answer") or f"[stub] intent={state.get('intent', '')}"
                span.set_attribute("risk_level", "low")
                success = True
                return {"final_answer": answer, "risk_level": "low"}
        finally:
            record_agent_call(
                "governance", success, (time.perf_counter() - t0) * 1000, state["trace_id"]
            )

    def route_after_supervisor(state: AgentState) -> str:
        if not state.get("intent"):
            return "intent"
        if state.get("task_plan"):
            return "policy"
        return "governance"

    graph.add_node("supervisor", supervisor_node)
    graph.add_node("intent", intent_node)
    graph.add_node("policy", policy_node)
    graph.add_node("governance", governance_node)

    graph.add_edge(START, "supervisor")
    graph.add_edge("intent", "supervisor")          # 意图识别后必回 supervisor 二次规划
    graph.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        {"intent": "intent", "policy": "policy", "governance": "governance"},
    )
    graph.add_edge("policy", "governance")
    graph.add_edge("governance", END)
    return graph.compile(checkpointer=checkpointer)


def create_default_graph():
    """无任何依赖的 stub 图（开发 / CI 用，见 references/architecture.md L2）。"""
    return build_graph()


if __name__ == "__main__":
    # 冒烟测试：节点是 async 的，必须用 ainvoke（同步 invoke 会 TypeError）
    _app = create_default_graph()
    _result = asyncio.run(
        _app.ainvoke(create_initial_state("smoke test", "trace-0"), CONFIG)
    )
    assert _result["final_answer"], "最终回答为空"
    assert _result["intent"], "意图未分类"
    print(f"graph smoke test ok: intent={_result['intent']} answer={_result['final_answer']}")
'''

MAIN_PY = '''"""FastAPI application factory (L1 access layer).

Stub-first：graph 在无 LLM/MCP/DB 时也能跑，所以本服务零基础设施即可启动。

⚠️ 中间件顺序（FastAPI 后注册先执行，不可乱序）见
references/architecture.md L1：CORS → Tracing → RBAC → Auth(JWT) → RequestLogging。
本骨架只挂了最基本的 CORS，Phase 1 补齐 JWT + RBAC。
"""
from __future__ import annotations

from functools import lru_cache
from uuid import uuid4

from fastapi import FastAPI
from pydantic import BaseModel, Field

from backend.config import get_settings
from orchestration.langgraph.graph import create_default_graph
from orchestration.langgraph.state import create_initial_state


class ChatRequest(BaseModel):
    user_query: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    answer: str
    intent: str
    risk_level: str
    trace_id: str


@lru_cache
def get_graph():
    """进程内缓存一个**不可变编译图**（build_graph 保持纯函数语义）。"""
    return create_default_graph()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)

    @app.get("/health")
    async def health() -> dict:
        """健康检查：供容器 healthcheck / K8s livenessProbe 使用。"""
        return {"status": "ok", "app": settings.app_name}

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest) -> ChatResponse:
        # TODO Phase 1: 此处之前插入 输入护栏（GuardrailRunner.run_input，
        #     在 graph.ainvoke **之前** 阻断，省 Token），见 patterns.md §6
        # TODO Phase 1: JWT + RBAC 中间件
        trace_id = uuid4().hex
        state = create_initial_state(req.user_query, trace_id)
        result = await get_graph().ainvoke(
            state, {"configurable": {"thread_id": trace_id}}
        )
        return ChatResponse(
            answer=result.get("final_answer", ""),
            intent=result.get("intent", ""),
            risk_level=result.get("risk_level", "low"),
            trace_id=trace_id,
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=12401)
'''

TEST_GRAPH_PY = '''"""Smoke tests: 图在 stub 模式下可构建、可运行、可降级。"""
from __future__ import annotations

from governance.metrics import REGISTRY
from orchestration.langgraph.graph import build_graph, create_default_graph
from orchestration.langgraph.state import AgentState, create_initial_state

CONFIG = {"configurable": {"thread_id": "t-unit"}}


async def test_stub_mode_runs():
    """节点是 async 的：必须用 ainvoke 装配后测试（见 testing-matrix.md §2.1）。"""
    graph = create_default_graph()
    result = await graph.ainvoke(create_initial_state("生育津贴怎么领？", "t-unit"), CONFIG)
    assert result["trace_id"] == "t-unit"
    assert result["intent"] == "policy"
    assert result["final_answer"]


async def test_explicit_degradation_without_mcp():
    """不注入 mcp_client 时必须**显式降级**（mode=stub），禁止静默 mock。"""
    graph = build_graph(mcp_client=None)
    result = await graph.ainvoke(create_initial_state("政策咨询", "t-degrade"), CONFIG)
    evidence = result["evidence"]
    assert evidence and evidence[0]["mode"] == "stub"


async def test_agent_calls_are_recorded():
    """铁律 #2：每次 Agent 调用都要记指标（成功与失败都记）。"""
    graph = create_default_graph()
    await graph.ainvoke(create_initial_state("你好", "t-metrics"), CONFIG)
    snapshot = REGISTRY.snapshot()
    assert {"supervisor", "intent", "policy", "governance"} <= set(snapshot)
    assert all(v["ok"] >= 1 for v in snapshot.values())


def test_typed_state_has_reducers():
    """列表字段必须配 reducer，否则 checkpoint 重放会翻倍（ARCH002）。

    注意 get_type_hints 默认 include_extras=False，会**剥掉** Annotated——
    必须显式传 True，否则本测试永远通过（假绿）。
    """
    from typing import get_args, get_type_hints

    hints = get_type_hints(AgentState, include_extras=True)
    for field in ("task_plan", "messages", "mcp_history", "evidence", "error_history"):
        annotation = hints[field]
        assert hasattr(annotation, "__metadata__"), f"{field} 缺少 Annotated reducer"
        assert callable(get_args(annotation)[1]), f"{field} 的 reducer 不可调用"


def test_messages_reducer_is_add_messages():
    """messages 必须用 add_messages，不能用 operator.add。

    operator.add 是纯拼接、不认识 message id：同 id 消息会堆积成两条，
    RemoveMessage(id=...) 会被当成一条空消息追加（删除记忆静默失效）。
    详见 skill 的 references/langgraph-1x-api.md §3.1。
    """
    from operator import add as operator_add
    from typing import get_args, get_type_hints

    from langgraph.graph import add_messages

    reducer = get_args(get_type_hints(AgentState, include_extras=True)["messages"])[1]
    assert reducer is add_messages
    assert reducer is not operator_add
'''

PYTEST_INI = '''[pytest]
asyncio_mode = auto
testpaths = tests
markers =
    e2e: 需要真实 LLM / DB（CI 默认不跑，见 testing-matrix.md §7）
    contract: 契约测试
'''

CI_YML = '''name: ci

on:
  push:
    branches: [main, master]
  pull_request:

jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: |
          pip install -r requirements/requirements.txt
          pip install -r requirements/requirements-dev.txt

      # 零依赖、最快、最该早失败：架构违规先于测试暴露
      - name: Architecture conformance
        run: python scripts/check_architecture.py .

      - name: Lint
        run: ruff check .

      # 离线测试：stub 模式，不需要 LLM key 与 DB
      - name: Unit / integration tests
        run: pytest tests/ -m "not e2e and not contract" --cov=. --cov-report=term-missing
'''

CLAUDE_MD = '''# CLAUDE.md — __APP_NAME__（多智能体平台，六层架构）

> Runtime: Python 3.12+ | FastAPI | LangChain/LangGraph 1.x | MCP | A2A | RAG | AgentOps
> 架构细节见 multi-agent-platform skill 的 `references/`（架构 / 规范 / 模式三篇必读）。

## 五条核心铁律（违反即返工）

1. **Agent 优先**：流程由 Supervisor → Planner → Router → Specialist → Tool Calling 驱动。
   禁止 `if user_input=="xxx": call_function()`。
2. **所有 Agent 必须可观测**：每次调用记录 trace_id / agent_name / input / output /
   latency / token_usage / tool_calls，**成功与失败都要记**。禁止裸 `agent.run()`。
3. **工具必须标准化**：Agent 禁止直接 import 业务代码，必须走
   MCP Client → MCP Gateway(JWT+RBAC) → MCP Server → Business Service；
   Prompt 禁止硬编码，必须走 Prompt Registry 版本化。
4. **运行时必须无状态**：进程内禁止持有业务可变状态（checkpoint / 缓存 / 任务 / 指标
   全部外置 PG/Redis/prometheus_client）；api 与 worker 可多副本水平扩展。
5. **生态优先，自研收敛**：checkpointer 用官方 PostgresSaver、指标用 prometheus_client、
   追踪用 OTel SDK；自研名额仅一个（guardrail），其余须写 ADR 说明理由。

## 六条配套铁律（企业级 P0）

- **决策可追溯**：架构选型 / 铁律修改 / 生态件引入必须写 ADR（`docs/adr/`）
- **租户隔离前置**：建表第一天带 `tenant_id`，查询强制过滤，缓存键加 tenant 前缀
- **成本可观察**：每次 LLM 调用记 token + 模型 + 租户，按租户配额告警
- **安全分两层**：输入护栏前置 + 工具结果二次护栏（间接注入防不住直接护栏）
- **有承诺必留证据**：SLO 必须配套告警 + Postmortem
- **Prompt 是代码**：版本化 + A/B + 评测准入门槛

## 分层（依赖只能自上而下，禁止反向）

```
L1 backend/          FastAPI 接入（JWT+RBAC+trace_id+CORS白名单）
L2 orchestration/    LangGraph StateGraph 编排（state/graph/edges/runtime/checkpointer）
L3 agents/           专业 Agent（supervisor/intent/policy/governance + 业务域 Agent）
L4 tools/mcp/        MCP 工具协议（schema/client/gateway/servers/）
L5 tools/a2a/        A2A 跨域协同（connector/task/callback/HMAC）
L6 governance/       AgentOps 治理（trace/guardrail/pii/metrics/evaluation，旁路不答业务）
```

依赖方向：`backend → orchestration → agents → tools → rag/governance/database`

## 架构一致性校验（提交前必跑）

```bash
python scripts/check_architecture.py .          # 11 条规则，exit code 可接 CI
python scripts/check_architecture.py --list-rules   # 看规则清单
```

覆盖：Agent 直连业务层、自研 checkpointer、列表字段缺 reducer、静默吞异常、
硬编码 Prompt、按输入硬编码分支、async 内同步阻塞、print 代替 logger、节点未接可观测性。

确需例外时在同行加注释 `# arch: ignore[ARCH002]` 并写明原因——**不要**用 `--ignore` 关掉整条规则。
改动规则后跑 `python scripts/check_architecture.py --self-test`。

## 禁止清单

- 只许 StateGraph / Node / Edge / Checkpointer（`AgentExecutor` 在 LangChain 1.x 中已不存在，无从使用）
- `except Exception: pass`（静默吞异常）
- Agent 直连业务代码；硬编码 Prompt；静默 mock
- 列表型 state 字段不加 reducer 直接覆盖（checkpoint 重放会翻倍）
- 在请求线程里做耗时 >5s 的工作（必须入 ARQ 队列）

## 编码要求

async 优先；全量 type hint + docstring；配置走 pydantic-settings（禁止散落 os.getenv）；
每个核心模块内置 `python -m <module>` 冒烟测试；pytest 离线可跑（`-m "not e2e and not contract"`）。

## 端口规范（组内 +10 步进）

MCP 12001/12011/12021/12031 · A2A 12101/12111 · Redis/Milvus/PG 12201/12211/12221 ·
前端 12345 · API/Prom/Grafana/Alert 12401/12411/12421/12431
'''

ENV_EXAMPLE = '''# App
APP_NAME={app_name}
DEBUG=false

# LLM (leave empty to run in stub mode)
LLM_API_KEY=
LLM_API_URL=http://localhost:8000/v1
LLM_MODEL=default

# PostgreSQL
POSTGRES_HOST=localhost
POSTGRES_PORT=12221
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DB={db_name}

# Redis / Milvus
REDIS_HOST=localhost
REDIS_PORT=12201
MILVUS_HOST=localhost
MILVUS_PORT=12211

# Security
JWT_SECRET_KEY=change-me
CORS_ORIGINS=localhost:12345,localhost:12421
# A2A_HMAC_SECRET=change-me

# Agent runtime safeguards
RUNTIME_MAX_STEPS=10
RUNTIME_TIMEOUT=30
RUNTIME_MAX_RETRIES=3
'''

REQUIREMENTS = '''fastapi
uvicorn
pydantic
pydantic-settings
langchain>=1.0
langgraph>=1.0
langgraph-checkpoint-postgres
langchain-openai
mcp
httpx
arq
prometheus-client
sqlalchemy[asyncio]
asyncpg
alembic
redis
loguru
'''

REQUIREMENTS_DEV = '''pytest
pytest-asyncio
pytest-cov
ruff
mypy
'''


def write(path: Path, content: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"  + {path}")


def copy_architecture_checker(root: Path) -> None:
    """把架构校验器**复制进项目**——下游 CI 才能直接调用，不必依赖 skill 目录。"""
    src = Path(__file__).resolve().parent / "check_architecture.py"
    if not src.exists():
        print("  ! 未找到 check_architecture.py，跳过（生成项目的 CI 将缺少架构校验步骤）")
        return
    write(root / "scripts" / "check_architecture.py", src.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Scaffold multi-agent platform project")
    parser.add_argument("--name", required=True, help="project name (snake_case)")
    parser.add_argument("--domain", default="general", help="business domain (e.g. gov/finance/edu)")
    parser.add_argument("--dir", default=".", help="parent directory to create the project in")
    args = parser.parse_args()

    app_name = args.name.strip().replace("-", "_").replace(" ", "_")
    if not app_name.replace("_", "").isalnum() or app_name[0].isdigit():
        parser.error("--name must be a valid python-ish identifier (letters/digits/underscore)")

    root = Path(args.dir).resolve() / app_name
    if root.exists():
        parser.error(f"target already exists: {root}")
    root.mkdir(parents=True)

    print(f"Creating six-layer skeleton at {root} (domain: {args.domain})")
    for d in _with_parents(PY_PACKAGES):
        write(root / d / "__init__.py", INIT.format(pkg=d.replace("/", ".")))
    for d in PLAIN_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
        write(root / d / ".gitkeep", "")  # keep empty dirs in git
        print(f"  + {root / d}/")

    # Root files
    write(root / ".env.example", ENV_EXAMPLE.format(app_name=app_name, db_name=app_name))
    write(root / ".gitignore", "\n".join([
        "__pycache__/", "*.pyc", ".env", "models/*", "logger/*.log",
        ".pytest_cache/", ".ruff_cache/", ".coverage", "htmlcov/",
        "PLAN.md", "dist/", ".venv/", "\n",
    ]))
    write(root / "README.md", f"# {app_name}\n\n{args.domain} 多智能体平台（六层架构：LangGraph + MCP + A2A + RAG + AgentOps）。\n")
    write(root / "example.py", '"""File header template: Author / Date / Version / Task."""\n')
    write(root / "pytest.ini", PYTEST_INI)

    # L6 governance 骨架（graph 的可观测性依赖它，必须先有）
    write(root / "governance" / "metrics.py", GOVERNANCE_METRICS_PY)
    write(root / "governance" / "trace.py", GOVERNANCE_TRACE_PY)

    # Core chain (Phase 1) —— 生成即可运行，非 TODO 空壳
    write(root / "backend" / "config.py", CONFIG_PY.replace("__APP_NAME__", app_name).replace("__DB_NAME__", app_name))
    write(root / "backend" / "main.py", MAIN_PY)
    write(root / "orchestration" / "langgraph" / "state.py", STATE_PY)
    write(root / "orchestration" / "langgraph" / "graph.py", GRAPH_PY)

    # Tests + CI + 架构校验器
    write(root / "tests" / "test_graph.py", TEST_GRAPH_PY)
    write(root / ".github" / "workflows" / "ci.yml", CI_YML)
    copy_architecture_checker(root)

    # CLAUDE.md for AI coding tools
    write(root / "CLAUDE.md", CLAUDE_MD.replace("__APP_NAME__", app_name))

    # Requirements
    write(root / "requirements" / "requirements.txt", REQUIREMENTS)
    write(root / "requirements" / "requirements-dev.txt", REQUIREMENTS_DEV)

    print("\nNext steps（生成即可跑，先验证基线再进 Phase 1）：")
    print(f"  cd {root}")
    print("  1. pip install -r requirements/requirements.txt -r requirements/requirements-dev.txt")
    print("  2. python -m orchestration.langgraph.graph   # graph 冒烟测试")
    print("  3. pytest tests/ -q                          # 离线测试，无需 LLM/DB")
    print("  4. python scripts/check_architecture.py .    # 架构一致性校验")
    print("  5. uvicorn backend.main:app --port 12401     # 起服务，curl localhost:12401/health")
    print("  6. Phase 1: JWT/RBAC 中间件 + api/routes.py + agents/supervisor/*")
    print("  7. Phase 2: agents/intent + policy + rag/（含 ingest 摄取管道）")
    print("  8. Phase 3: tools/mcp/* servers; Phase 4: governance 护栏 + 评测 + cases/")
    print("\nP5（规模化前）必做：")
    print("  - 决定租户隔离策略：单租户部署 vs PG RLS + 6 层隔离（references/multi-tenant.md）")
    print("  - 写第一份 ADR（docs/adr/0001-*.md，模板见 references/adr-template.md）")
    print("  - 定义 SLO + 告警规则（references/slo.md + deploy/prometheus/rules/）")
    print("\n注意：governance/{trace,metrics}.py 是**骨架实现**，Phase 4 必须替换为")
    print("      OTel SDK + prometheus_client（铁律 #5）——否则指标多副本漂移、p95 不可算。")
    print("\n可选：跑端到端 demo（需要 OPENAI_API_KEY）：")
    print("  pip install -r ../scripts/demo_requirements.txt")
    print("  python ../scripts/demo_end_to_end.py")


if __name__ == "__main__":
    main()
