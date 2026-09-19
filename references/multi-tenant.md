# 多租户精细化（PG RLS 之外的维度）

> `conventions.md` §9 已经定义了 PG RLS 起步策略。但 LLM 应用还有 4 个新维度必须隔离：
> 向量库、Redis 缓存、Checkpoint、LLM 调用配额。本文档补齐这四层。

---

## 1. 多租户隔离的 6 层

```
┌─────────────────────────────────────────────────────────────┐
│  L1  业务表 (PostgreSQL)      ← RLS 已在 conventions.md §9    │
│  L2  向量库 (Milvus)          ← 本文档 §2 ✨                  │
│  L3  Redis 缓存 (含 LLM 缓存)  ← 本文档 §3 ✨                  │
│  L4  Checkpoint (PostgresSaver) ← 本文档 §4 ✨                │
│  L5  LLM 调用配额 / 计费      ← llm-cost.md 已覆盖一部分       │
│  L6  Prompt Registry / 评测   ← 本文档 §5 ✨                  │
└─────────────────────────────────────────────────────────────┘
```

**黄金法则**：**所有持久化/缓存层都必须把 `tenant_id` 嵌入主键或 schema 字段，禁止仅靠"应用层过滤器"。**

---

## 2. L2：Milvus 向量库隔离

> ⚠️ **版本要求：Milvus 2.5.4+**。`partitionkey.isolation` 配置项自 **2.5.4**（2025-01）才引入，且**默认值为 `false`**——因此锁定 "2.5+" 不够：在 2.5.0~2.5.3 上这个开关根本不存在，等于没有强制隔离手段。

### 2.1 方案对比（按租户规模分档，含官方推荐）

| 方案 | 隔离强度 | 成本 | 适用场景 |
|---|---|---|---|
| **A. 共享 collection + tenant_id 过滤** | 弱 | 低 | 内部小团队、低合规要求 |
| **B. 按租户分 partition**（本文推荐） | 中 | 中 | 大多数企业级 SaaS（**数万租户**量级） |
| **C. 按租户分 collection** | 强 | 高（管理成本） | 金融/医疗等强合规、**少量大租户** |
| **D. Database 级隔离** | 最强 | 最高 | 强合规 / 物理隔离诉求（默认上限 64 库，`maxDatabaseNum`） |
| **E. Partition Key** | 逻辑隔离（非物理） | 低 | **百万级 ToC 租户**（官方推荐档）；不支持 RBAC、不支持热冷分离 |

### 2.1.1 方案 B 的隐藏风险：漏传 `partition_names` 即跨租户泄漏

方案 B 的隔离**完全依赖每处 search 显式传 `partition_names`**——这是人工约定，不是引擎强制。任何一处漏传（重构时、批量脚本里、运维临查中），查询就会**扫描全部分区**，直接跨租户泄漏。三条要求：

1. **禁止业务代码直接拿 `Collection` 对象调用 `search()`**——必须经 §2.2 的 `TenantVectorStore` 统一封装；
2. **若采用方案 E（Partition Key），必须开启 `partitionkey.isolation=true`**：开启后，不含 partition key 等值过滤的表达式（含使用 `OR` 的）会被引擎**直接拒绝**——这是把"人工约定"升级为"引擎强制"的唯一手段。默认 `false` 意味着这个保护是关着的；
3. 方案 B 的兜底仍靠 §2.3 评审清单 + metadata 冗余 `tenant_id` 字段（见 §2.2 代码）。

### 2.2 推荐方案 B：按租户分 partition

```python
# rag/milvus/tenant_partition.py
from pymilvus import Collection, FieldSchema, CollectionSchema, DataType

class TenantVectorStore:
    async def ensure_partition(self, tenant_id: str):
        """惰性创建租户 partition。"""
        coll = Collection("documents")
        partition_name = f"tenant_{tenant_id}"
        if not coll.has_partition(partition_name):
            coll.create_partition(
                partition_name=partition_name,
                description=f"Partition for tenant {tenant_id}",
            )

    async def search(self, tenant_id: str, query_embedding, top_k: int):
        coll = Collection("documents")
        await self.ensure_partition(tenant_id)
        # 关键：search 时必须指定 partition
        return coll.search(
            data=[query_embedding],
            anns_field="embedding",
            param={"metric_type": "IP"},
            limit=top_k,
            partition_names=[f"tenant_{tenant_id}"],   # ← 隔离核心
            output_fields=["id", "content", "source", "tenant_id"],
        )

    async def insert(self, tenant_id: str, docs: list[dict]):
        await self.ensure_partition(tenant_id)
        coll = Collection("documents")
        # 必须强制写入 tenant_id 字段（即使 partition 已隔离，作为冗余防御）
        for d in docs:
            d["tenant_id"] = tenant_id
        coll.insert(docs, partition_name=f"tenant_{tenant_id}")
```

