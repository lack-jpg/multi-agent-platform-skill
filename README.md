# multi-agent-platform Skill

> 企业级多智能体平台架构（LangGraph + MCP + A2A + RAG + AgentOps 六层架构）
> 源自 gov_AP 项目实战沉淀 + 一轮企业级架构评审。

## 5 分钟入门

```bash
# 1. 生成新项目骨架（10 秒，产物开箱可运行——stub 模式无需 LLM/DB）
python scripts/scaffold_project.py --name my_agent_app --domain finance

# 2. 验证生成产物（生成 → graph 冒烟 → pytest → 架构校验）
python scripts/test_scaffold.py

# 3. 跑端到端 demo（需要 OPENAI_API_KEY）
pip install -r scripts/demo_requirements.txt
python scripts/demo_end_to_end.py

# 4. 跑回归测试（不需要 LLM key）
python scripts/test_demo.py

# 5. 核查技能里关于外部库/协议的断言是否还成立（能跑的会真跑）
python scripts/check_facts.py

# 6. 校验版本号与变更记录一致
python scripts/check_release.py
```

> 生成的项目自带 `.github/workflows/ci.yml`（架构校验 → ruff → pytest）与
> `scripts/check_architecture.py`，**不依赖本 skill 目录**即可在它自己的 CI 里跑。

> 本 skill **自身**也有一份 CI（`.github/workflows/ci.yml`），把上面 6 步加架构校验
> 全部跑一遍，用 Python **3.12**（即本 skill 声明的最低版本——开发机在 3.14，验不出 3.12 的差异）。
> 改本 skill 前请先读 `SKILL_MAINTENANCE.md`（§8 是提交前必跑的清单）。

## 文档地图

```
SKILL.md                          ← 入口：六层架构 + 五条铁律 + 配套铁律
README.md                         ← 本文件
SKILL_MAINTENANCE.md              ← 维护本 skill 自己的约定（改本 skill 前必读）
FACTS.md                          ← 易漂移事实清单（外部库/协议断言 + 核实方式 + 日期）
VERSION / CHANGELOG.md            ← 版本号与变更记录（+ scripts/check_release.py 校验一致性）

references/
├── architecture.md               ← 六层架构详解、数据流、规模化部署
├── conventions.md                ← 目录/端口/编码/Docker 规范
├── patterns.md                   ← 核心代码模式（13 个骨架片段）
│
├── adr-template.md               ← ADR 模板与门禁（MADR 4.0）
├── slo.md                        ← SLO / 灾备 / 容量规划 / Postmortem
│
├── llm-cost.md                   ← 租户配额 / 复杂度路由 / 缓存
├── llm-security.md               ← 间接注入 / 文档投毒 / Prompt 泄露
├── llm-routing.md                ← 主备容灾 / Circuit Breaker / 多供应商
├── prompt-versioning.md          ← Prompt Registry / A/B / 评测准入
│
├── multi-tenant.md               ← 6 层隔离（PG / Milvus / Redis / Checkpoint）
├── mcp-platform-integration.md   ← 4 类 MCP 来源决策树
├── brownfield.md                 ← 棕地接入 + 反腐蚀层（ACL）+ 五阶段可回滚迁移
│
├── grafana-dashboards.md         ← 7 个 Dashboard JSON 模板
├── testing-matrix.md             ← 4 层测试矩阵 / LLM 非确定性应对
└── langgraph-1x-api.md           ← LangGraph 1.x API 速查

scripts/
├── scaffold_project.py           ← 一键生成项目骨架（产物开箱可运行）
├── check_architecture.py         ← 架构一致性校验（AST 扫描，CI 可用）
├── check_facts.py                ← 事实核查（跑 FACTS.md 的可执行断言 + 过期告警）
├── check_release.py              ← 发布纪律（VERSION ↔ CHANGELOG 一致 + --bump）
├── test_scaffold.py              ← 产物验收回归（生成→冒烟→pytest→架构校验）
├── demo_end_to_end.py            ← 端到端最小可运行 demo
├── test_demo.py                  ← Demo 回归测试（不依赖 LLM key）
├── demo_requirements.txt         ← 跑 Demo 的最小依赖集
└── ci_requirements.txt           ← 跑全部闸门的最小依赖集

.github/workflows/ci.yml          ← 本 skill 自身的 CI（跑上面全部闸门）
```

## 何时使用本 Skill

| 场景 | 用法 |
|---|---|
| 新建一个多 Agent / Agent 平台项目 | 跑 `scaffold_project.py` 起步 |
| 评审现有 Agent 项目代码 | 先跑 `python scripts/check_architecture.py <项目路径>` 自动查出可机器判定的违规，再对照 `SKILL.md §10 评审 checklist` + `conventions.md §8 无状态红线` 逐条人工过 |
| **已有系统不能停，要在旁边长出 Agent**（棕地） | `references/brownfield.md`：反腐蚀层落点 + 五阶段可回滚迁移；然后在项目根放 `.arch-legacy` 启用 `ARCH010/011` |
| 改架构 / 选新生态件 | 写 ADR（`adr-template.md` 模板） |
| 出了事故 / 想加监控 | `slo.md` + `grafana-dashboards.md` |
| 评估成本 / 加多租户 | `llm-cost.md` + `multi-tenant.md` |
| 调试 Agent 决策 / 测 LLM 输出 | `testing-matrix.md` |

## 关键设计原则（5+6 条铁律）

**五条核心铁律**（见 SKILL.md §3）：
1. Agent 优先，禁止业务 if/else 直调
2. 所有 Agent 必须可观测
3. 工具必须标准化（MCP + Prompt Registry）
4. 运行时必须无状态
5. 生态优先，自研收敛

**六条配套铁律**（新增）：
- 决策可追溯（ADR）
- 租户隔离前置
- 成本可观察
- 安全分两层（输入 + 工具结果）
- 有承诺必留证据（SLO + Postmortem）
- Prompt 是代码（版本化 + A/B + 准入门槛）

## 适用边界

- ✅ **Python 3.12+ / FastAPI / LangGraph 1.x / PG / Redis 技术栈** —— 直接采用全部
- ✅ **已有系统不能停、要在旁边长出 Agent 能力（棕地）** —— 走 `references/brownfield.md`
  的渐进路径（脚手架仍可用，但新代码是**在旁边长**，不是替换遗留系统）
- ⚠️ **TypeScript / Java / Go 栈** —— 复用架构原则（分层、无状态、生态优先），不照搬目录与代码
- ⚠️ **不需要 RAG / Milvus 的轻量场景** —— Milvus 等非必需组件按需裁剪
- ❌ **单文件 demo / 一次性脚本** —— 杀鸡用牛刀，直接写 function 即可

## 文档贡献指南

如发现错误或缺漏：
1. 跑 `scripts/test_demo.py` 确认未引入回归
2. 修改相关文档（保持现有章节结构）
3. 更新本 README 的文档地图（如新增文件）
4. 提交时附上"修复了哪些 P0/P1/P2 不足"的说明

如需补全新主题，先在 `references/` 加文档，然后在 `SKILL.md §9` 与本 README 双向索引。