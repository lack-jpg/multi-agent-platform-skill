# SKILL_MAINTENANCE.md — 本 skill 的自我维护约定

> 位置：`C:\Users\le\.workbuddy\skills\multi-agent-platform\SKILL_MAINTENANCE.md`
> 读者：负责修改本 skill 的人类或 AI（同等于下游项目的 CLAUDE.md 角色）。

## 1. 仓库结构（修改前先认路）

```
multi-agent-platform/
├── SKILL.md           ← 入口：六层架构 + 5/6 条铁律 + 评审 checklist（≤ 300 行）
├── README.md          ← 5 分钟入门 + 文档地图 + 适用边界
├── SKILL_MAINTENANCE.md ← 本文件：维护本 skill 自己的约定
├── FACTS.md           ← 易漂移事实清单（外部库/协议断言 + 核实方式 + 日期）
├── VERSION            ← 版本号唯一来源（机器可读），与 CHANGELOG 顶条目必须一致
├── CHANGELOG.md       ← 变更记录 + 版本号规则（Keep a Changelog / semver）
├── .gitignore
├── .github/workflows/ci.yml  ← 本 skill 自身的 CI（跑下面全部闸门）
├── references/        ← 深度文档（按主题分文件，不堆在一处）
│   ├── architecture.md / conventions.md / patterns.md     ← 核心三篇（必读）
│   ├── adr-template.md / slo.md                          ← 治理运营
│   ├── llm-cost.md / llm-security.md / llm-routing.md / prompt-versioning.md
│   ├── multi-tenant.md / mcp-platform-integration.md
│   ├── brownfield.md                                     ← 棕地接入 + 反腐蚀层（ACL）
│   ├── grafana-dashboards.md / testing-matrix.md / langgraph-1x-api.md
└── scripts/           ← 可执行：脚手架 / 一致性校验 / 端到端 demo / 离线回归
    ├── scaffold_project.py
    ├── check_architecture.py
    ├── check_facts.py          ← 事实核查（FACTS.md 的可执行断言 + 过期告警）
    ├── check_release.py        ← 发布纪律（VERSION ↔ CHANGELOG 一致 + --bump）
    ├── test_scaffold.py        ← scaffold 产物验收（生成→冒烟→pytest→架构校验）
    ├── demo_end_to_end.py
    ├── test_demo.py
    ├── demo_requirements.txt   ← 跑 demo 的最小依赖集
    └── ci_requirements.txt     ← 跑全部闸门的最小依赖集（比上面宽）
```

> 注意：`.github/workflows/ci.yml` 是**本 skill 自己的** CI；scaffold 生成的**下游项目**
> CI 是另一份模板，在 `scripts/scaffold_project.py` 的 `CI_YML` 里。改一个不会影响另一个。

## 2. 章节 / 文件职责

| 位置 | 职责 | 不该出现 |
|---|---|---|
| `SKILL.md §1-3` | 何时用、架构总览、铁律 | 具体协议示例（移到 references/） |
| `SKILL.md §10` | 评审 checklist | 详细代码片段（链接到 patterns.md） |
| `references/` 单文件 | 单一主题深度，≥ 5KB 自成一篇 | 多个不相关主题拼贴 |
| `scripts/scaffold_*.py` | 产物自动可跑、零依赖运行 | 与 references 内容重复 |
| `scripts/check_architecture.py` | 架构一致性校验，规则表与 SKILL.md §10 一一对应（`--self-test` 强制） | 发明 §10 之外的规则（会变成两份真理） |
| `references/brownfield.md` | 棕地接入路径 + 反腐蚀层（ACL）的落点/职责/五阶段迁移 | 与 `architecture.md` §5 重复讲分层（那篇讲"是什么"，这篇讲"怎么迁"） |
| 下游项目的 `.arch-legacy` | 声明"哪些顶层包是遗留系统"，据此启用 ARCH010/011 | 放进本 skill 仓库（它描述的是**下游项目**的事实） |
| `scripts/test_scaffold.py` | scaffold 产物验收：生成即可跑，坏产物必须失败 | 把产物缺陷写成"已知问题"绕过 |
| `scripts/check_facts.py` | 事实核查：跑 `FACTS.md` 里的可执行断言 + 过期告警 | 把"未安装依赖"报成"通过"（会造成假绿） |
| `scripts/check_release.py` | 发布纪律：`VERSION` ↔ `CHANGELOG.md` 一致 + `--bump` | 把版本号只写在 CHANGELOG 里（两份来源必然漂） |
| `FACTS.md` | 易漂移外部断言的唯一登记处，含核实方式与日期 | 登记无法证伪的主张（那不是事实断言） |
| `VERSION` / `CHANGELOG.md` | 版本号的机器可读来源 / 人类可读历史 | 两处不一致（`check_release.py` 会拦） |
| `scripts/ci_requirements.txt` | 跑全部闸门的最小依赖集 | 与 `demo_requirements.txt` 混淆（后者只够跑 demo） |
| `scripts/test_demo.py` | 离线回归，全绿为门槛 | 依赖 OPENAI_API_KEY |

