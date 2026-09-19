---
name: multi-agent-platform
description: 企业级多智能体平台架构（LangGraph + MCP + A2A + RAG + AgentOps 六层架构，源自 gov_AP 项目）。当用户要新建/搭建/开发一个多 Agent 平台、智能体应用、Agent Runtime 项目，或要在不能停服的存量/遗留系统里增量长出 Agent 能力（棕地改造，不做推倒重写），或希望沿用 gov_AP 项目的架构进行脚手架生成、架构设计、模块拆分、代码评审时使用。已覆盖：架构原则、运行时形态、可观测性、ADR/SLO/合规、成本治理、LLM 安全、多租户 6 层隔离、棕地反腐蚀层（ACL）与五阶段可回滚迁移、Prompt 版本治理、测试矩阵。触发词：多智能体架构、Agent 平台、LangGraph 编排、MCP 工具、A2A 协同、AgentOps 治理、新项目脚手架、棕地改造、遗留系统接 Agent、存量系统迁移、反腐蚀层、老系统加智能体。不适用：单文件 demo / 一次性脚本 / 纯 RAG 检索任务 / 非 Python 技术栈的纯工程任务 / 与 Agent 能力无关的遗留系统重构或纯技术栈迁移。
---

# 企业级多智能体平台架构（六层架构）

本 skill 沉淀自 gov_AP（政务多智能体协同与治理平台 v3.0，Python 38k+ 行、231 测试）+ 一轮企业级架构评审结论。
目标：让任何新项目都能**直接采用同一套架构**落地——可复用、简洁、高内聚低耦合、且**运行时形态**（而非仅蓝图）达到企业级。

## 0. 读者分流表（按角色选入口，不必从头读到尾）

| 你是谁 | 优先读 |
|---|---|
| 新手，第一次接触本 skill | README §5 分钟入门 → SKILL.md §1-3（什么时候用、六层架构、五条铁律） → `scaffold_project.py` |
| 工程师，要在已有项目里加 Agent 能力 | `references/brownfield.md`（反腐蚀层 + 五阶段可回滚迁移）→ `references/patterns.md`（13 个骨架片段 + `[修订]` 历史踩坑） |
| 架构师/技术评审，逐条对照检查代码 | SKILL.md §10 评审 checklist + `references/conventions.md` |
| 治理负责人（成本 / 安全 / SLO / 合规） | `references/llm-cost.md`、`llm-security.md`、`slo.md`、`multi-tenant.md`（按需加载，**不要全读**） |

> 经验：脚手架生成后先跑 `scripts/test_demo.py` 验证基线可用，再按上方分流读对应文档。一次性把 references/ 全读会显著降低效率。

## 1. 什么时候用本 skill

- 新建一个多 Agent / Agent 平台 / 智能体工作流项目
- 已有项目要重构为 Agent 架构（单体 LLM 调用 → 编排式多 Agent）
- **已有系统不能停，要在旁边长出 Agent 能力**（棕地）→ 先读 `references/brownfield.md`
- 评审 Agent 项目代码是否符合架构规范
- 需要为项目生成符合架构的目录骨架 / CLAUDE.md / docker-compose

**适用前提**：本 skill 的目录结构、铁律、代码模式均绑定 Python 3.12+ / LangGraph / FastAPI / PG / Redis 技术栈（见 §6）。若项目是 TypeScript/Java 等其他栈，或不需要 RAG/Milvus 的轻量场景，只复用本 skill 的**架构原则**（分层、无状态、生态优先、降级显式），不要照搬目录与技术选型；Milvus 等非必需组件按需裁剪。

## 2. 六层架构总览（必须严格遵循）

```
L1 接入层        FastAPI（JWT认证 + RBAC + 请求日志/trace_id + CORS白名单）
L2 编排层        LangGraph StateGraph（无状态构图；状态/Checkpoint 全外置 PG/Redis）
L3 专业Agent层   intent / policy / material / workflow / governance（按业务域裁剪）
L4 MCP工具层     Agent → MCP Client → MCP Gateway(JWT+RBAC) → MCP Server → 业务系统
L5 异步协同层    任务队列(ARQ/Redis) + A2A Connector + HMAC Callback + 挂起/恢复
L6 AgentOps治理层 OTel真接入 + prometheus_client指标 + Guardrail(前置) + 评测 + 租户隔离
```

