# 工程规范（目录 / 端口 / 编码 / Docker）

## 1. 目录与命名规范

- 顶层包按层分：`agents/ orchestration/ tools/ rag/ governance/ database/ backend/ prompts/ cases/ frontend/ models/ deploy/ requirements/ tests/`
- Agent 包内固定结构：`agent.py`（核心+process()节点接口）、`schema.py`（Pydantic）、`prompts.py`（模板）、能力模块（如 ocr.py/extractor.py/validator.py）
- MCP Server 命名：`tools/mcp/servers/<域>_server/`，只含 `server.py` + `tools.py`
- 单例模式统一：`get_xxx()` 工厂函数 + 模块级缓存（或 lru_cache）
- Python 文件头模板（example.py 维护）：Author / Date / Version / Task docstring

## 2. 端口规范（组内 +10 步进，中间留 9 空位）

| 组 | 服务 | 端口 |
|---|---|---|
| MCP | Gateway / Server1 / Server2 / Server3 | 12001 / 12011 / 12021 / 12031 |
| A2A | 外部 Agent1 / Agent2 | 12101 / 12111 |
| 数据库 | Redis / Milvus / PostgreSQL | 12201 / 12211 / 12221 |
| 前端 | Streamlit | 12345（固定） |
| 其他 | API / Prometheus / Grafana / Alertmanager | 12401 / 12411 / 12421 / 12431 |

要点：
- 端口采用非默认值（如 12221 而非 5432）是为了避免与开发机上已运行的本地 PostgreSQL/Redis/Milvus 冲突——多项目并行开发时不必停服务。单人开发且无本地冲突时可改回默认端口，改 .env 即可，代码不感知。
- 宿主机映射端口加偏移防本地冲突；**容器内服务间用内部端口**（postgres:5432 / redis:6379 / milvus:19530）
- 容器内访问宿主机 LLM：`http://host.docker.internal:8000/v1`（Docker Desktop）

## 3. Docker Compose 服务清单

```
api ×N  worker ×M  mcp-server  a2a-mock  postgres  redis  milvus
prometheus  grafana  alertmanager  (可选: otel-collector)
```

- api 与 worker 用**同一镜像、不同 command**（`uvicorn backend.main:app` vs `arq tools.workers.main.WorkerSettings`）
- worker 健康检查：Redis 内心跳 key（`worker:heartbeat`，30s TTL）
- 日志卷挂载 `./logger:/app/logger`（`GOV_LOG_DIR` 可覆盖）
- 模型目录 / 评测结果目录按需 bind mount
- **Windows Docker bind mount 权限坑（必踩）**：Windows Docker Desktop 挂载目录显示 root 所有，非 root 用户写不进 → 日志 PermissionError → 容器反复重启。解法：`deploy/docker-entrypoint.sh`——root 启动 chown 挂载目录 → `setpriv` 降权 appuser 运行 uvicorn，并 `export HOME=/home/appuser`（setpriv 不更新 HOME，否则 asyncpg 读 /root/.postgresql 报权限错）。**新增 bind mount 目录必须同步加入 chown 列表。**
- Grafana 看板与 Prometheus 告警规则目录组织见 `grafana-dashboards.md` §3

## 4. 编码规范

必须：
- async 优先；全量 type hint；模块/函数 docstring
- 配置走 pydantic-settings，禁止散落的 os.getenv
- Prompt 走 Prompt Registry 版本化（结构：Role/Goal/Constraints/Tools/Output Schema/Examples）
- LLM 输出 JSON 解析必须容错（markdown 代码块剥离 + 截取首个 JSON 对象 + fallback 规则）
- 降级路径全部 `logger.warning()` 显式记录

禁止：
```python
except Exception:
    pass                     # 静默吞异常
if user_input == "xxx": ...  # 业务硬编码分支
from service.xxx import yyy  # Agent 直连业务代码（绕过 MCP）
prompt = "固定字符串"          # 硬编码 Prompt
```
- 只用 `StateGraph`（`AgentExecutor` 在 LangChain 1.x 中已被移除、import 不到，见 `FACTS.md` F-004；它是**迁移旧代码**时的清理项，不是新代码的选项）
- 生产禁止静默 mock（OCR/外部系统失败要返回明确错误，不可假装成功）

## 5. 测试与 CI

- **仅 L1/L2 子集**离线/DB-free 可跑：`pytest tests/ -m "not e2e and not contract"`（依赖注入 stub 模式保证）；L3 契约需契约栈、L4 E2E 需真实 LLM——四层划分见 `testing-matrix.md` §1
- 每模块内置冒烟测试：`python -m <module>` 直接运行自检
- CI 流水线：`ruff check` → `pytest` → 镜像构建推送（master）→ e2e（手动触发，docker compose 全栈跑集成断言）
- 评测：`python -m governance.evaluation.runner run --version <v> --datasets <names> [--run-real --use-llm --save-to-db]`

