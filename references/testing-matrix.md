# 测试矩阵（LLM 应用的非确定性测试方法论）

> LLM 测试最大的难点是**输出非确定性**。本文给出 4 层测试矩阵 +
> 3 类 LLM 特有测试方法，让 Agent 决策链可回归。

---

## 1. 四层测试矩阵

```
┌────────────────────────────────────────────────────────────┐
│ Layer 1: 单元测试（<1s）      覆盖率门禁 80%                │
│ Layer 2: 集成测试（<10s）     覆盖率门禁 60%                │
│ Layer 3: 契约测试（<30s）     必须 100% 通过                │
│ Layer 4: E2E + LLM 评测（分钟级）  核心场景必须通过          │
└────────────────────────────────────────────────────────────┘
```

| 层级 | 范围 | 工具 | 触发时机 | 必须覆盖 |
|---|---|---|---|---|
| L1 单元 | 纯函数 / 节点（经 `build_graph` 装配后测，见 §2.1） / 工具实现 | pytest + 依赖注入 stub | 每次 PR | 业务逻辑、边界条件、降级路径 |
| L2 集成 | 模块协作（不依赖真实 LLM/DB） | pytest + docker-compose 测试栈 | 每次 PR | API 路由、MCP Client、Worker |
| L3 契约 | 模块间接口 | schemathesis / pact | 每次 PR | API Schema、MCP Schema |
| L4 E2E + LLM 评测 | 完整业务流 | pytest + 真实 LLM key | 每日 + Release 前 | 决策链、golden 用例、回归 |

---

## 2. L1：单元测试（最频繁，依赖注入是关键）

### 2.1 节点如何测（注意：节点不是纯函数）

⚠️ **常见误区**：LangGraph 节点**不是**纯函数——按 `patterns.md` §2 的设计，节点在 `build_graph()` 内部定义，**依赖（llm / mcp_client / checkpointer）经闭包注入**（见 `langgraph-1x-api.md` §4.1）。因此：

- ❌ **不存在** `policy_node(state, mcp_client=...)` 这种调用——节点签名只有 `(state)` 或 `(state, config)`
- ✅ 测节点 = 装配一个 `build_graph(mcp_client=...)` 后 `ainvoke`，把节点当黑盒验证其状态增量

```python
# tests/agents/test_policy_node.py
import pytest
from orchestration.langgraph.graph import build_graph        # 节点在 build_graph 内闭包注入依赖
from orchestration.langgraph.state import create_initial_state

@pytest.fixture
def stub_state():
    return create_initial_state(user_query="查询生育津贴政策", trace_id="t-1")

class FakeMCP:
    """Mock MCP Client 返回固定结果。"""
    async def call_tool(self, name, args):
        return {"evidence": [{"source": "policy_001", "excerpt": "...", "score": 0.92}]}

CONFIG = {"configurable": {"thread_id": "t-unit"}}

async def test_policy_node_with_stub_mcp(stub_state):
    graph = build_graph(mcp_client=FakeMCP())                # 依赖在此注入，而非传给节点
    result = await graph.ainvoke(stub_state, config=CONFIG)
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["source"] == "policy_001"

async def test_policy_node_explicit_degradation(stub_state):
    """不注入 mcp_client 时必须显式降级（mode=stub），禁止静默 mock。"""
    graph = build_graph(mcp_client=None)
    result = await graph.ainvoke(stub_state, config=CONFIG)
    assert result["evidence"][0]["mode"] == "stub"
```

> **代价说明**：闭包注入让节点无法脱离 graph 单独调用，所以这一层实际是**轻量集成测试**。若某段 Agent 逻辑需要真正的纯单元测试，应把它抽成**不依赖 LangGraph 的纯函数**（如 `agents/policy/extractor.py`），节点只做薄封装——这正是 `patterns.md` §3「节点包装模式」的用意。

### 2.2 降级路径必须测