**`FACTS.md` 的登记规则**（新增外部断言时必读）：

1. 先问"**这条能不能写成一段跑得起来的断言？**"。能写就必须写成 A 级——写不出代码，
   往往说明当初就没真验过（本 skill 的 `add` 虚构断言就是这么漏过去的）。
2. 只有真需要外部信源（官方文档、release notes、服务端行为）的才登记为 B 级，
   并写清**去哪查**，不能只留一个日期。
3. 改了任何文档里的版本/API 断言 → 同步改 `FACTS.md` 对应条目并 `--stamp`。
4. 发现"文档与实测不符"时，先判断**是不是自己查错了源**（例：MCP 的 SDK 常量本就落后
   于规范日期，见 `FACTS.md` F-010 备注）——别急着改文档。
5. **能写进代码的数字，不要只写在文档里**。价格、模型型号这类"文档说是 X、厂商页说是 Y"
   的断言，登记成 B 级只是次优解（日期拦不住写错）；更好的做法是让代码在运行时从配置读，
   文档就不再持有会漂的数字。**先问"这个数字非得写死在文档里吗"**——见 `FACTS.md` F-018 备注。
6. **判断一条断言属于"上游契约"还是"我们自己的规矩"**：后者**不该**登记进 `FACTS.md`
   （它不会漂，只会被我们自己改）。本 skill 已两次把自家约定误写成版本差异（`add`、`thread_id`，
   见 F-001 / F-016）。Prompt 版本号规范（`v3.2.1-canary`）就属于"自家规矩"，故未登记。

## 3. 命名约定

