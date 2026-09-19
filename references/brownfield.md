# 棕地接入（在已有系统旁长出 Agent 层）

> 绿地（新项目）跑 `scaffold_project.py` 就完了。**棕地**指的是：已有一套在跑的系统
> （ERP / 工单 / 计费 / 老的政务后台），你不想也不能停掉它重写，只想在旁边长出 Agent 能力。
>
> 本文给出一条**可回滚的渐进路径** + 一道**机器可检的防腐边界**。
> 配套检查器规则：`ARCH010`（绕过反腐蚀层直连遗留）/ `ARCH011`（遗留反向依赖新层）。

---

## 1. 先记住三件不能做的事

| 不能做 | 为什么它一定会失败 |
|---|---|
| **大爆炸重写**（停旧系统，重写完再上线） | 你会在第 6 个月发现业务规则只存在于旧系统的代码和某人的记忆里。而且这期间业务还在变。 |
| **双写**（一次请求同时写新旧两套） | 两套的一致性永远对不上，且**没人能说清以谁为准**。回滚时数据已经分叉。 |
| **Agent 直连遗留系统**（Agent 里直接 `from legacy_erp import ...`） | 遗留系统的模型（字段名、金额单位、错误码）会渗进 Agent 层。它每改一次，你改一片——**你成了它的下游**，而不是它的消费者。 |

第三条就是**反腐蚀层**存在的理由。它也是最容易被跳过的一条，因为"先跑通再说"太诱人了——
所以本 skill 把它做成了 CI 里的断言（`ARCH010`），而不是文档里的一段劝告。

---

## 2. 反腐蚀层（ACL）是什么，放在哪

反腐蚀层（Anti-Corruption Layer，DDD 的概念）是**新架构与遗留系统之间唯一的翻译层**：

```
Agent (L3)
  └─ MCP Client
       └─ MCP Gateway
            └─ MCP Server  tools/mcp/servers/<系统>/      ← 遗留系统在这里被包成一个普通工具
                 ├─ acl/                                   ← 反腐蚀层：唯一允许 import 遗留系统的地方
                 │    ├─ models.py   遗留模型 → 新架构契约
                 │    └─ adapter.py  调用 + 错误翻译 + 幂等
                 └─ server.py        对 Agent 只暴露工具名与 JSON Schema
```

**关键点：对 Agent 来说，遗留系统就是一个普通的 MCP 工具。**
翻译的脏活全部封在 `acl/` 里面，Agent 层完全不知道对面是 1998 年的 SOAP 服务还是 Excel。

位置是**有讲究的**——`acl/` 必须落在 L4 工具层里（路径同时含 `tools` 与 `acl` 段），
`check_architecture.py` 只认这个位置。理由：放错层的 ACL 不是 ACL，只是又一个直连点
（`backend/acl/` 里的东西照样能被 `backend/` 直接 import 走）。

### ACL 的三项职责（只做这三件）

1. **模型翻译**：遗留的字段名 / 单位 / 空值语义 → 新架构的契约。
   例：遗留金额是「分」且键名 `AMT`，对外必须是 `{"amount": <元>}`。
2. **错误翻译**：遗留的错误码 / 超时 / 脏数据 → 新架构的错误分类（可重试 / 不可重试 / 降级）。
   **绝不把遗留的异常类型透出去**——那等于把对方的类型系统引进来了。
3. **幂等**：给写操作补上幂等键。遗留系统多半没有，而 Agent 会重试。

**ACL 不做的事**：不做业务决策、不做流程编排、不缓存业务状态。
一旦你发现自己在 `acl/` 里写 `if 客户等级 == "VIP"`，说明业务逻辑跑错层了——
它是防腐层，不是新的业务层。

### 骨架