```python
async def test_intent_classifier_three_tiers(monkeypatch):
    """三级降级：小模型 → 关键词 → LLM fallback。"""
    # Tier 1：小模型置信度 <0.7，降级到 Tier 2
    monkeypatch.setattr(classifier, "_bert_infer_sync", lambda x: IntentResult("policy", 0.5))
    assert (await classifier.classify("xx")).tier == "keyword"

    # Tier 2：关键词命中区间 [0.5, 0.7)，直接返回
    monkeypatch.setattr(classifier, "_bert_infer_sync", lambda x: IntentResult("unknown", 0.3))
    monkeypatch.setattr(classifier, "_keyword_classify", lambda x: IntentResult("policy", 0.6))
    assert (await classifier.classify("xx")).tier == "keyword"

    # Tier 3：全降级到 LLM
    monkeypatch.setattr(classifier, "_keyword_classify", lambda x: IntentResult("unknown", 0.1))
    assert (await classifier.classify("xx")).tier == "llm_fallback"
```

---

## 3. L2：集成测试（模块协作，不依赖真实 LLM/DB）

### 3.1 API 路由测试

```python
# tests/api/test_chat_route.py
from fastapi.testclient import TestClient
from backend.main import create_app

@pytest.fixture
def client():
    return TestClient(create_app())

def test_chat_requires_jwt(client):
    r = client.post("/api/chat", json={"user_query": "hi"})
    assert r.status_code == 401   # 无 token 直接 401

def test_chat_with_stub_graph(client, monkeypatch):
    """stub 模式跑通完整链路：JWT + RBAC + graph。"""
    monkeypatch.setattr("backend.api.dependencies.execute_agent",
                        lambda *a, **k: {"answer": "stub", "risk_level": "low"})
    token = create_test_jwt(user_id="u-1", role="user")
    r = client.post(
        "/api/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"user_query": "hi"},
    )
    assert r.status_code == 200
    assert r.json()["answer"] == "stub"
```

### 3.2 MCP Gateway 测试

```python
# tests/mcp/test_gateway.py
async def test_gateway_unknown_tool_returns_403(client_with_jwt):
    r = await client_with_jwt.post("/api/tools/call", json={"name": "unknown", "args": {}})
    assert r.status_code == 403
    assert "audit" in r.json()["detail"]

async def test_gateway_rbac_blocks_low_role(client_with_jwt):
    """低权限用户调 high-tenant-only tool 应被拒。"""
    r = await client_with_jwt.post(
        "/api/tools/call",
        json={"name": "aws_s3_list", "args": {}},
    )
    assert r.status_code == 403
```

### 3.3 Worker 任务测试

```python
# tests/workers/test_ocr_job.py
async def test_ocr_job_writes_to_pg(monkeypatch, test_db):
    """Worker 必须把结果落 PG，不进进程内存。"""
    monkeypatch.setattr("tools.workers.tasks.ocr_engine",
                        FakeOCR(return_value=[("text1", 0.9)]))
    result = await run_ocr_job(ctx=None, trace_id="t-1", file_path="/tmp/x.pdf")
    assert result["status"] == "ok"
    record = await test_db.fetch_one(
        "SELECT * FROM ocr_result WHERE trace_id = $1", "t-1"
    )
    assert record is not None
    assert record["text"] == "text1"
```

---

## 4. L3：契约测试（模块接口冻结）

### 4.1 API Schema 契约

```python
# tests/contract/test_api_schema.py
import schemathesis

schema = schemathesis.from_uri("http://test-fastapi/openapi.json")

@schema.parametrize()
def test_api_contract(case):
    """所有 API 必须符合 OpenAPI Schema。"""
    case.call_and_validate()
```

### 4.2 MCP Tool Schema 契约

```python
# tests/contract/test_mcp_schema.py
async def test_all_tools_have_pydantic_schema():
    for name, tool in TOOL_REGISTRY.items():
        assert tool.input_schema is not None, f"{name} 缺 input schema"
        assert tool.output_schema is not None, f"{name} 缺 output schema"

async def test_tool_input_validates():
    """Tool input 必须能被 Pydantic 校验。"""
    for name, tool in TOOL_REGISTRY.items():
        sample = load_sample_input(name)
        validated = tool.input_schema.model_validate(sample)
        assert validated is not None
```

