# ADR 模板（Architecture Decision Record）

> 决策可追溯是企业级治理的最低底线。每一次"选 A 不选 B"都必须留痕。
> 本文件采用 MADR 4.0 模板 + gov_AP 真实案例。

## 0. 什么时候必须写 ADR

满足以下任一条件，**强制 ADR**：

- 引入一个**自研**基础设施件（checkpointer / 指标导出 / 队列 / 序列化 / 限流器等）
  - 默认答案必须是"生态件不满足"——举证责任在引入方。
- 选型 A vs B 涉及**长期维护成本**（数据库 / 消息队列 / 协议 / ORM）
- 修改**核心铁律**（§3 五条铁律中的任一条）
- 引入/下线一个**生态依赖**（如升级 LangGraph 主版本）
- 跨**多租户 / 合规 / 计费**边界的设计决策

不要求 ADR 的：

- 业务 Agent 新增、prompt 模板微调、UI 样式
- 单文件内部重构

---

## 1. 模板（复制即用）

文件位置：`docs/adr/NNNN-<kebab-case-title>.md`（NNNN 四位递增，从 0001 起）。

```markdown
# NNNN. <简短标题>

- 状态：Proposed | Accepted | Deprecated | Superseded by NNNN
- 日期：YYYY-MM-DD
- 决策人：@author1 @author2

## Context（背景与约束）

要解决的什么问题？当时的技术约束是什么？涉及的上下游模块？

## Decision（决策）

我们决定...

## Options Considered（备选方案）

### Option A — <名字>
- 优点：...
- 缺点：...

### Option B — <名字>
- 优点：...
- 缺点：...

## Consequences（后果）

- 正面：...
- 负面：...
- 风险：...

## Rollback Plan（回滚路径）

如何撤销？需要多少工作量？回滚后业务影响？
```

---

## 2. 真实案例（ADR-0003：checkpointer 选官方 PostgresSaver）

> 这是 gov_AP v3.0 评审时最关键的决策之一。

```markdown
# 0003. Checkpointer 使用官方 PostgresSaver，禁止自研

- 状态：Accepted
- 日期：2025-11-20
- 决策人：@platform-team

## Context

LangGraph 的 checkpointer 是整个运行时无状态化的基石——它承担了
A2A 挂起恢复、人工审批中断、retry 重规划三个核心场景的持久化需求。

v1.x 之前的 gov_AP 内部有个 `BaseCheckpointSaver` 自研抽象，
用 JSONB + 自研 schema 存 PG，存在以下问题：
1. 与 LangGraph 0.x → 1.x 升级路径冲突（官方接口签名变了 3 次）
2. 自研序列化无法利用官方 `__pickle__` 优化，5MB checkpoint 写入耗时 800ms+
3. 每次升级 LangGraph 都需要 fork 修改兼容层

## Decision

**所有 checkpointer 必须用官方 `langgraph-checkpoint-postgres`（`AsyncPostgresSaver`）。
禁止自研 BaseCheckpointSaver。**

A2A 挂起/恢复通过 `thread_id = f"{tenant_id}:{trace_id}"` 组合官方 saver 实现，
不引入第二套持久化格式。（`trace_id` 仍是全链路那一个 ID；`tenant_id` 前缀是
`multi-tenant.md` §4.2 的隔离约定，**不是** LangGraph 的版本要求。）

## Options Considered

### Option A — 自研 BaseCheckpointSaver（现状）
- 优点：完全可控，协议可定制
- 缺点：维护成本随 LangGraph 升级线性增长；序列化性能差；新成员上手难

### Option B — 官方 PostgresSaver（采纳）
- 优点：零维护，跟随官方版本演进；社区共享 bug 修复；性能优化免费
- 缺点：受限于官方 schema 演进（但 LangGraph 已承诺 BC 兼容）

### Option C — RedisSaver
- 优点：延迟低（<5ms）
- 缺点：AOF/RDB 持久化策略与"企业级数据不丢"冲突；超出 Redis 容量后丢 checkpoint

## Consequences

- 正面：升级 LangGraph 1.x → 2.x 时无需 fork；多租户场景可天然利用 PG RLS；
  可与现有 PG 备份/审计体系共用
- 负面：每次 LangGraph 升级需回归测试（QA 投入约 2 人日）
- 风险：若官方在某次升级破坏 BC（极小概率），需冻结升级点

## Rollback Plan

如果官方 PostgresSaver 出现严重问题：
1. 短期：用 Option C RedisSaver 兜底（接受丢 checkpoint 的风险）
2. 中期：fork 官方版本内部维护，但必须先升级 ADR 为 Deprecated
3. 长期：等待官方修复或迁移到 Option A（需要 8 人周以上投入）
```

---

## 3. 与铁律的关联

| 铁律 | 相关 ADR 类型 |
|------|------|
| #5 生态优先 | 所有"自研基础设施件"类 ADR 的默认立场必须是"用生态件" |
| #4 运行时无状态 | checkpointer / 指标 / 队列选型 ADR |
| #3 工具标准化 | MCP 选型、tool schema 版本演进 |

---

## 4. ADR 评审 checklist（PR 阶段执行）

- [ ] 状态字段已填（Proposed / Accepted / Deprecated / Superseded）
- [ ] Options Considered 至少列 2 个备选
- [ ] Consequences 区分了正面/负面/风险
- [ ] Rollback Plan 有具体步骤
- [ ] 与现有 ADR 无矛盾（如矛盾需在 PR 描述里说明 supersede 关系）
- [ ] 若引入自研件，必须有"为什么生态件不满足"的举证

---

## 5. 索引维护

根目录 `docs/adr/README.md` 必须维护一张决策总览表：

```markdown
# ADR 索引

| 编号 | 标题 | 状态 | 日期 |
|------|------|------|------|
| 0001 | 六层架构定义 | Accepted | 2025-09-01 |
| 0002 | 多租户隔离策略 | Accepted | 2025-10-15 |
| 0003 | Checkpointer 选官方 PostgresSaver | Accepted | 2025-11-20 |
| 0004 | ... | Proposed | 2026-01-10 |
```

任何人新加 ADR 时同步更新此表——这是新人最快了解项目决策脉络的入口。