- 文件名：`kebab-case.md`（`architecture.md`、`testing-matrix.md`）。
- 章节锚：`## N. 标题` 编号在 SKILL.md 强制保留（外部稳定锚点）；`references/` 可不编号。
- 图节点名 / Agent 名：`snake_case`，与代码一致（如 `policy_node`、`supervisor`）。
- Markdown 内代码块必须声明语言（```python / ```bash / ```yaml）。

## 4. frontmatter 规则

`SKILL.md` 顶部的 frontmatter 由消费方解析，**禁止**随意改字段：

- `name`：保持 `multi-agent-platform`，改名会破坏宿主注册的 skill 名。
- `description`：≤ 1024 字符。要修改需同时：
  1. 保持"正向 trigger + 不适用 trigger"对称结构；
  2. 在 README 的"何时使用本 Skill"表格同步更新触发词。
- 添加新 frontmatter 字段前先确认宿主是否支持（WorkBuddy / Claude Code / Cursor 各家 schema 不同）。

## 5. 什么是破坏性变更

修改以下任意一项**必须**在 README 顶部加一行 ⚠️ 提示，并写 ADR（`docs/adr/NNNN-title.md`）：

- 端口规范（`conventions.md §端口规范`）
- 六层架构的边界（`SKILL.md §2`）
- 铁律增删（`SKILL.md §3`）
- 目录结构（`SKILL.md §5`）
- checkpointer / metrics / checkpointer 等生态件选型

非破坏性变更（typo、补一句说明、加 references）无需 ADR，但需在 commit message 说明。

## 6. 添加新 references

1. 在 `references/` 新增 `<topic>.md`，自包含、单一主题。
2. 在 `SKILL.md §9 参考文档` 表格中加一行，指明分类（核心 / 治理 / 多租户 / 质量）。
3. 在 `README.md` 的"文档地图"同步加一行。
4. 如果新文档改变了某条铁律的可执行性，**回去更新 SKILL.md §10 评审 checklist**。

## 7. 修改 / 新增 scripts

- 所有脚本必须 **Python 3.12+ 兼容**，使用 `from __future__ import annotations`。
- ⚠️ **`scripts/` 下每个脚本都必须有控制台编码防线**（那段 `for _stream in (sys.stdout, sys.stderr): ... reconfigure(encoding="utf-8")`）。
  中文本身 GBK 编得出，**崩的是 `✓ ⚠ ✅ →` 这类符号**——Windows 本地跑 `test_demo.py`
  会直接 `UnicodeEncodeError`，而 **CI 跑在 Linux 上永远发现不了**。
  已踩过：`test_demo.py` / `demo_end_to_end.py` / `scaffold_project.py` 三个脚本漏了这段，
  意味着 §8 的闸门 1 在中文 Windows 上**根本跑不过**。
  CI 步骤 7 静态检查此条，**查 AST 不查子串**——注释里提一句 `reconfigure`、
  或把调用改名成 `reconfigure_DISABLED`，都会骗过子串查法（都实测过）。
  无参数的 `reconfigure()` 也不算数：它不改编码。
- 任何对 `demo_end_to_end.py` 或 `scaffold_project.py` 的修改都必须：
  1. 保持 `python scripts/test_demo.py` **全绿**（不依赖 API key）；
  2. 保持 `python scripts/test_scaffold.py` **全绿**——它才是 scaffold 的验收门槛：
     生成到临时目录，跑 `python -m orchestration.langgraph.graph`（冒烟）、`pytest tests/`、
     `check_architecture.py`、FastAPI 路由注册，四级全绿才算产物可用。改完 scaffold 必须跑。
  3. 如果改了生成的目录结构或文件命名，回到 `README.md` §5 分钟入门同步；
  4. 如果引入新依赖，更新 `scripts/demo_requirements.txt` 并在 PR 描述里标注。
- 新增脚本前先想：能不能用现有 `scaffold_project.py` 加一个子命令？优先扩而不是堆文件。
- **已记录的例外 1**：`check_architecture.py` 是独立脚本，不是 `scaffold_project.py` 的子命令。理由：两者依赖方向相反——scaffold **生成**项目，check_architecture **读取**项目；且后者要对**下游项目自己的 CI** 可用，塞进生成器会让下游为了做检查必须先复制整个脚手架脚本。
- **已记录的例外 2**：`check_release.py` 是独立脚本。理由：它查的是**本 skill 自己的发布纪律**（`VERSION` ↔ `CHANGELOG`），与"下游项目的架构"和"外部事实"都无关；塞进 `check_architecture.py` 会让后者的规则表与 `SKILL.md §10` 的 `⚙ ARCHxxx` 标注脱钩（§7 下一条要求两者一一对应）。零依赖，可单独进 CI。
- `check_architecture.py` 的 `RULES` 表与 `SKILL.md §10` 的 `⚙ ARCHxxx` 标注**必须一一对应**——增删规则时两边同改。这条由 `--self-test` 强制（下一节）。
- **新增检查规则时，先问它该不该是 opt-in**。需要项目提供额外事实才能判定的规则
  （如 ARCH010/011 要先知道"哪些包算遗留"），必须由项目根的一个配置文件启用，
  缺省不参与——否则会在每个绿地项目里误报，用户只能整条关掉，规则就废了。
  反过来：**配置文件存在却解析不出内容时必须报错退出**，不能静默放行。
  "配了但没生效"和"检查通过"的输出一模一样（本 skill 反复踩的假绿）。
- 它有内置自检 `python scripts/check_architecture.py --self-test`，断言三件事：
  1. **全部规则可触发**（无静默失效）——开发中已踩过一次：重复定义 `visit_ClassDef`
     让 ARCH002 整条失效。加规则时同步补 `_BAD_FIXTURE`，否则自检会以"规则静默失效"报错。
  2. **合规样例零误报**。
  3. **棕地规则确实是 opt-in**——不开 `.arch-legacy` 跑同一份坏 fixture，ARCH010/011
     必须不报。这条是元验证：fixture 是常量，只有真把规则关掉才会静默。少了它，
     "规则按项目启用"就只是文档里的一句话。
  4. **`RULES` 与 `SKILL.md §10` 的 `⚙ ARCHxxx` 一一对应**（见下条）。
  **改判定逻辑或加规则后必须跑。**
- ⚠️ 写涉及 `visit_*` 的规则时**只能有一个** `visit_Import` / `visit_ImportFrom`，
  多条规则共用。后定义的方法会覆盖先定义的，先定义那条会**整体静默失效**且输出与"通过"一样
  （ARCH002 就是这么死的）。ARCH004 / ARCH010 / ARCH011 三条共用同一个 `_check_import`。
- `RULES` 与 `SKILL.md §10` 的对应性**已由 `--self-test` 强制**（`check_skill_sync`）。
  之前它只是 §7 里的一句"MUST"，没有任何东西拦——又是一处靠自觉。
  ⚠️ 该检查**必须收一整行里的所有 ID**：§10 存在 `⚙ ARCH001 / ARCH005` 这种多 ID 写法，
  只取 `⚙` 后第一个会误报 ARCH005/ARCH008"漏标"（已踩过一次）。
- `--self-test` 已纳入 `test_scaffold.py` 的 L1 级别（**先自检再校验**）——理由：检查器静默失效时，它给出的"0 error"与"检查通过"完全一样，会把下游验收变成假绿。**不要**把它移回手动跑。
- `test_scaffold.py` 分级执行，依赖缺失时**跳过并说明**（不是静默通过）：L1 静态（永远跑）/ L2 运行（需 langgraph）/ L3 服务（需 fastapi）。CI 用 `--strict` 让跳过也失败。
- ⚠️ 已知陷阱：写产物断言时注意 `typing.get_type_hints` 默认 `include_extras=False` 会**剥掉 `Annotated`**，导致"检查 reducer"的测试永远通过（假绿）。已踩过一次，必须传 `include_extras=True`。
- `check_facts.py` 与 `check_architecture.py` 同款纪律，四条硬规则：
  1. **条目代码块嵌在列表项下会带缩进**，执行前必须 `textwrap.dedent`——否则断言会以
     `IndentationError` "失败"，失败原因是解析而不是断言本身（假信号）。已踩过一次。
  2. **"未安装依赖"必须报 SKIP，绝不能报通过**——否则清单会在没装包的机器上全绿。
     反向也成立：**装了就不能报 SKIP**，否则覆盖被静默削掉。导入名 ≠ 发行名的包
     （`python-dotenv`→`dotenv`、`pyyaml`→`yaml`、`pillow`→`PIL`）必须两个都查。
  3. **断言的工作目录是 skill 根目录，不是脚本所在的临时目录**。条目里用相对路径
     （`Path("scripts").glob(...)`）是常态；cwd 错了，glob 到 0 个文件，断言会**恒真**。
     已踩过一次：F-008 空转过一段时间，期间 `test_demo.py` 真的缺
     `from __future__ import annotations` 却一直报 ok。**写带 glob/读文件的断言时，
     必须同时断言"扫到的数量 > 0"**——否则它退化了你不会知道。
  4. 自检 `python scripts/check_facts.py --self-test` 主动塞入一条**已知为假**的断言，
     断言它必须被判 FAIL（而非 OK 或 SKIP）。加/改判定逻辑后必跑。
  > 贯穿这四条的是同一个失败模式：**假绿**。它比断言失败危险得多——输出和"通过"
  > 一模一样。它有两种变体，都要防：
  > 1. **断言恒真**（根本没测到东西）——如 cwd 错了导致 glob 到空。对策：断言里加
  >    "扫到的数量 > 0"。
  > 2. **断言跑了、也测了，但测的不是你以为的那条性质**——F-017 第一版就栽在这：
  >    它验的是"实装版本满足下界"，而真正的 bug 是"下界跨代导致**整组**无解"，
  >    前者对着旧下界照样通过。这版断言即使跑起来也永远抓不到目标缺陷。
  > **唯一可靠的防法是元验证**：写完断言，把**已知的坏样例植进去**，确认它真的变红。
  > 本 skill 对 F-002、F-008、F-016、F-017 都做过这一步——其中 F-017 正是靠元验证
  > 才发现第一版是摆设。**没做过元验证的断言，不算数。**
- `FACTS.md` 的 B 级（人工）条目是**弱保证**：日期只能提醒"该看了"，拦不住虚构。
  新增条目时优先想办法升级成 A 级。

## 8. 修改前必跑的六件事

0. `python scripts/check_release.py` → 与 `VERSION` / `CHANGELOG.md` 一致。
   **只要这次改动动了接口、文档结构或事实断言，就得在 `CHANGELOG.md` 留痕**
   （事实更正走 PATCH，但必须写进 `Fixed`）。
1. `python scripts/test_demo.py` → 必须全绿（demo 回归）。
2. `python scripts/test_scaffold.py` → 必须全绿（scaffold 产物验收；改了 scaffold 就必跑）。
3. `python scripts/check_facts.py` → 无 FAIL。**改了任何版本 / API 断言就必跑**；
   条目过期（STALE）不算错，但要么复核后 `--stamp`，要么说明为何暂不处理。
4. `wc -l SKILL.md` → 不超过 **300** 行（写死数字会漂，直接跑命令看）。
   历史上限 200 行，2026-09-19 由维护者决定放宽到 300。理由：SKILL.md 实际承载
   11 节（§0 分流表 / §1 适用边界 / §2 六层总览 / §3 铁律 / §4 内聚规则 /
   §5 目录树 / §6 技术栈 / §7 落地流程 / §8 模式速查 / §9 文档索引 / §10 评审 checklist），
   200 行会迫使把"铁律"或"checklist"这类**每次都要看**的内容拆到 references/，
   反而增加加载成本。
   放宽的是**行数**不是**职责**：SKILL.md 仍只承载架构原则与索引，
   具体协议示例、代码骨架、运营细则一律进 references/（见 §2 职责表）。
5. 通读一遍 `SKILL.md §10 评审 checklist` → 把变更过的项打勾或补充。

## 9. 不该做的事

- ❌ 把"业务实现细节"塞进 SKILL.md——它只承载架构原则。
- ❌ 把上限放宽到 300 行当作"可以往里堆"的许可——放宽的是行数不是职责（§8.3）。
- ❌ 在 README / SKILL.md 里出现"9.x / 10"这种自评分——交给用户评测。
- ❌ 把"触发词"挤进 description 末尾时超过 1024 字符——超出会被宿主截断。
- ❌ 删除或重组 references/ 中的文件而不更新 README 文档地图——会留死链。
- ❌ 修改 SKILL.md §6 技术栈锁定版本时不做完整回归——版本错配会带来连锁故障。
- ❌ **凭印象写"vX 与 vY 的差异"而不实测**。本 skill 曾断言"0.x 用 `operator.add`、1.x 改用 `add`"——
  而 `langgraph.graph` 从未导出过 `add`（实测 1.1.6 只有 `add_messages`），`Annotated[list, reducer]`
  的机制两代一致，**这个版本差异是虚构的**；错误扩散到 4 个文件。
  写版本差异前必须先在实际安装的版本上跑一遍验证（`python -c "import X; ..."`），
  并把核实版本号写进正文。同理：宣称"某 API 不存在/已废弃"也要先实测。

## 10. 版本与变更记录

**版本号规则只有一份，在 `CHANGELOG.md` 顶部**（MAJOR = §5 的破坏性变更 /
MINOR = 新增 references 或 scripts / PATCH = typo、措辞、事实更正）。
本文件**刻意不复述**——两份真理必然漂，这是本 skill 反复踩的坑
（同 `check_architecture.py` 规则表必须与 `SKILL.md §10` 一一对应的道理）。

升版本：`python scripts/check_release.py --bump patch|minor|major`
（改 `VERSION` + 在 CHANGELOG 插入空条目）。**插入的条目必须填内容——空条目等于没记。**

一致性由 `python scripts/check_release.py` 校验，CI 第一步就跑。

**留痕粒度**：
- MAJOR：写 ADR 说明动机，每年最多 1 次。
- MINOR：随时可发，但 `README.md` 文档地图必须同步更新。
- PATCH：直接合，无须 ADR；但**事实更正必须写进 `Fixed`**——否则后人不知道文档曾错过。

**总体约定**：**先改 README 索引，再改 references，最后改 SKILL.md**——倒过来做容易忘同步。

## 11. CI 与本地闸门

`.github/workflows/ci.yml` 在 push / PR 时跑完 §8 的全部闸门，顺序是
**先自检、再校验**（检查器静默失效时输出与"通过"一样，必须先证明它活着）。

CI 的两个刻意选择，改动时别"顺手优化"掉：

1. **`python-version: "3.12"`，不用更新的**。3.12 是 `SKILL.md §6` 声明的最低版本，
   而本机开发在 3.14——`FACTS.md` F-008 明确承认过"本机验不出 3.12 的差异"。
   这一格就是用来补那个盲区的，换成 3.14 等于把盲区搬进 CI。
2. **`test_scaffold.py --strict`**。CI 里依赖是齐的，不该出现跳过；`--strict` 让跳过也算失败。

⚠️ **CI 不能替代本地跑**：它在你 push **之后**才知道结果。CI 是兜底，不是第一道防线。
另外 CI 里不会有跳过——`check_facts.py` 的依赖已全部写进 `scripts/ci_requirements.txt`
（包括 F-007 要的两个 psycopg 包），**本地若看到 SKIP 而 CI 没有，说明是本机缺包**。