---

## 5. L4：E2E + LLM 评测（最复杂）

### 5.1 E2E 测试（真实 LLM + 真实 DB）

```python
# tests/e2e/test_policy_qa_e2e.py
import pytest

pytestmark = pytest.mark.e2e   # CI 中独立跑

async def test_policy_qa_full_chain(real_graph, real_pg, real_redis):
    """端到端：JWT → 护栏 → Graph → RAG → LLM → 输出护栏 → 响应。"""
    token = create_test_jwt(user_id="u-1", tenant_id="t-1", role="user")
    r = await client.post(
        "/api/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"user_query": "北京生育津贴怎么领？"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["risk_level"] in ("low", "medium")
    assert len(body["evidence"]) > 0
    assert "trace_id" in body
```

### 5.2 Golden 用例（处理 LLM 非确定性）

```python
# tests/llm_eval/test_golden_cases.py
CASES = json.load(open("cases/policy_qa_golden.json"))
# cases/policy_qa_golden.json 示例：
# [
#   {
#     "input": "北京生育津贴怎么领？",
#     "must_contain": ["社保", "单位", "申报"],
#     "must_not_contain": ["我不知道", "无法回答"],
#     "evidence_min_count": 1
#   },
#   ...
# ]

@pytest.mark.parametrize("case", CASES)
async def test_golden_case(real_graph, case):
    result = await real_graph.ainvoke(create_initial_state(case["input"], "t-" + uuid.uuid4().hex[:8]))
    answer = result["final_answer"]
    for kw in case["must_contain"]:
        assert kw in answer, f"缺少关键词 {kw}：{answer}"
    for kw in case["must_not_contain"]:
        assert kw not in answer, f"不应包含 {kw}：{answer}"
    assert len(result.get("evidence", [])) >= case["evidence_min_count"]
```

### 5.3 LLM Judge（更智能的评测）

对于"答案质量"无法用关键词衡量的场景：

```python
# tests/llm_eval/test_llm_judge.py
async def test_answer_quality_with_llm_judge(real_graph, llm_judge):
    case = {"input": "...", "reference": "..."}
    result = await real_graph.ainvoke(create_initial_state(case["input"], "t-1"))
    judge_result = await llm_judge.evaluate(
        query=case["input"],
        answer=result["final_answer"],
        reference=case["reference"],
        criteria=["faithfulness", "answer_relevance"],
    )
    assert judge_result["faithfulness"] >= 0.8
    assert judge_result["answer_relevance"] >= 0.8
```

### 5.4 Agent 决策链回归测试

LLM 输出的关键不是最终答案，而是**走了哪条路径**：

```python
async def test_intent_then_policy_path(real_graph, trace_recorder):
    """验证：query "政策咨询" 必须经过 intent → policy 路径。"""
    state = create_initial_state("公积金提取条件", "t-1")
    result = await real_graph.ainvoke(state)
    trace = await trace_recorder.get_trace("t-1")
    nodes_visited = [s["node"] for s in trace["spans"]]
    assert "intent" in nodes_visited
    assert "policy" in nodes_visited
    assert "supervisor" in nodes_visited   # 意图回环验证
```

---

## 6. LLM 非确定性的工程应对

### 6.1 温度控制

```python
# 评测用例必须把 LLM 温度设为 0（确定性）
EVAL_LLM = ChatOpenAI(model="...", temperature=0)

# 生产用例保持 temperature=0.1-0.3（允许少量多样性）
PROD_LLM = ChatOpenAI(model="...", temperature=0.2)
```

### 6.2 Prompt 锁定

评测用例必须锁定 prompt 版本：

```python
# pytest fixture：固定 Prompt 版本
@pytest.fixture
def locked_prompt_registry(monkeypatch):
    monkeypatch.setenv("PROMPT_REGISTRY_VERSION", "v1.2.3-locked")
    return PromptRegistry()
```

### 6.3 重试机制对测试的影响（分层策略，不可一刀切）