> L4 说明：自建 MCP Gateway（统一 JWT+RBAC+审计）适用于**多租户工具治理**场景（工具需按角色鉴权、调用需审计）。单人/小项目若工具本就受控，可让 MCP Client 直连 MCP Server 省掉一跳，但架构位置保留，后续要治理时再插回 Gateway。

数据流（一次请求）：

```
POST /api/chat → JWT认证 → 护栏前置(LLM前阻断) → create_initial_state
→ supervisor(规划+路由) → intent(三级分类) → 专业Agent(经MCP调工具)
→ workflow(落库办件) → [重任务: 入队Worker执行 | A2A: 挂起→回调恢复]
→ governance(输出过滤) → END → ChatResponse{answer, evidence, intent, risk_level, execution_steps}
```

## 3. 五条铁律（写代码前先检查）

1. **Agent 优先，禁止业务 if/else 直调**：流程由 Supervisor → Planner → Router → Specialist → Tool Calling 驱动，不许 `if user_input=="xxx": call_function()`
2. **所有 Agent 必须可观测**：任何 Agent 调用必须记录 trace_id / agent_name / input / output / latency / token_usage / tool_calls，禁止裸 `agent.run()`
3. **工具必须标准化**：Agent 不许直接 import 业务代码，必须走 `Agent → MCP Client → MCP Gateway → MCP Server → Business Service`；Prompt 不许硬编码，必须走 Prompt Registry 版本化
4. **运行时必须无状态**：Graph 构图为纯函数、可随时重建；一切可变状态（checkpoint / 缓存 / 指标 / 任务）外置到 PG/Redis，**禁止进程内单例持有业务态**——这是多副本水平扩展的前提
5. **生态优先，自研收敛**：能用生态件不自研（checkpointer 用官方 PostgresSaver、指标用 prometheus_client、追踪用 OTel SDK）；一个平台只许保留**一个**自研核心（通常是 guardrail，业务差异化所在），其余自研件必须给出保留理由并写 ADR

**配套铁律（企业级 P0）**：
- **决策可追溯**：任何架构选型/铁律修改/生态件引入必须写 ADR（见 `adr-template.md`）
- **租户隔离前置**：建表第一天带 `tenant_id`、缓存键加 tenant 前缀、Checkpoint thread_id 包含 tenant（见 `multi-tenant.md`）
- **成本可观察**：每个 LLM 调用记录 token + 模型 + 租户，按租户配额告警（见 `llm-cost.md`）
- **安全分两层**：输入护栏前置 + **工具结果二次护栏**（间接注入防不住直接护栏）（见 `llm-security.md`）
- **有承诺必留证据**：SLO 必须配套告警 + Postmortem（见 `slo.md`）
- **Prompt 是代码**：版本化 + A/B + 评测准入门槛（见 `prompt-versioning.md`）

## 4. 高内聚低耦合规则（模块拆分时执行）

- **一个模块只归属一层**：`agents/` 只放决策逻辑，`tools/` 只放协议适配，`rag/` 只放检索——禁止"工具层里写业务判断、Agent 里写 HTTP 细节"
- **模块间只经三种通道通信**：AgentState（图内）、MCP 协议（跨进程）、Pydantic Schema（跨层接口）——禁止模块间共享可变全局变量
- **依赖注入贯穿**：构造函数/工厂传 `llm/mcp_client/a2a_connector/checkpointer`，全部 Optional + stub 兜底——任何模块单测不需要真实基础设施
- **降级显式**：所有 fallback 标注 `mode=stub` + logger.warning，生产禁止静默 mock

## 5. 标准目录结构（按层分包，不许跨层反向依赖）

