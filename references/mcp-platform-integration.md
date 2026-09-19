# MCP 平台集成边界（自建 Gateway 与 WorkBuddy MCP / 官方 MCP / 本地 MCP）

> SKILL.md 提到"自建 MCP Gateway 做多租户工具治理"，但没说明它与 WorkBuddy 平台已有 MCP 工具的边界。
> 本文档给出 4 类 MCP 来源的取舍矩阵。

---

## 1. MCP 来源四象限

```
                    平台托管（WorkBuddy MCP）
                           ▲
                           │
           ┌───────────────┼───────────────┐
           │               │               │
   平台托管 │   场景 A      │   场景 B      │
   本地自建 │   场景 C      │   场景 D      │
           │               │               │
           └───────────────┼───────────────┘
                           │
                           ▼
                     本地 / 私有部署
```

| 来源 | 示例 | 治理边界 | 适用 |
|---|---|---|---|
| **A. 平台官方 MCP** | WorkBuddy MCP Connector | 由平台治理（认证、计费、审计） | 调用平台已对接的 SaaS（邮件/CRM/数据库） |
| **B. 第三方官方 MCP** | Anthropic MCP、AWS MCP | 自行治理或对接平台 | 跨云能力 |
| **C. 自建 MCP（直连）** | 本地 mcp_server.py | 完全自治理 | 单人小项目、内网工具 |
| **D. 自建 MCP Gateway** | tools/mcp/gateway.py | 统一治理多源 MCP | 多租户/多 MCP 来源的企业级平台 |

**关键问题**：你的项目到底用哪个？

---

## 2. 决策树：是否需要自建 MCP Gateway？

```
是否需要按租户隔离工具调用？
  ├─ 否 → 直连模式（场景 C）
  └─ 是 → 是否需要统一审计 / 计费 / 限流？
              ├─ 否 → 平台 MCP（场景 A）或本地 MCP（场景 C）
              └─ 是 → 自建 Gateway（场景 D）
```

### 2.1 场景 A：纯平台 MCP 调用（最简单）

```python
# tools/mcp/client.py —— 直接复用 WorkBuddy MCP Connector
from mcp import ClientSession  # 平台提供的 SDK

class PlatformMCPClient:
    """直接调用平台 MCP，无需自建 Gateway。"""
    async def call_tool(self, name: str, arguments: dict) -> dict:
        # 平台负责 JWT/RBAC/审计/计费
        async with ClientSession() as session:
            return await session.call_tool(name, arguments)
```

**适用**：项目只需要调平台已有的 SaaS 工具（飞书/钉钉/数据库等），不需要私有工具。

### 2.2 场景 C：本地 MCP 直连（小项目）

```python
# Agent 直接连本地 mcp_server，跳过 Gateway
class LocalMCPClient:
    async def call_tool(self, name: str, arguments: dict) -> dict:
        # 直连本地 stdio / HTTP
        return await self._direct_call(name, arguments)
```

**适用**：单租户 / 内部工具 / PoC 阶段。

### 2.3 场景 D：自建 MCP Gateway（企业级）

当同时存在**多租户** + **多 MCP 来源** + **统一审计/计费**需求时，必须自建 Gateway。

典型场景：
- 同时调用平台 MCP（飞书）+ 第三方 MCP（AWS）+ 自建 MCP（内部业务系统）
- 不同租户能调用的 MCP 工具不同（如金融租户不能调邮件工具）
- 需要把 MCP 调用统一计入 LLM 成本

---

## 3. 自建 MCP Gateway 的架构（场景 D）

```
Agent → MCP Client → MCP Gateway (JWT+RBAC+审计+计费)
                       │
                       ├─→ 平台 MCP Connector（透明转发）
                       ├─→ 第三方 MCP（Anthropic/AWS）
                       └─→ 自建 MCP Server（业务系统）
```

### 3.1 Gateway 路由表

```python
# tools/mcp/gateway/router.py
class MCPRouter:
    def __init__(self):
        self.routes: dict[str, str] = {}     # tool_name -> backend

    def register(self, tool_name: str, backend: str):
        self.routes[tool_name] = backend

    def backend_for(self, tool_name: str) -> str:
        if tool_name not in self.routes:
            raise UnknownToolError(tool_name)
        return self.routes[tool_name]


# 初始化（来自 .env 或配置中心）
router = MCPRouter()
router.register("send_email",   "platform:feishu")     # 平台 MCP
router.register("query_db",     "self:postgres_server") # 自建 MCP
router.register("aws_s3_list",  "thirdparty:aws")       # 第三方 MCP
```

### 3.2 统一审计

```python
# tools/mcp/gateway/middleware.py
@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    tool_name = request.path_params["tool_name"]
    tenant_id = request.state.tenant_id
    user_id = request.state.user_id
    t0 = time.perf_counter()
    response = await call_next(request)
    await audit_log(
        actor=user_id, tenant=tenant_id, tool=tool_name,
        latency_ms=(time.perf_counter() - t0) * 1000,
        status=response.status_code,
    )
    return response
```

