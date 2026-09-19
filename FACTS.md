# FACTS.md — 易漂移事实清单

> 本 skill 中所有**关于外部库/协议/服务的可证伪断言**都登记在这里，附核实方式与日期。
> 核查：`python scripts/check_facts.py`（`--self-test` 自检核查器本身）。

## 为什么需要本文件

skill 里的外部事实断言会以两种方式失效：

1. **虚构**——写的时候就没核实过。本 skill 真实踩过：`langgraph-1x-api.md §3.1` 曾断言
   "0.x 用 `operator.add`、1.x 改用 `add`"，而 `langgraph.graph` **从未导出过 `add`**，
   错误扩散到 4 个文件。
2. **漂移**——写的时候是对的，上游改了。

⚠️ **关键认识：光有 `last_verified` 日期拦不住第 1 种。** 凭印象写下的日期反而制造
"已核实"的假象。能拦住虚构的只有一件事：**把断言写成可执行的形式**。写不出检查代码，
往往说明当初就没真验过。

所以清单分两级，并明确标注哪级弱：

| 级别 | 手段 | 拦虚构 | 拦漂移 | 数量 |
|---|---|---|---|---|
| **A 可执行** | 对已安装的包真跑一段断言 | ✅ | ✅ | 见 `--list` |
| **B 人工** | 只有日期 | ❌ | ⚠️ 仅提醒"该看了" | 见 `--list` |

**维护规则：新增 B 级条目前，先问"这条能不能写成断言"。** 能写就必须写成 A 级。

---

## F-001 · `langgraph.graph` 的导出面

- **断言**：`langgraph.graph` 导出 `add_messages`，**不导出** `add`
- **被用于**：`references/langgraph-1x-api.md §3.1`、`references/patterns.md §1`、`scripts/scaffold_project.py`
- **核实方式**：可执行
  ```python
  import langgraph.graph as g
  assert "add_messages" in dir(g), "add_messages 不见了"
  assert "add" not in dir(g), "上游新增了 add —— §3.1 的『不存在』断言需重写"
  ```
- **依赖**：langgraph
- **核实版本**：langgraph 1.1.6
- **last_verified**：2026-09-19
- **漂移风险**：中——上游若新增 `add` 别名，§3.1 的更正说明即失效。这正是本条要盯的。

## F-002 · LangGraph 核心符号的导入路径

- **断言**：`StateGraph` / `START` / `END` 自 `langgraph.graph` 导入；`MemorySaver` 自 `langgraph.checkpoint.memory` 导入
- **被用于**：`references/langgraph-1x-api.md §1-2`、`scripts/scaffold_project.py`、`scripts/demo_end_to_end.py`
- **核实方式**：可执行
  ```python
  from langgraph.graph import StateGraph, START, END
  from langgraph.checkpoint.memory import MemorySaver
  assert all(x is not None for x in (StateGraph, START, END, MemorySaver))
  ```
- **依赖**：langgraph
- **核实版本**：langgraph 1.1.6
- **last_verified**：2026-09-19
- **漂移风险**：低——1.x 内的稳定路径，跨大版本才可能变。

## F-003 · `add_messages` 与 `operator.add` 的语义差异

- **断言**：`add_messages` 按 message id 合并（同 id 覆盖、`RemoveMessage` 真删除）；`operator.add` 只是拼接（同 id 变两条、`RemoveMessage` 变空消息）
- **被用于**：`references/langgraph-1x-api.md §3.1`（行为差异表）、`references/patterns.md §1`、`scripts/scaffold_project.py`（产物断言）
- **核实方式**：可执行
  ```python
  from operator import add
  from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
  from langgraph.graph import add_messages

  old, new = AIMessage(content="旧", id="m1"), AIMessage(content="改", id="m1")
  assert len(add([old], [new])) == 2, "operator.add 应产生两条"
  assert len(add_messages([old], [new])) == 1, "add_messages 应原地覆盖为一条"

  a, b = AIMessage(content="A", id="a"), HumanMessage(content="B", id="b")
  assert len(add([a, b], [RemoveMessage(id="a")])) == 3, "operator.add 会追加空消息"
  assert len(add_messages([a, b], [RemoveMessage(id="a")])) == 1, "add_messages 应真删除"
  ```