```
project/
├── agents/            # L3 Agent层：supervisor/intent/policy/material/workflow/governance
│   └── <name>/{agent.py, schema.py, prompts.py, ...能力模块}
├── orchestration/langgraph/   # L2 编排：state.py graph.py nodes.py edges.py runtime.py
├── tools/            # L4/L5 协议层：mcp/{client,gateway,schema,servers/*}, a2a/{connector,task,callback,...}
│   └── workers/      # 异步任务：ARQ worker + 任务定义（OCR/评测/索引重建等重任务）
├── rag/              # 知识层：embedding retriever reranker generator knowledge_base ingest
├── governance/       # L6 治理：trace guardrail pii metrics dashboard evaluation/
├── backend/          # L1 接入：main.py config.py api/ middleware/ services/
├── database/         # 数据层：connection models schemas migrations/
├── prompts/          # Prompt注册中心（版本化）
├── cases/            # 评测用例 JSON
├── frontend/         # 演示前端（可选）
├── models/           # 本地模型（gitignore）
├── deploy/           # Dockerfile* + k8s/ + prometheus/ grafana/
├── requirements/     # requirements.txt / -dev / -gpu / -ocr
├── tests/            # pytest
├── CLAUDE.md STRUCTURE.md README.md PLAN.md .env.example docker-compose.yml
```

依赖方向：`backend → orchestration → agents → tools/mcp|a2a|workers → rag/governance/database`。
治理（governance）为旁路，**不参与业务回答**；workers 与 api 是两个进程，只通过 Redis 队列和 PG 通信。

## 6. 技术栈（锁定版本代际，不要混用旧式 API）

> 本节所有版本/API 断言登记在 `FACTS.md`，逐条附核实方式与日期。
> 核查：`python scripts/check_facts.py`（可执行的断言会真跑，不只是看日期）。
> 改本节前先跑一次；发现断言已失效就改本节**并**更新 `FACTS.md`。

- Python 3.12+ · FastAPI · Pydantic v2 · pydantic-settings
- LangChain 1.x / LangGraph 1.x —— 只用 `StateGraph + Node + Edge + Checkpointer(官方 PostgresSaver)`，**禁止自研 checkpointer**。（`AgentExecutor` 在 1.x 中已被移除、根本 import 不到，不存在"要不要禁"的问题——只在迁移旧代码时才需清理，见 `FACTS.md` F-004）
- MCP（工具协议，**日期版本号**如 `2026-07-28`，**不存在 "1.x" 版本**）· A2A（Agent 间协议，**v1.0 已于 2026-03 发布**）
- PostgreSQL 16（SQLAlchemy async + asyncpg + Alembic）· Redis 7（缓存 + ARQ 任务队列）· Milvus **2.5.4+**（`partitionkey.isolation` 自 2.5.4 起引入，见 multi-tenant.md §2）
- 观测：OpenTelemetry SDK（真接入，不用 NoOp 摆设）· prometheus_client（标准 histogram bucket）· Grafana；日志 loguru（ContextVar 传 trace_id）

## 7. 新项目落地流程（按此顺序，勿颠倒）

**第 0 步 脚手架**：运行 `scripts/scaffold_project.py --name <项目名> --domain <业务域>`
→ 生成六层目录骨架 + CLAUDE.md + .env.example + requirements + CI，**产物开箱可运行**；
跑 `python scripts/test_scaffold.py` 验收（生成 → graph 冒烟 → pytest → 架构校验四级全绿）。

**第 1 步 核心链路**（先让系统在 stub 模式跑通，无 LLM/DB 也可运行）：
`backend/config.py → backend/main.py → api/routes.py → api/dependencies.py`（execute_agent 桥）→
`orchestration/langgraph/state.py → graph.py → nodes.py → edges.py` → `agents/supervisor/*`

**第 2 步 Agent 能力**：intent（三级分类链：小模型→关键词→LLM fallback）→ 专业 Agent（每个含 agent.py + schema.py + prompts.py）→ rag/ 管线（混合检索 + RRF + 重排 + **ingest 摄取管道**，语料进不来检索等于零）