### 2.3 强制隔离的代码评审清单

- [ ] search 调用是否带了 `partition_names` 参数？
- [ ] insert 调用是否带了 `partition_name` 参数？
- [ ] 即使 partition 已隔离，文档 metadata 是否仍带 `tenant_id` 字段？
- [ ] 跨租户查询（如管理员代查）是否有专门的审计日志？

### 2.4 跨租户检索（管理员场景）

```python
async def admin_cross_tenant_search(admin_id: str, tenant_ids: list[str], query):
    """管理员代查：必须记录审计日志，绝不能用于生产数据访问。"""
    await audit_log(
        actor=admin_id, action="cross_tenant_search",
        tenants=tenant_ids, query_hash=hash(query),
    )
    # 仅合并多个租户的 partition 结果，不直接查无 partition 模式
    results = []
    for tid in tenant_ids:
        results.extend(await store.search(tid, query))
    return results
```

---

## 3. L3：Redis 缓存隔离（含 LLM 语义缓存）

### 3.1 缓存键命名规范（强制）

```
{业务域}:{层}:{租户ID}:{业务标识}
```

| 业务 | 键示例 |
|---|---|
| LLM 语义缓存 | `llm:cache:tenant_123:a1b2c3d4...` |
| 会话状态 | `session:tenant_123:user_456` |
| 限流令牌桶 | `quota:tenant_123:rpm` |
| 工具结果缓存 | `tool:cache:tenant_123:tool_name:hash` |
| A2A 任务锁 | `a2a:lock:tenant_123:task_xxx` |

### 3.2 实现要点

```python
# backend/services/cache_keys.py
class CacheKey:
    """所有缓存键的生成器，强制带 tenant_id。"""

    @staticmethod
    def llm_cache(tenant_id: str, query_hash: str) -> str:
        return f"llm:cache:{tenant_id}:{query_hash}"

    @staticmethod
    def quota_rpm(tenant_id: str) -> str:
        return f"quota:{tenant_id}:rpm"
```

### 3.3 跨租户违规检测（自动化扫描）

CI 流水线中加一条规则：

```bash
# scripts/check_tenant_isolation.sh
# 扫描所有 redis key 模式，必须包含 tenant_id
grep -rn 'redis\.get\|redis\.set\|cache\.get\|cache\.set' backend/ \
  | grep -v 'tenant_id' \
  | grep -v '# tenant-isolated' \
  && exit 1
```

---

## 4. L4：Checkpoint 隔离

### 4.1 风险

LangGraph `PostgresSaver` 用 `thread_id` 隔离不同 session。
如果不显式带 `tenant_id`，**租户 A 可能通过 trace_id 命中租户 B 的 checkpoint**。

### 4.2 必须的 thread_id 命名规范

```python
# orchestration/langgraph/state.py（升级 create_initial_state）
def create_initial_state(user_query: str, trace_id: str, tenant_id: str) -> AgentState:
    return AgentState(
        # 关键：thread_id 必须是 tenant_id:trace_id 的组合
        # PostgresSaver 用 thread_id 做 namespace 隔离
        thread_id=f"{tenant_id}:{trace_id}",   # 实际通过 config 传入
        trace_id=trace_id,
        tenant_id=tenant_id,        # 显式字段冗余
        user_query=user_query,
        # ...
    )

# 调用处
config = {
    "configurable": {
        "thread_id": f"{tenant_id}:{trace_id}",   # 必须带 tenant_id
    }
}
await graph.ainvoke(state, config)
```

### 4.3 A2A 恢复场景

A2A callback 恢复时必须校验 tenant：

```python
# tools/a2a/callback.py
async def resume_after_callback(task_id: str):
    task = await a2a_task_store.get(task_id)
    config = {
        "configurable": {
            "thread_id": f"{task.tenant_id}:{task.source_trace_id}",
        }
    }
    # 校验：调用方 tenant 必须等于 callback 中的 tenant
    if task.tenant_id != current_request.tenant_id:
        raise PermissionError("跨租户恢复尝试被阻止")
    graph = build_graph(checkpointer=saver)
    return await graph.ainvoke({"external_result": artifact}, config)
```