```python
# tools/mcp/servers/erp/acl/adapter.py
from __future__ import annotations

from loguru import logger

from legacy_erp.client import ErpClient          # ← 全项目唯一允许出现这一行的地方（ARCH010）
from legacy_erp.errors import LegacyNotFound, LegacyTimeout

from tools.mcp.servers.erp.acl.models import Invoice   # 新架构的契约（本目录内定义）


class ErpAcl:
    """遗留 ERP 的反腐蚀层：模型翻译 + 错误翻译 + 幂等。"""

    def __init__(self, client: ErpClient) -> None:
        self._client = client

    async def fetch_invoice(self, invoice_id: str) -> Invoice | None:
        try:
            raw = await self._client.get_invoice(invoice_id)
        except LegacyTimeout as exc:                 # ← 遗留异常在这里终止，不外传
            logger.warning("legacy_erp 超时，降级为空结果: {}", exc)
            return None                              # 降级必须显式（铁律：降级路径可读）
        except LegacyNotFound:
            return None
        # 模型翻译集中在这一处，别处再出现 `AMT` / `/100` 就是漏了
        return Invoice(id=raw["ID"], amount_cents=raw["AMT"])
```

---

## 3. 渐进路径：五个阶段，每阶段可回滚

**每个阶段都必须先写清退出条件与回滚动作再动手。**没有回滚路径的迁移不是迁移，是赌博。

### 阶段 0 —— 止血（不改遗留系统一行代码）

- **做**：在项目根写 `.arch-legacy`（一行一个遗留顶层包名），把 `ARCH010` 接进 CI；
  建好 `tools/mcp/servers/<系统>/acl/` 空目录。
- **进入条件**：遗留系统能跑；有至少一个明确的、值得做成 Agent 的场景。
- **退出条件**：人工在 `backend/` 里 import 一次遗留包，CI **真的变红**。
  （没验证过红灯的规则等于没有规则——见 `SKILL_MAINTENANCE.md §7`。）
- **回滚**：删掉 `.arch-legacy`，规则自动失效，零影响。

> `.arch-legacy` 为空文件时 `check_architecture.py` 会**直接报错退出**（exit 2），
> 不是静默放行。因为"写了配置文件但规则没生效"和"检查通过"的输出一模一样。

### 阶段 1 —— 只读接入（遗留代码零改动）

- **做**：选**一个只读**用例，Agent 经 ACL 读遗留数据。
- **退出条件**：该场景端到端可跑；ACL 有**对拍测试**（同一输入，ACL 输出 vs 直连遗留的原始输出）；
  遗留系统代码 diff 为空。
- **回滚**：下线路由开关，Agent 路径整条不启用。

### 阶段 2 —— 影子运行（新旧并行，不对外生效）

- **做**：同一请求同时走新 Agent 路径与遗留路径，**只比较、不采信**，差异进报表。
- **退出条件**：连续 N 天 / M 次请求，差异率低于约定阈值，且**每一条差异都被解释过**
  （是 bug 还是遗留系统本身的怪癖？没解释过的差异会在阶段 3 变成事故）。
- **回滚**：关影子开关。因为不对外生效，回滚是零风险的——这也是阶段 2 的价值所在。

### 阶段 3 —— 写路径灰度（最危险的一步）

- **做**：带幂等键的写操作，按租户 / 百分比灰度放量。
- **退出条件**：**人为注入过重复回调与超时**，幂等与补偿脚本演练通过；对账脚本连续无差异。
- **回滚**：开关回退 + 补偿脚本执行（所以补偿脚本必须先写好并演练，而不是出事再写）。

### 阶段 4 —— 拆遗留

- **做**：一个模块一个模块地把遗留系统下线。此时 `ARCH011` 开始真正发力。
- **退出条件**：`ARCH011` 零告警，且**遗留目录的代码被删掉**（不是留着"以防万一"）。
- **为什么 `ARCH011` 决定这一步能不能做完**：只要遗留目录里有一处
  `from agents.x import y`，遗留系统就成了新架构的调用方，**你永远删不掉它**——
  拆除路径被这一行切断了。这就是 `ARCH011` 存在的全部理由。

---

## 4. 机器能查什么，查不了什么

**能查（`check_architecture.py`）**：

| 规则 | 查什么 | 严重度 |
|---|---|---|
| `ARCH010` | 非 ACL、非测试的文件 import 了 `.arch-legacy` 里列的包 | error |
| `ARCH011` | 遗留目录里的文件 import 了六层的顶层包（依赖方向倒置） | warning |

启用方式（二选一，可叠加）：