- **依赖**：langgraph, langchain-core
- **核实版本**：langgraph 1.1.6 / langchain-core 1.2.25
- **last_verified**：2026-09-19
- **漂移风险**：低——reducer 语义是核心契约；但 `RemoveMessage` 的行为值得盯。

## F-004 · LangChain 1.x 中已无 `AgentExecutor`

- **断言**：`langchain.agents` 不再导出 `AgentExecutor`——**正因如此，把它写进"禁止清单"是空转的**：
  新代码里根本 import 不到，读者会以为还有得选。全 skill 已改为"只许 StateGraph…；
  `AgentExecutor` 是迁移旧代码时的清理项"（`SKILL.md §6`、`conventions.md §4`、
  scaffold 生成的 CLAUDE.md、`langgraph-1x-api.md §10`）
- **被用于**：`SKILL.md §6`、`CLAUDE.md` 模板（scaffold 产物的禁止清单）
- **核实方式**：可执行
  ```python
  import langchain.agents as a
  assert not hasattr(a, "AgentExecutor"), "AgentExecutor 又回来了 —— 禁止清单需重新表述"
  ```
- **依赖**：langchain
- **核实版本**：langchain 1.2.15
- **last_verified**：2026-09-19
- **漂移风险**：低——已移除的 API 不会无声回归；但若被移到子模块，本条会失真。

## F-005 · pydantic-settings 的配置类写法

- **断言**：`BaseSettings` / `SettingsConfigDict` 自 `pydantic_settings` 导入（scaffold 产物的 `config.py` 依赖此写法）
- **被用于**：`scripts/scaffold_project.py`（`CONFIG_PY` 模板）
- **核实方式**：可执行
  ```python
  from pydantic_settings import BaseSettings, SettingsConfigDict
  class _S(BaseSettings):
      model_config = SettingsConfigDict(env_file=".env", extra="ignore")
      x: int = 1
  assert _S().x == 1
  ```
- **依赖**：pydantic-settings
- **核实版本**：pydantic-settings 2.13.1
- **last_verified**：2026-09-19
- **漂移风险**：中——pydantic v3 可能改配置方式。

## F-006 · prometheus_client 的核心符号

- **断言**：`Counter` / `Histogram` / `start_http_server` 自 `prometheus_client` 导入
- **被用于**：`scripts/demo_end_to_end.py`、`references/grafana-dashboards.md`
- **核实方式**：可执行
  ```python
  from prometheus_client import Counter, Histogram, start_http_server
  c = Counter("facts_probe_total", "probe", ["agent"])
  c.labels(agent="x").inc()
  h = Histogram("facts_probe_seconds", "probe")
  h.observe(0.1)
  assert start_http_server is not None
  ```
- **依赖**：prometheus-client
- **核实版本**：prometheus-client 0.25.0
- **last_verified**：2026-09-19
- **漂移风险**：低——核心 API 极其稳定。

## F-007 · AsyncPostgresSaver 的导入路径与连接池契约

- **断言**：路径为 `langgraph.checkpoint.postgres.aio.AsyncPostgresSaver`；必须配 `AsyncConnectionPool`，`kwargs={"autocommit": True, "row_factory": dict_row}`，且须显式 `await saver.setup()`
- **被用于**：`references/langgraph-1x-api.md §5.1`、`references/patterns.md`（checkpointer 片段）、`SKILL.md §6`
- **核实方式**：可执行
  ```python
  from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
  from psycopg_pool import AsyncConnectionPool
  from psycopg.rows import dict_row
  import inspect

  params = inspect.signature(AsyncPostgresSaver.from_conn_string).parameters
  assert params, "from_conn_string 签名变了"
  assert hasattr(AsyncPostgresSaver, "setup"), "setup() 不见了 —— 建表步骤无从触发"
  assert AsyncConnectionPool is not None and dict_row is not None
  ```
- **依赖**：langgraph-checkpoint-postgres, psycopg-pool
- **核实版本**：langgraph-checkpoint-postgres 3.1.2 / langgraph-checkpoint 4.2.0 /
  psycopg 3.3.6 / psycopg-pool 3.3.2
