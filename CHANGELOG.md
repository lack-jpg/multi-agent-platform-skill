# 变更记录

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## 版本号规则

与 `SKILL_MAINTENANCE.md §5` 的"破坏性变更"清单绑定：

| 位 | 何时 +1 | 例子 |
|---|---|---|
| **MAJOR** | §5 列出的破坏性变更：端口规范 / 六层架构边界 / 铁律增删 / 目录结构 / 生态件选型 | 把 Redis 端口从 12201 改掉 |
| **MINOR** | 新增 `references/` 或 `scripts/`；新增配套铁律 | 新增一篇 references |
| **PATCH** | typo、措辞、**事实更正**、断言修正 | 修正一处虚构的版本差异 |

> ⚠️ **事实更正走 PATCH**——它不改接口，但**必须在 `Fixed` 里写明改了哪条事实**，
> 否则后人不知道文档曾经错过。登记处是 `FACTS.md`。

`VERSION` 文件是机器可读的唯一来源；本文件最新的已发布条目必须与它一致，
由 `python scripts/check_release.py` 校验（CI 第一步就会跑）。

升版本：`python scripts/check_release.py --bump patch|minor|major`（会改 `VERSION` 并在下方插入空条目）。

---

## [Unreleased]

## [1.1.1] - 2026-09-19

一轮三方审计（自查 + 两个独立评审）后的修复。主题是**"什么都没检查"与"检查通过"输出一样**
——四处闸门存在静默失效路径，另有若干外部事实与交叉引用过期。

### Added

- `check_facts.py --strict`：`SKIP` 也算失败。**这是本次最重要的一条**——依赖少装一个包，
  该条断言就静默降级为 `SKIP`，而 `SKIP` 不影响退出码，输出与"核查通过"完全一样。
  CI 已改用 `--strict`（CI 里依赖是齐的，出现 `SKIP` 即说明清单漏了包）。
- `test_scaffold.py` 新增 **L1 产物 `ruff check .`** 一级：产物 CI 模板里有这一步，
  此前却没有任何验收跑它，"生成的目录开箱即被自己的 CI 判红"能一路绿灯到用户手里。
  为此 `scripts/ci_requirements.txt` 增补 `ruff`（缺失时该级降级为跳过，`--strict` 下即失败）。
- `references/grafana-dashboards.md` **§2.8 LLM Routing & Failover Dashboard**：
  `llm-routing.md` §2.6 有熔断告警、§7 有三个路由指标，此前**没有任何 dashboard 覆盖它们**。
  指标切了、熔断了、主备全挂了，看板上什么都看不到。
- 反例进自检：`_GOOD_FIXTURE` 补 `time.sleep()` 与 `state["query"]` 两处**反向用例**，
  钉住 ARCH007 / ARCH006 的误报方向（只验"能报错"不验"不误报"，规则迟早被写歪）。

### Changed

- `check_facts.py` 把控制台编码防线提到模块级（原来在 `self_test` 与 `main` 里各抄一份）。
  Windows GBK 下崩的是 `✓ ⚠ ✅` 这类符号（中文本身编得出），而 **CI 跑在 Linux 上永远发现不了**。

### Fixed

- **假绿 ×4**：
  - `check_facts.py`：代码块里没有 `assert` / `raise` / `sys.exit` 的条目现在报错
    （`print("nothing checked here")` 跑完必然通过，等于没检查）——解析告警非空时
    **退出码 2**，不再当"清单为空"静默通过；
  - `check_architecture.py`：`--select` / `--ignore` 把规则筛空时明确报错，
    而不是"没有规则 → 没有违规 → 通过"；
  - `test_scaffold.py`：新增的 lint 级与既有各级一样，跳过 ≠ 通过。
- **A2A Task 状态名是 v0.3 的写法**（`references/patterns.md` §8、`references/architecture.md` §5、
  `SKILL.md` §5、`references/adr-template.md`）。v1.0 按 ProtoJSON 写作
  `TASK_STATE_SUBMITTED` / `TASK_STATE_INPUT_REQUIRED` / …；小写与连字符写法在官方 SDK 里
  只存在于 `a2a/compat/v0_3/` 兼容层。照旧写法比 `resp.status` 会**恒不相等且不报错**，
  分支永不命中。核实方式：解包 `a2a-sdk` 1.1.4 检查 `a2a/types/a2a_pb2.py`。
- **熔断告警永不触发**（`references/llm-routing.md` §2.6）：
  `llm_circuit_breaker_state{state="open"} == 1` 里两处都错——该指标 label 只有 `provider`，
  **状态是值不是标签**，`{state="open"}` 选中的是不存在的序列（恒空集，Prometheus 不报错）；
  且 `0=closed / 1=half_open / 2=open`，`== 1` 命中的是半开。改为 `== 2`。