## 6. 数据库规范

- async SQLAlchemy + asyncpg；`get_db()` FastAPI 依赖注入（自动 commit/rollback/close）
- ORM 模型全部 `mapped_column` + comment；Pydantic schema `from_attributes=True` 支持 ORM 直转
- Alembic 迁移命名 `NNNN_<name>.py`（0001 核心表 trace/agent/prompt/evaluation/checkpoint → 0002 a2a_task → 0003 conversation）
- 核心表清单：trace（Agent执行记录）/ agent（配置）/ prompt（版本）/ evaluation（评测结果）/ checkpoint（LangGraph状态）/ a2a_task / conversation(+message)
- 无 alembic 时回退 create_all

## 7. 安全规范清单

- JWT：Bearer Token 强制，无旁路；`/metrics` 与 `/api/a2a/callback`（走 HMAC）是仅有的免 JWT 路径
  - ⚠️ JWT 是**本平台内部约定**。MCP 规范要求 Server 作 OAuth Resource Server（OAuth 2.1 + Protected Resource Metadata RFC 9728）——**对接第三方/外部 MCP 时 Gateway 必须补 OAuth 2.1**，否则规范实现方连不进来（见 `architecture.md` L4、`mcp-platform-integration.md` 场景 B/D）
- RBAC：角色枚举（admin/agent/user...）+ 权限枚举 + MCP Tool 级鉴权
- A2A 回调：HMAC-SHA256 签名 + ±300s 时间窗口防重放（**自定约定**：A2A v1.0 只定义 `PushNotificationConfig{url,token,authentication}`，**重试策略规范未定义**——HMAC 与时间窗是我们的实现选择，非协议要求）
- CORS：白名单制（localhost:前端端口,localhost:grafana端口），可 .env 扩展
- 护栏前置：`execute_agent()` 在 graph.ainvoke 前跑 `GuardrailRunner.run_input()`
- PII 脱敏四类：手机 / 身份证 / 邮箱 / 银行卡（正则 + 掩码）

## 8. 无状态与内存治理（[修订] 企业级红线）

- **进程内禁止持有业务可变状态**：配置/模型句柄可以单例；checkpoint、缓存、任务、指标计数必须外置（PG/Redis/prometheus_client Registry）
- 一切内存缓存/列表必须有界：LRU（容量上限）或 TTL（过期清理），启动时打印容量配置
- Graph 构图：`build_graph()` 保持纯函数语义，编译图不可变；禁止"边构图边注册全局副作用"
- 自研件引入门禁：每引入一个自研基础设施件（checkpointer/指标导出/队列/序列化），必须先写一段 ADR 说明生态件为何不满足——默认答案是"不满足不成立，用生态件"

## 9. 多租户隔离（[修订] P5 前必须定案）

- 起步（单租户部署）：所有业务表带 `tenant_id` 字段 + 所有查询强制 tenant_id 过滤（repository 层统一注入，禁止散落拼接）
- 进阶（SaaS/多委办局）：PostgreSQL RLS（Row Level Security）做库级隔离，连接层注入 `SET app.tenant_id`
- 配套：租户级限流（Redis 令牌桶）、租户级配额（trace/LLM token 月度上限）、看板按租户维度聚合
- 原则：**隔离策略后补成本极高**——建表第一天就带 tenant_id，哪怕先全填默认值

## 10. 配套文档索引

本章只列"目录/端口/编码/Docker"基础规范。下面这些专项主题有独立文档，**先读本文再按需深入**：

| 主题 | 文档 |
|---|---|
| 架构决策记录（ADR）模板与门禁 | `adr-template.md` |
| SLO / 灾备 / 容量规划 | `slo.md` |
| LLM 成本治理（配额 / 路由 / 缓存） | `llm-cost.md` |
| LLM 安全（注入 / 投毒 / 泄露） | `llm-security.md` |
| 多租户精细化（向量 / Redis / Checkpoint） | `multi-tenant.md` |
| MCP 平台集成边界 | `mcp-platform-integration.md` |
| 测试矩阵 | `testing-matrix.md` |
| Grafana Dashboard 模板 | `grafana-dashboards.md` |
| Prompt 版本治理与 A/B | `prompt-versioning.md` |
| LLM 路由与多供应商 | `llm-routing.md` |
| LangGraph 1.x API 速查 | `langgraph-1x-api.md` |