**第 3 步 工具协议**：`tools/mcp/schema.py（Tool Schema + 注册表）→ client → gateway → servers/<域>_server/{server.py, tools.py}`；跨系统协同加 `tools/a2a/*`；耗时 >5s 的重任务（OCR/批量索引/评测）进 `tools/workers/` 队列

**第 4 步 治理评测**：`governance/trace + guardrail + pii + metrics + dashboard` → `governance/evaluation/{metrics, evaluator, benchmark, runner}` → `cases/*.json`；确定租户隔离策略（见 conventions.md §9）

**第 5 步 生产化**：`deploy/Dockerfile* + docker-compose.yml`（端口规范见 references/conventions.md）→ CI（ruff + pytest + 覆盖率门禁 + 镜像构建）→ OTel/Prometheus/Grafana 告警闭环 → 多副本验证（杀掉任一 api 实例服务不中断）

## 8. 关键模式速查（详细实现见 references/patterns.md）

| 模式 | 要点 |
|---|---|
| AgentState | TypedDict，标量字段覆盖更新 + 列表字段 Annotated reducer（按 id 合并去重） |
| 依赖注入 | build_graph(llm, mcp_client, a2a_connector, checkpointer) 全部 Optional，缺失走 stub |
| 三级降级 | 真实优先 → 显式降级（结果标注 mode=stub）→ **生产禁止静默 mock** |
| 安全护栏 | 输入护栏在 graph.ainvoke **之前**（省 Token）；输出护栏在 governance_node；PII 四类脱敏 |
| Runtime 护栏 | max_steps=10、循环检测（窗口6/阈值3）、timeout、retry<3 回 supervisor 重规划 |
| 意图回环 | intent_node → supervisor 为静态边，识别后必回 supervisor 二次规划 |
| A2A 异步 | 同步 `TASK_STATE_COMPLETED` 直接续跑；异步 `TASK_STATE_SUBMITTED` → checkpointer 挂起 → HMAC 回调恢复 |
| 重任务入队 | 耗时 >5s 的任务（OCR/批量索引/评测）→ ARQ 队列 → Worker 进程执行，API 线程只做轻量编排 |

## 9. 参考文档（按需加载，勿一次性全读）

**核心三篇（必读）**：
- `references/architecture.md` —— 六层架构逐层职责、数据流、请求全链路时序、降级容错链、规模化要点
- `references/conventions.md` —— 目录/端口/命名/编码规范、docker-compose 服务清单、Windows Docker 坑、租户隔离
- `references/patterns.md` —— 核心代码模式与骨架片段：AgentState、build_graph、节点包装、MCP Client/Gateway、任务队列、护栏、评测指标

**治理与运营（升级企业级必备）**：
- `references/adr-template.md` —— ADR 模板（MADR 4.0）+ ADR-0003 真实案例（checkpointer 选型）
- `references/slo.md` —— SLO / 灾备 / 容量规划（可用性 99.5%、p95 < 2.5s、错误预算、Postmortem 模板）
- `references/llm-cost.md` —— LLM 成本治理（租户配额、复杂度路由、缓存命中率告警）
- `references/llm-security.md` —— LLM 安全（间接注入、文档投毒、Prompt 泄露、系统提示边界）
- `references/llm-routing.md` —— LLM 主备容灾 + Circuit Breaker + 多供应商负载均衡
- `references/prompt-versioning.md` —— Prompt Registry + A/B 分流 + 评测准入 + 灰度发布

**多租户与可观测性**：
- `references/multi-tenant.md` —— 多租户 6 层隔离（PG + Milvus partition + Redis key + Checkpoint + Prompt）
- `references/mcp-platform-integration.md` —— 自建 Gateway vs WorkBuddy MCP / 第三方 MCP 决策矩阵
- `references/grafana-dashboards.md` —— 7 个 Dashboard 的 JSON 模板（API / Agent / LLM 成本 / Worker / Security / MCP / Checkpoint）