- **last_verified**：2026-09-19
- **漂移风险**：中——这是本 skill 里**曾经写错过一次**的地方（曾误写为 asyncpg 风格），必须盯。
- **备注**：本条**曾经长期处于 SKIP**（本机没装这两个包），2026-09-19 装好后**首次真跑通过**。
  装法：`pip install langgraph-checkpoint-postgres "psycopg[binary,pool]"`——已写进
  `scripts/ci_requirements.txt`，CI 里不会再跳过。
  ⚠️ 本机若又缺包，它会静默退回 SKIP。**SKIP 不是通过**——别把"0 失败"读成"全验过了"。

## F-008 · 骨架代码在 Python 3.12+ 上可用

- **断言**：`scripts/` 下所有脚本使用 `from __future__ import annotations` 且不依赖 3.12 之后的语法
- **被用于**：`SKILL_MAINTENANCE.md §7`、`SKILL.md §6`
- **核实方式**：可执行
  ```python
  import ast, pathlib

  files = sorted(pathlib.Path("scripts").glob("*.py"))
  # 反空转：工作目录错时 glob 到 0 个文件，下面的断言会**恒真**。
  # 本条真的这么空转过一段时间（核查器曾把 cwd 设成临时目录），见"备注"。
  assert len(files) >= 5, f"只扫到 {len(files)} 个脚本 —— 工作目录不对，本条会假绿"

  missing = []
  for p in files:
      tree = ast.parse(p.read_text(encoding="utf-8"))
      if not any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
                 and any(a.name == "annotations" for a in n.names) for n in tree.body):
          missing.append(p.name)
  assert not missing, f"缺 from __future__ import annotations: {missing}"
  ```
- **依赖**：-
- **核实版本**：Python 3.12+ 语法子集（本机 3.14.0 可解析）
- **last_verified**：2026-09-19
- **漂移风险**：低——但**本机跑在 3.14 上，验不出 3.12 的差异**；真正的下限验证需在 3.12 环境跑。这一局限必须承认。
- **备注**：断言用相对路径 `scripts/`，**核查器必须把工作目录设成 skill 根目录**。
  本条曾因核查器把 cwd 设成临时目录而**空转**（glob 到 0 个文件 → 断言恒真），
  空转期间 `test_demo.py` 实际缺 `from __future__ import annotations` 却一直报 ok。
  2026-09-19 修 `check_facts.py` 的 cwd 后才暴露出来。

## F-014 · `ChatPromptTemplate` 的导入路径

- **断言**：LangChain 1.x 中 `ChatPromptTemplate` 在 `langchain_core.prompts`；**`langchain.prompts` 子模块已不存在**（`from langchain.prompts import ...` 报 ModuleNotFoundError）
- **被用于**：`references/langgraph-1x-api.md §9`、`references/llm-security.md §3`
- **核实方式**：可执行
  ```python
  from langchain_core.prompts import ChatPromptTemplate
  p = ChatPromptTemplate.from_messages([("system", "x"), ("human", "{q}")])
  assert p.format(q="y")

  try:
      import langchain.prompts  # noqa: F401
  except ModuleNotFoundError:
      pass
  else:
      raise AssertionError("langchain.prompts 又出现了 —— 可改回短路径，或两条并存")
  ```
- **依赖**：langchain-core, langchain
- **核实版本**：langchain-core 1.2.25 / langchain 1.2.15
- **last_verified**：2026-09-19
- **漂移风险**：中——若上游恢复 `langchain.prompts` 转发层，本条会失败（属"变好了"，改文档即可）。
- **备注**：本 skill 曾**两处**写成 `langchain.prompts`，2026-09-19 实测失效后改为 `langchain_core.prompts`。

## F-015 · async 节点必须用 `ainvoke`