Agent 有 retry 机制（`max_retries=3`），单次失败可能重试成功——这会让测试结果取决于"重试了几次"。**策略必须分层**：

| 测试层 | retry 设置 | 理由 |
|---|---|---|
| L1 单元 / 降级路径 | **禁用**（`max_retries=0`） | 让失败立刻暴露，不被重试掩盖 |
| L1 单元 · 专测 retry 本身 | 启用 + 注入"前 N 次失败"的 mock | retry 逻辑必须有**自己的**测试，否则永远测不到 |
| L2/L3 集成 / 契约 | 禁用 | 同上，且能稳定复现 |
| L4 E2E / Golden | **保留生产配置** | 模拟真实行为；此处禁用会偏离生产 |

```python
# 禁用：让失败立刻暴露
@pytest.fixture
def no_retry_config():
    return RuntimeConfig(max_retries=0)

# 专测 retry：前 2 次调用失败、第 3 次成功
@pytest.fixture
def flaky_then_ok_mcp():
    calls = {"n": 0}
    class FlakyMCP:
        async def call_tool(self, name, args):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("transient")
            return {"evidence": [{"source": "policy_001", "excerpt": "...", "score": 0.9}]}
    return FlakyMCP()
```

### 6.4 Snapshot 测试（决策路径快照）

```python
# 用 syrupy / snapshottest 记录决策路径
async def test_routing_snapshot(snapshot, real_graph):
    state = create_initial_state("查询北京生育津贴", "t-1")
    result = await real_graph.ainvoke(state)
    decision_path = extract_decision_path(result)
    assert decision_path == snapshot   # 任何决策路径变化必须显式更新 snapshot
```

---

## 7. CI 流水线集成

> **分层靠 pytest marker，不靠目录**：用 `@pytest.mark.e2e` / `@pytest.mark.contract`（见 §5.1 的 `pytestmark`）。
> 原因：scaffold 生成的项目 `tests/` 下是平铺的，硬编码 `tests/unit`、`tests/agents` 这类子目录会让 CI 直接报 "file or directory not found"。

```yaml
# .github/workflows/test.yml
# 分层用 pytest marker，不依赖目录结构（scaffold 只生成平铺的 tests/）
jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/ -m "not e2e and not contract" --cov=. --cov-report=term-missing
      - run: ruff check .

  contract:
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests/ -m contract

  e2e:
    runs-on: ubuntu-latest
    needs: [unit, contract]
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    steps:
      - run: docker compose up -d postgres redis milvus
      - run: pytest tests/ -m e2e
        env:                                       # secret 放 step 的 env；job 级 secrets 键只对 reusable workflow 的 uses 调用合法
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}

  llm_eval_nightly:
    runs-on: ubuntu-latest
    if: github.event_name == 'schedule'   # 每日
    steps:
      - run: python -m governance.evaluation.runner run --datasets all --use-llm --save-to-db
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

---

## 8. 覆盖率与质量门禁

| 层级 | 最低覆盖率 | 阻断 |
|---|---|---|
| L1 单元（核心模块） | 85% | CI 失败 |
| L1 单元（其他） | 70% | CI 失败 |
| L2 集成 | 60% | CI 失败 |
| L4 LLM 评测 | golden 用例 100% 通过 | 阻塞 release |
| ruff / mypy | 0 错误 | CI 失败 |

---

## 9. 反模式（测试的常见错误）

❌ **测试中调用真实 LLM 但不锁定 prompt 版本**——升级 prompt 后 CI 莫名失败
❌ **在所有层统一禁用 retry**——L1/L2 禁用是对的，但 L4 E2E 禁用会偏离生产行为；且 retry 逻辑自身若无测试，永远测不出来
❌ **golden 用例数量太少**（<20 条）——不具统计意义
❌ **不测降级路径**——生产崩溃时才发现没降级
❌ **不测跨租户隔离**——安全 bug 进入生产
❌ **覆盖率看总数不看模块**——核心模块 30% 覆盖率也算"平均 70%"