### 3.3 平台 MCP 与自建 MCP 的协议差异处理

WorkBuddy MCP 走平台 SDK（带 Bearer Token 自动注入）；自建 MCP 走 JSON-RPC over HTTP。

Gateway 需要**适配层**：

```python
class PlatformMCPAdapter:
    async def call(self, tool_name: str, args: dict) -> dict:
        # 调用平台 MCP SDK
        ...

class SelfMCPAdapter:
    async def call(self, tool_name: str, args: dict) -> dict:
        # HTTP JSON-RPC
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{settings.self_mcp_base}/{tool_name}",
                json=args,
                headers={"Authorization": f"Bearer {self.token}"},
            )
            return r.json()

class ThirdPartyMCPAdapter:
    async def call(self, tool_name: str, args: dict) -> dict:
        # Anthropic/AWS MCP 协议
        ...

# Gateway 统一接口
async def dispatch(tool_name: str, args: dict, tenant_id: str) -> dict:
    backend = router.backend_for(tool_name)
    if backend.startswith("platform:"):
        return await PlatformMCPAdapter().call(tool_name, args)
    elif backend.startswith("self:"):
        return await SelfMCPAdapter().call(tool_name, args)
    elif backend.startswith("thirdparty:"):
        return await ThirdPartyMCPAdapter().call(tool_name, args)
```

---

## 4. RBAC 在不同 MCP 来源下的实现

| MCP 来源 | RBAC 控制点 | 工具级权限 |
|---|---|---|
| 平台 MCP | 平台 RBAC（自动） | 平台分配的工具白名单 |
| 第三方 MCP | Gateway 端 RBAC + 第三方 IAM | Gateway 配置 `tenant_tool_acl` |
| 自建 MCP | 自建 MCP Server 端 JWT 校验 | Server 内 `_require_role` |

**统一抽象**：在 Gateway 入口处完成鉴权，后端不做二次校验（除非特别敏感工具）。

```python
# tools/mcp/gateway/auth.py
TOOL_ACL: dict[str, dict[str, set[str]]] = {
    "send_email":   {"platform:feishu", {"admin", "agent"}},
    "query_db":     {"self:postgres_server", {"admin", "agent"}},
    "aws_s3_list":  {"thirdparty:aws", {"admin"}},  # 仅 admin
}

def check_acl(tool_name: str, tenant_id: str, user_role: str) -> bool:
    backend, allowed_roles = TOOL_ACL.get(tool_name, ({}, set()))
    if user_role not in allowed_roles:
        audit_denied(tool_name, tenant_id, user_role)
        return False
    return True
```

---

## 5. 计费集成

### 5.1 平台 MCP 调用

平台自动计费（费用计入平台账户）。Gateway 只透传，不重复计费。

### 5.2 第三方 MCP 调用

Gateway 必须记录调用次数与 token（如 AWS MCP 走的是 AWS 凭据），按租户聚合：

```python
THIRDPARTY_USAGE = Counter(
    "thirdparty_mcp_usage_total",
    "Third-party MCP usage",
    ["tenant_id", "tool_name", "provider"],
)

async def track_thirdparty_call(tenant_id, tool_name, provider):
    THIRDPARTY_USAGE.labels(
        tenant_id=tenant_id, tool_name=tool_name, provider=provider,
    ).inc()
```

### 5.3 自建 MCP 调用

本地工具调用成本可忽略，但建议仍记录调用次数用于容量评估。

---

## 6. 选型决策表

| 项目阶段 | 租户数 | MCP 来源数 | 推荐 |
|---|---|---|---|
| PoC | 1 | 1 | 场景 C 直连 |
| 单租户 | 1 | 多 | 场景 C 直连 + 适配层 |
| 小规模 SaaS | 2-10 | 1-3 | 场景 A 平台 MCP |
| 中等 SaaS | 10-100 | 3-10 | 场景 D 自建 Gateway |
| 大型 SaaS / 多委办局 | >100 | >10 | 场景 D + 完整 RBAC + 计费 |

---

## 7. 渐进式迁移路径

**阶段 1（PoC）**：场景 C 直连
**阶段 2（试运营）**：抽象 `MCPRouter`，但仍直连
**阶段 3（规模化）**：插入 Gateway（保留 router 配置）
**阶段 4（多云/多供应商）**：Gateway 内增加第三方适配器

每个阶段的成本都很低——只要一开始就用 `MCPRouter` 抽象而不是 hardcode 调用 URL。

---

## 8. 禁止事项

- ❌ **禁止**让 Agent 直接 import 平台 MCP SDK（绕过 Gateway，绕过审计/计费）
- ❌ **禁止**同一项目混用"自建 Gateway"和"直连模式"——只能选其一（除非有过渡期兼容层）
- ❌ **禁止**把平台 MCP 凭据写死在自建 MCP Server 里（凭据应由平台统一管理）
- ❌ **禁止**第三方 MCP 直接外连到租户内网（必须经过 Gateway）