- **断言**：含 `async def` 节点的图，同步 `.invoke()` 抛 `TypeError: No synchronous function provided to ...`；必须用 `await app.ainvoke(...)`
- **被用于**：`scripts/scaffold_project.py`（产物冒烟测试与 `tests/test_graph.py`）、`references/testing-matrix.md`、`SKILL.md §8`
- **核实方式**：可执行
  ```python
  from langgraph.graph import END, START, StateGraph

  async def _n(state: dict) -> dict:
      return {}

  g = StateGraph(dict)
  g.add_node("n", _n)
  g.add_edge(START, "n")
  g.add_edge("n", END)
  app = g.compile()

  try:
      app.invoke({})
  except TypeError:
      pass
  else:
      raise AssertionError("同步 invoke 竟然成功了 —— 文档的 ainvoke 要求需重写")

  import asyncio
  assert asyncio.run(app.ainvoke({})) == {}
  ```
- **依赖**：langgraph
- **核实版本**：langgraph 1.1.6
- **last_verified**：2026-09-19
- **漂移风险**：低——这是 LangGraph 的既定行为，不会无声改变。
- **备注**：**这是本 skill 真实踩过两次的坑**——scaffold 产物曾整体跑不起来，
  `testing-matrix.md` 曾有 4 处 `await ...invoke()`。已全部改为 `ainvoke`。

## F-010 · MCP 使用日期版本号，不存在 "1.x"

- **断言**：MCP 规范版本形如 `YYYY-MM-DD`（表示最后一次不兼容变更日期），当前为 `2026-07-28`；**不存在 "MCP 1.x" 这种说法**
- **被用于**：`SKILL.md §6`、`references/mcp-platform-integration.md`
- **核实方式**：人工 —— 查 modelcontextprotocol.io 的 Versioning 页与 `specification/<date>/` 目录
- **核实版本**：规范 `2026-07-28`（发布日期 2026-07-28；候选版锁定 2026-05-21）
- **last_verified**：2026-09-19
- **漂移风险**：中——新规范日期会不断出现，本条需定期确认"当前版本"是否已变。
- **⚠️ 这条不是 bug，别"修"**：本机 `mcp` SDK 的 `LATEST_PROTOCOL_VERSION` 是
  `2025-11-25`，**落后于** 规范日期 `2026-07-28`。这是**正常且预期**的——SDK 实现总是
  滞后于规范。不要在看到两者不一致时去把 SKILL.md 的规范日期改小。
  要验规范日期本身，查 modelcontextprotocol.io，不要查 SDK 常量。

## F-011 · A2A v1.0 的发布

- **断言**：A2A（Agent2Agent）协议 v1.0 为首个 GA/生产可用版本，发布于 2026 年 3 月（Linux Foundation 托管）
- **被用于**：`SKILL.md §6`、`references/architecture.md`（L5）、`references/patterns.md`（A2A 片段）
- **核实方式**：人工 —— 查 Linux Foundation / AAIF 公告
- **核实版本**：v1.0（2026-03-12）
- **last_verified**：2026-09-19
- **漂移风险**：低——历史事实不会变；但"当前版本是否为 v1.x"会变，若引用"最新版"需另立条目。

## F-012 · Milvus `partitionkey.isolation`

- **断言**：`partitionkey.isolation` 自 Milvus **2.5.4** 起引入（多租户隔离方案依赖它）
- **被用于**：`SKILL.md §6`、`references/multi-tenant.md §2`
- **核实方式**：人工 —— 查 Milvus 官方 release notes / 文档的版本标注
- **核实版本**：Milvus 2.5.4+
- **last_verified**：2026-09-19
- **漂移风险**：低——引入版本是历史事实；但 Milvus 大版本演进可能改变推荐的隔离做法。

## F-013 · 生成的项目需要 `pytest-asyncio` 才能跑 async 测试

- **断言**：产物的 `tests/test_graph.py` 全是 `async def`，无 `pytest-asyncio` 时会被静默跳过（pytest 对未注册的 async 测试只警告不失败）
- **被用于**：`scripts/scaffold_project.py`（`REQUIREMENTS_DEV`、`PYTEST_INI`）、`scripts/test_scaffold.py`
- **核实方式**：可执行
  ```python
  import importlib.util
  assert importlib.util.find_spec("pytest_asyncio") is not None, "缺 pytest-asyncio"
  assert importlib.util.find_spec("pytest") is not None
  ```