- **交叉引用指向错章节**：`llm-routing.md` §7 原写"Grafana 面板见 §2.6"，
  而 §2.6 是 MCP Gateway。现指向新加的 §2.8；`deploy/prometheus/rules/llm_routing.yml`
  也补进了 `grafana-dashboards.md` §3 的目录树（此前被引用却不在树里）。
- **`thread_id` 丢了租户前缀**（`references/patterns.md` §8、`references/adr-template.md`）：
  `langgraph-1x-api.md` §5.4 与 `multi-tenant.md` §4.2 都要求 `{tenant_id}:{trace_id}`，
  但 A2A 一节三处写成裸 `trace_id`——而**这个恢复入口是外部可达的**（回调来自别的 Agent）。
  已统一为 `f"{tenant_id}:{trace_id}"`，并说明"trace_id 一份贯穿"与"checkpoint 键带租户前缀"不矛盾。
- **`check_release.py --bump` 会写坏唯一的版本号来源**：`VERSION=v1.0` 这类非法值被
  `_semver_key` 兜底成 `(0,0,0)`，于是"成功"升成 `0.0.1` 并覆盖真实版本，还打印 ✓。
  现在先校验再推算，非法值直接拒绝。
- **两条误报**：ARCH006 去掉裸关键词 `query`（`state["query"] == "policy"` 正是它不该拦的形状）；
  ARCH007 的 `_async_depth` 现在在进入 `def` 时归零（嵌套同步函数里的 `time.sleep()` 被算作异步路径）。
- `check_architecture.py`：`SKIP_DIRS` 按**相对路径分段**判断（绝对路径下
  `path.parts` 不含 `tests` 等段名，临时目录里的测试会被一起扫，规则失效而不报错）；
  `check_skill_sync` 先切出 `## 10.` 段落再核对（原来扫全文，SKILL.md 正文提到规则号就能骗过它）；
  ARCH010 的提示从 `brownfield.md §3` 改为实际的 §2。
- `check_release.py`：`parse_releases` 先剥围栏代码块——文档里举"条目长什么样"的示例行
  （`## [1.2.3] - 2026-01-01`）会被当成真实条目混进版本序列。
- `test_scaffold.py`：自检跑**产物里的检查器副本**，不只跑 skill 原件
  （下游 CI 执行的是副本）；规则条数从输出里取，不再写死"9 条规则"（加到 11 条后那句就是错的）。
- `scaffold_project.py` 产物模板三处开箱即红：`metrics.py` 多导 `field`、
  `trace.py` 从 `typing` 导 `Iterator`（应用 `collections.abc`）、`test_graph.py` 多导 `pytest`。
- `SKILL.md` frontmatter 补棕地关键词：§0/§1 早已把"已有系统不能停，要在旁边长出 Agent 能力"
  列为入口，触发词里却只有绿地场景——按棕地来描述需求时选不中本 skill。


## [1.1.0] - 2026-09-19

棕地接入路径 + 反腐蚀层（ACL）。此前本 skill 只覆盖绿地（`scaffold_project.py` 生成的
新项目），对"已有系统不能停"的场景只有一句泛泛的"可重构为 Agent 架构"。

### Added

- `references/brownfield.md` —— 反腐蚀层（ACL）的落点（L4 `tools/**/acl/`）与三项职责
  （模型翻译 / 错误翻译 / 幂等）+ **五阶段可回滚迁移**（止血 → 只读接入 → 影子运行 →
  写路径灰度 → 拆遗留），每阶段写明进入条件、可测量的退出条件与回滚动作；
  附棕地下五条铁律的折中表与 6 个常见失败模式。
- `check_architecture.py` **ARCH010**（error）：绕过反腐蚀层直连遗留系统。
  由项目根的 `.arch-legacy`（一行一个遗留顶层包名）启用；无此文件则整条不参与，
  绿地项目零误报。`tools/**/acl/` 与 `tests/` 放行（对拍需直连遗留取对照值）。
- `check_architecture.py` **ARCH011**（warning）：遗留目录反向依赖六层顶层包
  （`agents` / `orchestration` / `governance` / `tools` / `rag` / `backend`）。
  这类反向依赖会让遗留系统永远拆不掉——strangler 的拆除路径被切断。
  故意不含 `database` / `services` 等通用名，避免与遗留系统自有模块撞名误报。
- `check_architecture.py --legacy <pkg,...>`：`.arch-legacy` 的命令行等价入口。
- `--self-test` 新增两项断言：
  1. 棕地规则**确实是 opt-in**（不开配置跑同一份坏 fixture 必须不报）——元验证，
     fixture 是常量，只有真把规则关掉才会静默；
  2. `RULES` 与 `SKILL.md §10` 的 `⚙ ARCHxxx` **一一对应**（`check_skill_sync`）。
     这条此前只是 `SKILL_MAINTENANCE.md §7` 里的一句 MUST，没有任何东西强制。