```bash
# 项目根放 .arch-legacy（一行一个顶层包名，# 起注释）
echo "legacy_erp" > .arch-legacy
python scripts/check_architecture.py .

# 或走命令行（CI 里临时用）
python scripts/check_architecture.py . --legacy legacy_erp,billing_old
```

> ⚠️ **Windows 上别用 PowerShell 的 `>` 写这个文件**：PowerShell 5.1 的 `>` 默认写
> UTF-16，`Out-File` 默认也带 BOM。前者会直接报错退出（不会被误读成"通过"），
> 后者已被 `utf-8-sig` 兼容。用编辑器存 UTF-8，或
> `Set-Content -Encoding utf8 .arch-legacy "legacy_erp"`。

`tests/` 与 `scripts/` 被放行——**对拍测试必须直连遗留系统取对照值**，
否则你测的是 ACL 自己跟自己一致，那没有意义。

**查不了（必须人工）**：

- ACL 是否**真的**在翻译模型，还是把遗留的字段原样透传（规则看不出 `AMT` 与 `amount` 的区别）。
- 幂等是否真的幂等——要注入重复请求才算验过。
- 「双写」——规则看不到你在两个地方各写了一次。
- 阶段 2 的差异率是否"已被解释"。

**别把这份清单当成"跑完就合规"**。规则只覆盖"确定的形状"，
棕地的风险主要在语义层面，那部分只能靠评审和演练。

---

## 5. 哪些铁律在棕地可以折中，哪些绝对不行

| 铁律 | 棕地下的处理 |
|---|---|
| #1 Agent 优先，禁止业务 if/else | ⚠️ **可暂缓**：棕地允许确定性策略与 Agent 并存。但**新写的分支不许是 `if user_input == "xxx"` 直调**，且过渡期的确定性逻辑必须显式标注（注释写明"临时，阶段 N 后由 Agent 接管"）。 |
| #2 所有 Agent 可观测 | ❌ **不可折中**。棕地调试成本本来就高，没有 trace 你连"新路径到底跑没跑"都不知道。 |
| #3 工具标准化（MCP + Prompt Registry） | ❌ **不可折中**。ACL 必须挂在 MCP Server 内部，**不能**给遗留系统另开一条旁路。 |
| #4 运行时无状态 | ❌ **绝对不可折中**。遗留系统的进程内状态正是它拆不掉的原因；新层再沾上一样的状态，就等于把棕地的病传染过来。 |
| #5 生态优先，自研收敛 | ⚠️ 容易破防：ACL 看着像"自研适配层"。但它属于自研预算里**本来就该占位的那一个**（业务差异化），前提是别再顺手自研 checkpointer / metrics。 |

---

## 6. 常见失败模式

1. **ACL 长成胖适配器**：业务规则一点点挪进去，半年后它成了新的遗留系统。
   症状：`acl/` 里的文件比 `server.py` 大一个量级。
2. **双写**："为了平滑过渡，两边都写"。没有"以谁为准"的双写一定对不上，
   而对不上时你已经在生产环境了。
3. **Agent 直连遗留数据库**：比直连 API 更糟——遗留系统自己的 schema 变更会**同时**打断你和它。
4. **`.arch-legacy` 进了仓库但没进 CI**：规则存在但没人跑，等于没有。
   症状：CI 里只有 `pytest`，没有 `check_architecture.py`。
5. **遗留目录反向依赖新层**（`ARCH011`）：某次"顺手复用一下新工具"就切断了拆除路径，
   而且是**静默**发生的——代码照样跑，只是你再也删不掉旧系统了。
6. **把回滚留到出事再想**：阶段 3 的补偿脚本必须是**先写好并演练过**的，
   而不是出事当晚现写。

---

## 7. 相关文档

- `SKILL.md §10` —— 评审 checklist（`⚙ ARCH010` / `⚙ ARCH011` 两条）
- `references/architecture.md` —— 六层职责与依赖方向（ACL 为何属 L4）
- `references/mcp-platform-integration.md` —— MCP Server 的 4 类来源决策树
- `references/conventions.md §8` —— 无状态红线
- `references/adr-template.md` —— 迁移方案要写 ADR（阶段划分属架构决策）