- **依赖**：pytest, pytest-asyncio
- **核实版本**：本机已装
- **last_verified**：2026-09-19
- **漂移风险**：低——但"静默跳过"这个坑本身值得长期盯：`test_scaffold.py` 必须保持对它的显式检查。

## F-016 · LangGraph 不校验 `thread_id` 格式

- **断言**：`thread_id` 可以是任意字符串（含冒号、空格、中文、超长），LangGraph 不做格式校验；
  故 **"v1.x 才要求 thread_id 加 tenant 前缀"是虚构的**，那是本 skill 的多租户隔离约定
- **被用于**：`references/langgraph-1x-api.md §5.4 与 §10`、`references/multi-tenant.md §4`、
  `references/conventions.md §9`
- **核实方式**：可执行
  ```python
  import asyncio
  from langgraph.graph import END, START, StateGraph
  from langgraph.checkpoint.memory import MemorySaver

  runs = []

  def _n(state):
      runs.append(1)      # 计数：证明图真的跑了，断言不是空转
      return {}

  g = StateGraph(dict)
  g.add_node("n", _n)
  g.add_edge(START, "n")
  g.add_edge("n", END)
  app = g.compile(checkpointer=MemorySaver())

  tids = ("trace_abc", "tenant_123:trace_abc", "任意 字符串 / with:colon", "x" * 500)
  for tid in tids:
      asyncio.run(app.ainvoke({}, config={"configurable": {"thread_id": tid}}))
  assert len(runs) == len(tids), "图没执行 —— 断言空转，本条的结论不成立"

  # 上游连"不是字符串"都不校验（实测 1.1.6 接受 None / int / list），
  # 因此任何"v1.x 对 thread_id 有格式要求"的说法都不成立。
  for weird in (None, 12345, ["a"]):
      asyncio.run(app.ainvoke({}, config={"configurable": {"thread_id": weird}}))
  ```
- **依赖**：langgraph
- **核实版本**：langgraph 1.1.6
- **last_verified**：2026-09-19
- **漂移风险**：低——但**本机只有 1.x**，"两代都接受"的 0.x 那一半未实测；
  本节断言严格来说只证明了"1.x 不校验"。若上游某天开始校验，本条会失败。
- **备注**：与 F-001 同属"**项目约定被误写成版本差异**"这一类——本 skill 已踩两次
  （`add`、`thread_id`）。写"某个版本才要求 X"之前，先问：这是上游契约，还是我们自己的规矩？

## F-017 · Demo 依赖的下界必须与 LangChain 1.x 同代

- **断言**：`scripts/demo_requirements.txt` 里每个下界都能被**实装版本**满足（即该约束集
  有真实解）；且 `langchain-openai` 1.x 要求 `langchain-core>=1.2.21`，故"`langchain-openai>=0.1.0`
  + `langchain-core>=0.3.0` + `langchain>=1.0`"这种**跨代混写是无解的**，pip 会直接报冲突
- **被用于**：`scripts/demo_requirements.txt`（曾写错）、`scripts/demo_end_to_end.py` 头部安装说明
- **核实方式**：可执行
  ```python
  import importlib.metadata as md
  from pathlib import Path

  text = Path("scripts/demo_requirements.txt").read_text(encoding="utf-8")
  rows = [ln.strip().split(">=") for ln in text.splitlines()
          if ln.strip() and not ln.strip().startswith("#") and ">=" in ln]
  assert len(rows) >= 4, f"只解析到 {len(rows)} 条下界 —— 工作目录不对，断言空转"

  bad = []
  for name, lower in rows:
      name, lower, installed = name.strip(), lower.strip(), md.version(name.strip())
      # 关键：下界必须与实装**同代**。写 >=0.1.0 而实装 1.1.12 属跨代混写——
      # langchain-openai 0.x 要求 langchain-core 0.x，与本清单的 langchain>=1.0 无交集，
      # pip 直接报冲突无解。只比"实装满足下界"是抓不到的（>=0.1.0 确实被 1.1.12 满足）。
      if lower.split(".")[0] != installed.split(".")[0]:
          bad.append(f"{name}: 下界 {lower} 与实装 {installed} 不同代")
  assert not bad, f"下界跨代混写（整组无解）: {bad}"

  # 佐证跨代无解：1.x 的 langchain-openai 已把 langchain-core 拉到 1.2.x
  core = [x for x in (md.requires("langchain-openai") or []) if x.startswith("langchain-core")]
  assert core and not any("<1" in c for c in core), f"langchain-core 约束变了: {core}"
  ```