---

## 5. L5：Prompt Registry 与评测数据隔离

### 5.1 Prompt 按租户分组

```sql
-- prompts 表
CREATE TABLE prompt (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,         -- 系统级 prompt 为 '__shared__'
    name VARCHAR(128) NOT NULL,
    version INT NOT NULL,
    content TEXT NOT NULL,
    is_active BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, name, version)
);

CREATE INDEX idx_prompt_tenant ON prompt(tenant_id, name) WHERE is_active;
```

加载时优先级：**租户自定义 > 平台共享**。

### 5.2 评测数据隔离

```sql
-- cases 表
CREATE TABLE evaluation_case (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    name VARCHAR(128),
    input JSONB NOT NULL,
    expected_output JSONB,
    -- 禁止跨租户查询
    CHECK (tenant_id IS NOT NULL)
);

-- RLS 策略（同 conventions.md §9）
ALTER TABLE evaluation_case ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON evaluation_case
    USING (tenant_id = current_setting('app.tenant_id')::VARCHAR);
```

### 5.3 跨租户共享（白名单机制）

某些场景需要平台运营方共享评测用例给多个租户：

```python
class EvaluationCaseAccess:
    ALLOWED_SHARED_CASES = {"__platform_smoke__", "__security_red_team__"}

    async def get(self, case_id: int, tenant_id: str) -> EvaluationCase:
        case = await self.repo.get(case_id)
        if case.tenant_id == tenant_id:
            return case
        if case.name in self.ALLOWED_SHARED_CASES:
            return case
        raise PermissionError("跨租户评测用例访问被阻止")
```

---

## 6. 统一 Repository 层（防散落过滤）

禁止每个查询手动 `WHERE tenant_id = ?`。建立统一基类：

```python
# database/repository.py
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

class TenantScopedRepository:
    """所有租户隔离表的 Repository 基类。"""

    def __init__(self, session: AsyncSession, tenant_id: str):
        self.session = session
        self.tenant_id = tenant_id
        # 关键：每个连接自动注入 tenant_id 到 PG 会话
        await session.execute(
            text("SET LOCAL app.tenant_id = :tid"),
            {"tid": tenant_id},
        )

    async def list(self, model):
        stmt = select(model)
        # 即使漏写 WHERE，RLS 也会兜底拦截
        return await self.session.execute(stmt)

# 使用
async def get_my_cases(session: AsyncSession, tenant_id: str):
    repo = TenantScopedRepository(session, tenant_id)
    return await repo.list(Case)
```

---

## 7. 迁移检查清单（已有项目接入多租户）

如果现有项目**未做**多租户隔离，按以下顺序补齐（成本从低到高）：

1. **第一步**：所有业务表加 `tenant_id` 字段 + 默认值 + 索引（半天）
2. **第二步**：PG RLS 启用 + Repository 基类（1 天）
3. **第三步**：Redis 缓存键全部加 tenant_id 前缀（半天，自动化扫描）
4. **第四步**：LangGraph thread_id 改造（1 天）
5. **第五步**：Milvus 迁移到 partition 模式（最重，需要重新 ingest）
6. **第六步**：Prompt / Cases 加 tenant_id（半天）

总成本：约 1 周。**越往后越贵**——这是为什么 §9 说"建表第一天就带 tenant_id"。

---

## 8. 总结：6 层隔离检查矩阵

| 层 | 隔离机制 | 实现位置 | 兜底防御 |
|---|---|---|---|
| PG 业务表 | RLS + tenant_id 列 | conventions §9 | Repository 基类 |
| Milvus | partition_names | 本文档 §2 | metadata tenant_id |
| Redis 缓存 | key 前缀 | 本文档 §3 | CI 扫描 |
| Checkpoint | thread_id 拼 tenant_id | 本文档 §4 | 恢复时校验 |
| LLM 调用 | 令牌桶 + quota 表 | llm-cost.md §2 | Redis 计数器 |
| Prompt / Cases | tenant_id 列 + RLS | 本文档 §5 | 共享白名单 |

任何一层缺失都是 P0 风险。**多租户不是单点决策，是 6 层一致行动**。