- `SKILL.md §10` 新增"棕地接入"检查组；§0 分流表、§1 适用场景、§9 文档索引同步。

### Fixed

- **中文 Windows 上 §8 的闸门 1 根本跑不过**：`test_demo.py` / `demo_end_to_end.py` /
  `scaffold_project.py` 缺控制台编码防线。中文本身 GBK 编得出，崩的是 `✓ ⚠ ✅ →` 这类符号；
  CI 在 Linux 上跑，**永远发现不了**。三个脚本补上防线，并在 CI 步骤 7 加了静态检查
  （查 AST 不查子串——注释里提一句、或把调用改名都能骗过子串查法，均已实测）。
- **`.arch-legacy` 带 UTF-8 BOM 时整份配置静默失效**：Windows 记事本存 UTF-8 会写 BOM，
  BOM 会黏在首个包名前面使其匹配不上任何 import，ARCH010 一条都不报——而输出与
  "检查通过"完全一样。改用 `utf-8-sig` 读取。**这条是在元验证里植入 BOM 样例才发现的。**
- 检查器自身的**假绿**再次出现并已修正：元验证脚本里用 `"ARCH010" in stdout` 判断是否命中，
  而"棕地模式未启用"那行说明文字本身就含 `ARCH010`——**断言恒真**。
  改为按 `文件:行:列 ERROR/WARNING ARCHxxx` 格式精确匹配 finding 行。
  同类问题：`RULES` ↔ §10 的对应性检查若只取 `⚙` 后第一个 ID，会把
  `⚙ ARCH001 / ARCH005` 误报为"ARCH005 漏标"。
 

### Changed

- 

### Fixed

- 


## [1.0.0] - 2026-09-19

首个版本：六层架构 + 5/6 条铁律 + 13 篇 `references/` + 5 个可执行脚本 + 事实核查机制。

### Added

- **六层架构**：L1 接入 / L2 编排 / L3 专业 Agent / L4 MCP 工具 / L5 异步协同 / L6 AgentOps 治理
- **五条核心铁律 + 六条配套铁律**（`SKILL.md §3`）
- `references/` 13 篇深度文档（架构 / 规范 / 模式 / 治理 / 多租户 / 安全 / 成本 / 测试 / API）
- `scripts/scaffold_project.py` —— 一键生成**开箱可运行**的项目骨架（含 CLAUDE.md、CI、架构校验脚本）
- `scripts/check_architecture.py` —— AST 架构校验，9 条规则，`--self-test` 保证规则不静默失效
- `scripts/check_facts.py` —— 事实核查：真跑 `FACTS.md` 里的可执行断言 + 过期告警
- `scripts/test_demo.py` / `scripts/test_scaffold.py` —— 离线回归与产物验收（均不需要 LLM key）
- `FACTS.md` —— 易漂移事实的唯一登记处（A 级可执行 13 条 / B 级人工 6 条）
- 发布纪律：`VERSION` + 本文件 + `scripts/check_release.py` + `.github/workflows/ci.yml`

### Fixed

首版开发期间修正的缺陷。**记录在案是为了让后人知道这些坑真实存在过**，
每一条都在 `FACTS.md` 留下了可执行断言，防止复发：

- **虚构的版本差异**（错误曾扩散到 4 个文件）：曾断言"0.x 用 `operator.add`、1.x 改用 `add`"，
  而 `langgraph.graph` **从未导出过 `add`**（F-001）。同类问题还有
  `langgraph-1x-api.md §5.4` 的"v1.x 才要求 thread_id 加 tenant 前缀"（F-016）——
  那是本 skill 自己的多租户约定，不是版本差异。
- **scaffold 产物跑不起来**：生成的图含 `async def` 节点却用同步 `.invoke()`，
  抛 `TypeError: No synchronous function provided`（F-015）。
- **`langchain.prompts` 在 1.x 已不存在**，两处误用，改为 `langchain_core.prompts`（F-014）。
- **demo 的 PG/Redis 运行前提是捏造的**：脚本用 `MemorySaver` 与内存伪工具，从不连接这两个服务。
- **`demo_requirements.txt` 下界无解**：`langchain-openai>=0.1.0` 与 `langchain>=1.0` 无交集（F-017）。
- **检查器自身的假绿**（最隐蔽的一类）：`check_facts.py` 曾把断言跑在临时目录里，
  导致 F-008 的 `glob` 扫到 0 个文件却恒真——期间 `test_demo.py` 真的缺
  `from __future__ import annotations` 而一直没被发现。另有一版 F-017 本身测错了性质，
  靠"植入已知坏样例"的元验证才揪出来。