- **依赖**：packaging, langchain-openai, langchain, langchain-core, langgraph, prometheus-client, python-dotenv
- **核实版本**：langchain-openai 1.1.12 → 要求 `langchain-core<2.0.0,>=1.2.21`
- **last_verified**：2026-09-19
- **漂移风险**：中——上游发 2.0 时本条会失败（属正常，改下界即可）。真正要防的是
  **再有人凭印象把下界写回 0.x**。断言必须依赖实装包，所以本机没装 `langchain-openai` 时会 SKIP。

---

> **以下三条（F-018 ~ F-020）都是 B 级**，性质与前面不同：它们盯的是**厂商价格与型号**，
> 只存在于厂商定价页/API，本机没有任何包能离线验证——**写不成可执行断言**。
> 登记它们是因为"不登记就彻底没人管"，但必须清醒：**日期只能提醒"该看了"，拦不住写错。**
> （想升级成 A 级，唯一办法是把这些数字从文档里挪进代码，由调用方在运行时从配置读——
> 那样文档就不再持有会漂的数字。这是未来的方向，尚未做。）

## F-018 · 三层路由的模型单价量级

- **断言**：复杂度路由三档的模型单价量级约为**每 1M 输入 token** $0.25 / $3 / $15
- **被用于**：`references/llm-cost.md §3.1`（三层模型选择表）
- **核实方式**：人工 —— 查各厂商官方 pricing 页（OpenAI / Anthropic / Google），
  按该表点名的型号取**输入 token** 价。**不要**引用二手博客或聚合站数字
- **核实版本**：量级估算（本 skill 未锁定具体型号与报价）
- **last_verified**：2026-09-19
- **漂移风险**：**高**——厂商调价频繁；"小/中/大"的分档本身也随新模型发布而变。
- **为什么是 B 级**：价格只存在于厂商定价页，离线验不了。**这是承认的弱点。**
- **复核时的注意事项**：原表头只写"单价（每 1M token）"，**未标注 input 还是 output**，
  已补为"输入"。改数字时务必保持"输入 token"口径——否则会和 §7 的 input/output 拆分成两套账。

## F-019 · 供应商成本对比表的数字

- **断言**：1M 输入 token —— OpenAI ~$3 / Anthropic ~$3 / 国产 ~¥10；
  1M 输出 token —— ~$15 / ~$15 / ~¥30
- **被用于**：`references/llm-cost.md §7`（供应商成本对比，季度评审）
- **核实方式**：人工 —— 查各厂商 pricing 页；国产模型查对应厂商（智谱 / 通义 / 月之暗面等）官网
- **核实版本**：参考量级
- **last_verified**：2026-09-19
- **漂移风险**：**高**——但 §7 自带"每季度评审"机制，**评审时顺手 `--stamp` 本条**，
  让文档里的动作和这张清单联动起来，别各管各的。
- **为什么是 B 级**：同 F-018。

## F-020 · 默认 model id 是占位示例，不是推荐型号

- **断言**：`gpt-4o-mini` / `gpt-4o` / `gpt-5` / `claude-sonnet` 在本 skill 中**只是占位示例**
- **被用于**：`references/llm-routing.md`（`LLMSettings` 默认值、`TenantLLMConfig.allowed_models`）、
  `scripts/demo_end_to_end.py`
- **核实方式**：人工 —— 查厂商 models 列表页，确认型号当前是否仍可用
- **核实版本**：占位（刻意不锁定——锁了会立刻过期）
- **last_verified**：2026-09-19
- **漂移风险**：**高**——厂商下线旧型号是常态。
  尤其注意 `claude-sonnet` **不是完整的 model id**（真实 id 带版本号，如 `claude-sonnet-5`），
  它在这里只为示意"填 Anthropic 的型号"。**照抄它去调 API 会失败。**
- **为什么是 B 级**：型号可用性只能问厂商 API / 文档，离线验不了。