**棕地接入（已有系统旁长 Agent）**：
- `references/brownfield.md` —— 反腐蚀层（ACL）的落点与三项职责 + 五阶段可回滚迁移路径 + 棕地下的铁律折中表

**质量保证与 API 参考**：
- `references/testing-matrix.md` —— 四层测试矩阵 + LLM 非确定性应对 + Golden 用例 + LLM Judge
- `references/langgraph-1x-api.md` —— LangGraph 1.x API 速查 + 0.x → 1.x 迁移踩坑

**脚本**：
- `scripts/scaffold_project.py` —— 一键生成六层骨架 + CLAUDE.md + .env.example + requirements + CI
- `scripts/check_architecture.py` —— **架构一致性校验**：AST 扫描项目，把 §10 checklist 变成 CI 可失败的断言（11 条规则，零依赖）
- `scripts/demo_end_to_end.py` —— 真实 LLM + 内存 PG/Redis 的端到端最小可运行 demo（含 trace 与 metrics）
- `scripts/test_scaffold.py` / `test_demo.py` —— 离线回归：产物验收（生成→冒烟→pytest→架构校验）/ demo 回归，均不依赖 LLM key

## 10. 架构评审 checklist（改代码/评审时逐条过）

> 标 `⚙ ARCHxxx` 的条目已可机器检查：`python scripts/check_architecture.py <项目路径>`
> （exit code 可直接接 CI）。其余条目仍需人工判断——机器只能查"确定的形状"，查不了"设计是否合理"。

**耦合与内聚**
- [ ] 是否有 Agent 直连业务代码（绕过 MCP）？ ⚙ ARCH004
- [ ] 新模块是否只归属一层、只经 AgentState/MCP/Schema 三种通道通信？
- [ ] 是否存在跨模块共享可变全局变量？
- [ ] 是否存在按用户输入的硬编码分支（`if user_input == "xxx"`）？ ⚙ ARCH006

**棕地接入（仅当项目里有拆不掉的遗留系统时逐条过，见 `references/brownfield.md`）**
- [ ] 遗留系统是否只从反腐蚀层进入（`tools/**/acl/`），没有旁路直连？ ⚙ ARCH010
  （启用方式：项目根放 `.arch-legacy`，一行一个遗留顶层包名；没有该文件则本条不适用）
- [ ] 遗留目录是否没有反向依赖新架构层（否则拆除路径已被切断）？ ⚙ ARCH011
- [ ] ACL 是否只做翻译（模型 / 错误 / 幂等），没长出业务决策？
- [ ] 每个迁移阶段是否都写清了**退出条件与回滚动作**，且回滚演练过？
- [ ] 是否存在"双写"（同一请求写新旧两套）？

**无状态与规模化（企业级红线）**
- [ ] Graph / 指标 / 缓存 / 任务是否仍依赖进程内状态？（多副本会漂移）
- [ ] checkpointer / metrics / tracer 是否用的生态件而非自研？ ⚙ ARCH003
- [ ] 内存列表/缓存是否都有 TTL 或 LRU 上限（防泄漏）？
- [ ] 耗时 >5s 的任务是否已从请求线程挪进队列？

**质量与安全**
- [ ] 新增 Agent/节点是否接入 trace + metrics + guardrail？租户数据是否带 tenant_id 隔离？ ⚙ ARCH009
- [ ] 是否存在硬编码 Prompt / 静默 mock / `except: pass`？ ⚙ ARCH001 / ARCH005
- [ ] 列表型 state 字段是否配了 reducer（防重放翻倍）？ ⚙ ARCH002
- [ ] 降级路径是否显式标注（mode=stub / logger.warning）？
- [ ] 新增 bind mount 目录是否加入 docker-entrypoint chown 列表？
- [ ] async 路径是否混入同步阻塞（`time.sleep`/`requests`）或用 print 代替 logger？ ⚙ ARCH007 / ARCH008
- [ ] 是否 async + type hint + docstring，ruff 全绿、覆盖率过门禁？
