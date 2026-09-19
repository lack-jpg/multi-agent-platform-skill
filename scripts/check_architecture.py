"""架构一致性校验（conformance check）——把 SKILL.md §10 的评审 checklist 变成可失败的断言。

设计目标：让"架构"从**人读的清单**变成**机器可检查的契约**。架构约定改一处时，
下游项目能在 CI 里立刻知道哪些地方漂移了。

用法：
    python scripts/check_architecture.py [项目路径] [选项]

    python scripts/check_architecture.py ./my_agent_app
    python scripts/check_architecture.py . --format json
    python scripts/check_architecture.py . --select ARCH001,ARCH002
    python scripts/check_architecture.py . --ignore ARCH009
    python scripts/check_architecture.py . --fail-on warning
    python scripts/check_architecture.py --list-rules
    python scripts/check_architecture.py --self-test     # 改规则后必跑

退出码：
    0 = 通过（或仅有未达 --fail-on 阈值的 warning）
    1 = 存在达阈值的 finding
    2 = 用法/路径错误

豁免：在被标记语句所在行或结束行的同行注释里写 `# arch: ignore`
     或只豁免某条规则 `# arch: ignore[ARCH002]`。
     定点豁免时请在注释里补一句原因（如 A2A 替换语义字段）。

零依赖：仅标准库（ast / argparse / json / re / pathlib）。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Windows GBK 控制台下输出 ✓ ⚠ ✅ 这类符号会 UnicodeEncodeError（中文本身 GBK 编得出，
# 崩的是符号）。**scripts/ 下每个可执行脚本都要有这一段**——CI 跑在 Linux 上永远发现不了。
# stdout 与 stderr 都要处理——错误信息走 stderr，只改 stdout 会让报错反而乱码。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


# ============================================================
# 规则元数据
# ============================================================
@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    severity: str  # "error" | "warning"
    title: str
    hint: str


RULES: tuple[Rule, ...] = (
    Rule(
        "ARCH001", "bare-except-pass", "error",
        "静默吞异常（except: pass）",
        "改为 logger.warning(...) 记录，或显式降级（结果标注 mode=stub）",
    ),
    Rule(
        "ARCH002", "state-list-no-reducer", "error",
        "TypedDict 中 list/dict 字段缺 reducer（重放会翻倍或丢数据）",
        "加 Annotated[list, add]（messages 字段须用 langgraph.graph.add_messages，"
        "见 langgraph-1x-api.md §3.1）；确需替换语义时加注释 `# arch: ignore[ARCH002]` 并说明",
    ),
    Rule(
        "ARCH003", "custom-checkpointer", "error",
        "自研 checkpointer（违反铁律 #5 生态优先）",
        "改用官方 AsyncPostgresSaver（langgraph-checkpoint-postgres）",
    ),
    Rule(
        "ARCH004", "agent-imports-business", "error",
        "Agent 层直接 import 业务/L1 实现（绕过 MCP，违反铁律 #3）",
        "改为经 MCP 协议调用：Agent → MCP Client → Gateway → Server",
    ),
    Rule(
        "ARCH005", "hardcoded-prompt", "warning",
        "疑似硬编码 Prompt 长文本（违反铁律 #3）",
        "移入 Prompt Registry 版本化（prompts/），代码里只引用 key+version",
    ),
    Rule(
        "ARCH006", "business-if-else", "warning",
        "疑似按用户输入硬编码分支（违反铁律 #1 Agent 优先）",
        "改为 Supervisor → Planner → Router 路由；确定性规则下沉到 Agent 内部策略",
    ),
    Rule(
        "ARCH007", "blocking-call-in-async", "warning",
        "async 函数内同步阻塞调用（会卡死事件循环）",
        "用 await asyncio.sleep / httpx.AsyncClient；确需同步请丢进 Worker 队列",
    ),
    Rule(
        "ARCH008", "print-instead-of-logger", "warning",
        "源码用 print 而非 logger（规范要求 loguru + trace_id）",
        "改用 from loguru import logger；CLI 冒烟测试的 print 请放进 __main__ 块",
    ),
    Rule(
        "ARCH009", "node-without-observability", "warning",
        "LangGraph 节点未接入 trace/metrics（违反铁律 #2）",
        "节点内调用 record_agent_call() + AgentTracer.span()，成功失败都要记",
    ),
    Rule(
        "ARCH010", "legacy-import-outside-acl", "error",
        "绕过反腐蚀层直接 import 遗留系统（棕地接入的前提被破坏）",
        "把访问收敛进 ACL：`tools/mcp/servers/<系统>/acl/`，由它做模型翻译 + 错误翻译 + 幂等；"
        "见 references/brownfield.md §2",
    ),
    Rule(
        "ARCH011", "legacy-imports-new-layers", "warning",
        "遗留目录反向依赖新架构层（strangler 的拆除路径被切断）",
        "新层单向依赖遗留系统（经 ACL），遗留系统不感知新层；见 references/brownfield.md §6",
    ),
)
RULES_BY_ID: dict[str, Rule] = {r.id: r for r in RULES}

# 依赖方向（architecture.md §5）：backend → orchestration → agents → tools → rag/governance/database
# Agent 层不得直接触达下列实现层（必须经 MCP）
BUSINESS_LAYERS = frozenset({"backend", "services", "routers", "api", "database"})

# ---- 棕地（brownfield）：反腐蚀层 ----------------------------------------
# ARCH010 / ARCH011 是**按项目启用**的：项目根目录没有 `.arch-legacy` 就整条不参与，
# 因此 scaffold 生成的绿地项目零误报。
LEGACY_FILE = ".arch-legacy"

# 反腐蚀层的**落点**：L4 工具层里的 `acl/` 目录。
# 两个段都要有——只认 `acl` 会放过"ACL 放错层"，而放错层的 ACL 不是 ACL，
# 只是又一个直连点。测试由 is_script_or_test 单独放行（brownfield.md §4）。
ACL_REQUIRED_SEGMENTS = ("tools", "acl")

# ARCH011 用：遗留目录反向 import 这些顶层包 = 依赖方向倒置。
# **故意不含** `database` / `services` 这类通用名——遗留系统常有同名模块，列进来必误报。
NEW_LAYER_PACKAGES = frozenset({
    "backend", "orchestration", "agents", "tools", "rag", "governance",
})

# 节点可观测性标识（出现任一即视为已接入）
OBSERVABILITY_NAMES = frozenset({
    "record_agent_call", "AgentTracer", "tracer", "span", "start_trace",
    "AGENT_CALLS", "AGENT_LATENCY", "metrics", "record_mcp_call",
})

# 同步阻塞调用：模块名 → 该模块下视为阻塞的方法
BLOCKING_MODULES: dict[str, frozenset[str]] = {
    "time": frozenset({"sleep"}),
    "requests": frozenset({"get", "post", "put", "patch", "delete", "head", "request"}),
    "urllib": frozenset({"urlopen", "urlretrieve"}),
}

# 官方 checkpointer 白名单：继承这些是允许的；继承其它 *CheckpointSaver 视为自研
OFFICIAL_SAVERS = frozenset({
    "PostgresSaver", "AsyncPostgresSaver",
    "SqliteSaver", "AsyncSqliteSaver",
    "MemorySaver", "InMemorySaver",
})

PROMPT_CTORS = frozenset({"SystemMessage", "HumanMessage", "AIMessage", "ChatPromptTemplate"})
PROMPT_MIN_LEN = 100  # 超过此长度的字面量提示词视为硬编码

NODE_SUFFIX = "_node"

SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "env", "__pycache__", "build", "dist",
    "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache", "migrations",
})

_IGNORE_RE = re.compile(r"arch:\s*ignore")
_SCOPED_RE = re.compile(r"\[(ARCH\d{3})\]")


@dataclass
class Finding:
    rule: Rule
    file: str
    line: int
    col: int
    detail: str

    def as_dict(self) -> dict:
        return {
            "rule": self.rule.id,
            "name": self.rule.name,
            "severity": self.rule.severity,
            "file": self.file,
            "line": self.line,
            "col": self.col,
            "detail": self.detail,
            "hint": self.rule.hint,
        }


# ============================================================
# 豁免：`# arch: ignore` / `# arch: ignore[ARCH002]`
# ============================================================
def _ignores(source_lines: list[str], start: int, end: int, rule_id: str) -> bool:
    """语句首行或末行的同行注释声明豁免即生效。

    - `# arch: ignore`              → 豁免该语句涉及的所有规则
    - `# arch: ignore[ARCH002]`     → 只豁免列出的规则
    - `# arch: ignore[ARCH002] 原因` → 定点豁免 + 说明（推荐写法）
    """
    for lineno in {start, end}:
        if not (1 <= lineno <= len(source_lines)):
            continue
        line = source_lines[lineno - 1]
        if "#" not in line or not _IGNORE_RE.search(line):
            continue
        comment = line.split("#", 1)[1]
        scoped = _SCOPED_RE.findall(comment)
        if not scoped or rule_id in scoped:
            return True
    return False


def _base_name(base: ast.expr) -> str | None:
    """取基类/注解的末段名：`X` / `mod.X` / `X[...]` 都归一为名字。"""
    if isinstance(base, ast.Subscript):
        base = base.value
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return None


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _dotted(func: ast.expr) -> str:
    """`time.sleep` / `requests.get` / `urlopen` —— 用于报错信息。"""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return _call_name(func) or "<call>"


def _top_package(parts: tuple[str, ...]) -> str:
    """文件所属的顶层包名；`src/` 布局向下多看一层。

    只用于 ARCH011 判断"这个文件在不在遗留目录里"，与 import 的解析无关。
    """
    if not parts:
        return ""
    if len(parts) >= 2 and parts[0] == "src":
        return parts[1]
    return parts[0]


# ============================================================
# 单文件检查器
# ============================================================
class Checker(ast.NodeVisitor):
    """一个文件跑一遍，逐条规则产出 Finding。"""

    def __init__(
        self,
        rel_path: str,
        source_lines: list[str],
        enabled: frozenset[str],
        legacy: frozenset[str] = frozenset(),
    ) -> None:
        self.rel_path = rel_path
        self.lines = source_lines
        self.enabled = enabled
        self.legacy = legacy
        self.findings: list[Finding] = []

        parts = PurePosixPath(rel_path).parts
        self.is_agent_layer = "agents" in parts
        self.is_script_or_test = (
            "scripts" in parts
            or "tests" in parts
            or PurePosixPath(rel_path).name.startswith("test_")
        )
        # 棕地两条规则的作用域
        self.in_acl = all(seg in parts for seg in ACL_REQUIRED_SEGMENTS)
        self.is_legacy_module = _top_package(parts) in legacy
        self._async_depth = 0
        self._main_block_lines: set[int] = set()

    # ---------- 基础设施 ----------
    def run(self, tree: ast.AST) -> list[Finding]:
        self._collect_main_block(tree)
        self.visit(tree)
        return self.findings

    def _report(self, rule_id: str, node: ast.AST, detail: str) -> None:
        if rule_id not in self.enabled:
            return
        lineno = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", None) or lineno
        if _ignores(self.lines, lineno, end, rule_id):
            return
        self.findings.append(
            Finding(RULES_BY_ID[rule_id], self.rel_path, lineno,
                    getattr(node, "col_offset", 0), detail)
        )

    def _collect_main_block(self, tree: ast.AST) -> None:
        """`if __name__ == "__main__":` 块内允许 print（模块冒烟测试的合法出口）。"""
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and self._is_main_guard(node.test):
                for sub in ast.walk(node):
                    if hasattr(sub, "lineno"):
                        self._main_block_lines.add(sub.lineno)

    @staticmethod
    def _is_main_guard(test: ast.expr) -> bool:
        return (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and any(isinstance(c, ast.Constant) and c.value == "__main__"
                    for c in test.comparators)
        )

    # ---------- ARCH001 静默吞异常 ----------
    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        is_broad = node.type is None or (
            isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}
        )
        # pass / continue / ... 都等于"什么都没做"
        inert = all(
            isinstance(stmt, (ast.Pass, ast.Continue))
            or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
            for stmt in node.body
        )
        if is_broad and inert:
            self._report("ARCH001", node, "except 分支无任何处理，异常被静默吞掉")
        self.generic_visit(node)

    # ---------- ARCH002 缺 reducer + ARCH003 自研 checkpointer ----------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for base in node.bases:
            name = _base_name(base)
            if name and "CheckpointSaver" in name and name not in OFFICIAL_SAVERS:
                self._report(
                    "ARCH003", node,
                    f"`class {node.name}({name})` 疑似自研 checkpointer"
                    f"（应使用官方 PostgresSaver）",
                )

        if any(_base_name(b) == "TypedDict" for b in node.bases):
            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign) or stmt.annotation is None:
                    continue
                if self._is_list_like(stmt.annotation) and not self._has_reducer(stmt.annotation):
                    field = stmt.target.id if isinstance(stmt.target, ast.Name) else "?"
                    self._report(
                        "ARCH002", stmt,
                        f"TypedDict 字段 `{field}` 是 list/dict 但无 reducer"
                        f"（checkpoint 重放会翻倍或覆盖丢数据）",
                    )
        self.generic_visit(node)

    @staticmethod
    def _is_list_like(annotation: ast.expr) -> bool:
        target = annotation.value if isinstance(annotation, ast.Subscript) else annotation
        if isinstance(target, (ast.Name, ast.Attribute)):
            return (target.id if isinstance(target, ast.Name) else target.attr) in {
                "list", "List", "dict", "Dict",
            }
        return False

    @staticmethod
    def _has_reducer(annotation: ast.expr) -> bool:
        """`Annotated[...]` 即认为配了 reducer。"""
        return (
            isinstance(annotation, ast.Subscript)
            and isinstance(annotation.value, ast.Name)
            and annotation.value.id == "Annotated"
        )

    # ---------- import 类规则：ARCH004 / ARCH010 / ARCH011 ----------
    # ⚠️ 三条规则共用这一个 visitor。**不要为某条规则另写 visit_Import**——
    # Python 后定义的会覆盖先定义的，先定义的那条会整体静默失效（本 skill 踩过 ARCH002）。
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_import(alias.name, node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # level > 0 是相对导入，跨不出本包，不可能是遗留系统或新层
        if node.module and node.level == 0:
            self._check_import(node.module, node)
        self.generic_visit(node)

    def _check_import(self, dotted: str, node: ast.AST) -> None:
        top = dotted.split(".")[0]

        # ARCH004：Agent 层不得直连业务/L1 实现
        if self.is_agent_layer and top in BUSINESS_LAYERS:
            self._report(
                "ARCH004", node,
                f"agents/ 直接 import `{dotted}`（应经 MCP 协议调用）",
            )

        # ARCH010：遗留系统只能从反腐蚀层进
        if top in self.legacy and not self.in_acl and not self.is_script_or_test:
            self._report(
                "ARCH010", node,
                f"直连遗留系统 `{dotted}` —— 唯一合法入口是反腐蚀层"
                f"（L4 工具层下的 `tools/**/acl/`）",
            )

        # ARCH011：遗留目录不得反向依赖新架构层
        if self.is_legacy_module and top in NEW_LAYER_PACKAGES:
            self._report(
                "ARCH011", node,
                f"遗留目录 import 了新架构层 `{dotted}` —— 依赖方向倒置，"
                f"遗留系统将永远拆不掉",
            )

    # ---------- ARCH005 硬编码 Prompt + ARCH007 阻塞调用 ----------
    def visit_Call(self, node: ast.Call) -> None:
        func_name = _call_name(node.func)

        if func_name in PROMPT_CTORS:
            for kw in node.keywords:
                value = kw.value
                if (
                    kw.arg == "content"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and len(value.value) >= PROMPT_MIN_LEN
                ):
                    self._report(
                        "ARCH005", value,
                        f"`{func_name}(content=...)` 传入 {len(value.value)} 字符字面量，"
                        f"疑似硬编码 Prompt",
                    )

        if self._async_depth and self._is_blocking(node.func):
            self._report("ARCH007", node, f"async 函数内调用同步阻塞 `{_dotted(node.func)}`")

        self.generic_visit(node)

    @staticmethod
    def _is_blocking(func: ast.expr) -> bool:
        """`time.sleep` / `requests.get` / `urllib.request.urlopen` 三类。"""
        if not isinstance(func, ast.Attribute):
            return False
        if isinstance(func.value, ast.Name):
            allowed = BLOCKING_MODULES.get(func.value.id)
            return bool(allowed) and func.attr in allowed
        # urllib.request.urlopen 形态：模块名在 Attribute 链末端
        if isinstance(func.value, ast.Attribute) and isinstance(func.value.value, ast.Name):
            allowed = BLOCKING_MODULES.get(func.value.value.id)
            return bool(allowed) and func.attr in allowed
        return False

    # ---------- ARCH006 业务 if/else ----------
    def visit_Compare(self, node: ast.Compare) -> None:
        if self.is_agent_layer:
            for op, comparator in zip(node.ops, node.comparators):
                if (
                    isinstance(op, ast.Eq)
                    and isinstance(comparator, ast.Constant)
                    and isinstance(comparator.value, str)
                    and self._looks_like_user_input(node.left)
                ):
                    self._report(
                        "ARCH006", node,
                        f"按用户输入的字符串字面量分支 `== {comparator.value!r}`"
                        f"（疑似硬编码路由）",
                    )
        self.generic_visit(node)

    @staticmethod
    def _looks_like_user_input(node: ast.expr) -> bool:
        """只标记形如 user_input/user_query 的比较，避免误伤 `state["intent"] == "policy"` 这类正常路由。

        ⚠️ 关键字里**不能有裸 `query`**：`state["query"] == "policy"` 是标准的**状态路由**，
        正是本函数要放行的形状，却会因子串命中而被误报（本规则的 docstring 原文就写着
        要避免这类误伤）。`user_query` 仍能被 "user_query" 匹配到，丢掉裸 `query` 只是
        少了一条本就模糊的启发式——漏报远好于误报，误报会让人直接关掉检查器。
        """
        keywords = ("user_input", "user_query", "raw_input", "user_text", "user_message")
        if isinstance(node, ast.Name):
            return any(k in node.id.lower() for k in keywords)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            return any(k in str(node.slice.value).lower() for k in keywords)
        return False

    # ---------- ARCH008 print 代替 logger ----------
    def visit_Expr(self, node: ast.Expr) -> None:
        value = node.value
        if (
            "ARCH008" in self.enabled
            and not self.is_script_or_test
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "print"
            and node.lineno not in self._main_block_lines
        ):
            self._report("ARCH008", node, "源码中使用 print()，规范要求 loguru logger")
        self.generic_visit(node)

    # ---------- ARCH007 作用域维护 + ARCH009 节点可观测性 ----------
    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._async_depth += 1
        try:
            if node.name.endswith(NODE_SUFFIX):
                self._check_node_observability(node)
            self.generic_visit(node)
        finally:
            self._async_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """同步函数体内部**不是** async 上下文——ARCH007 不该往里看。

        没有这一段时 `_async_depth` 只在 AsyncFunctionDef 上加减，于是嵌在 async 节点
        里的普通同步函数（很常见：节点内定义的小工具函数）会被当成 async 体，
        `time.sleep(1)` 被误报成"async 函数内阻塞调用"。
        """
        saved, self._async_depth = self._async_depth, 0
        try:
            self.generic_visit(node)
        finally:
            self._async_depth = saved

    def _check_node_observability(self, node: ast.AsyncFunctionDef) -> None:
        if "ARCH009" not in self.enabled:
            return
        used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
        if not (used & OBSERVABILITY_NAMES):
            self._report(
                "ARCH009", node,
                f"节点 `{node.name}` 内未见 trace/metrics 调用"
                f"（无 AgentTracer / record_agent_call / metrics）",
            )


# ============================================================
# 目录扫描
# ============================================================
def _iter_python_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        # ⚠️ 必须用**相对路径**判断。曾经直接用 path.parts（绝对路径），于是项目只要
        # 位于名为 build/dist/env/venv/migrations 的目录下（`~/build/myapp`、
        # `D:\env\proj`、`/opt/venv/app`），整棵树都会被跳过——检查一条都没跑，
        # 报出来却是"未找到任何 .py 文件"，把排错方向带偏。
        try:
            parts = path.relative_to(root).parts
        except ValueError:      # 理论上不会发生；真发生时不静默跳过
            parts = path.parts
        if any(part in SKIP_DIRS for part in parts[:-1]):
            continue
        yield path


def _encoding_hint(path: Path) -> str:
    """给"读不了"加一句可操作的诊断。

    PowerShell 5.1 的 `>` 重定向默认写 **UTF-16LE**（`Out-File` 的默认编码），
    报 `utf-8 codec can't decode byte 0xff` 对用户毫无指向性。实测过。
    """
    try:
        head = path.read_bytes()[:2]
    except OSError:
        return ""
    if head in (b"\xff\xfe", b"\xfe\xff"):
        return ("　← 这是 UTF-16 编码（PowerShell 的 `>` 重定向默认写 UTF-16）。"
                "请用编辑器另存为 UTF-8，或改用 `Set-Content -Encoding utf8`。")
    return ""


def load_legacy(root: Path) -> tuple[frozenset[str], str | None]:
    """读 `<root>/.arch-legacy`，返回 (遗留顶层包名集合, 错误信息)。

    格式：一行一个顶层包名，`#` 起注释。

    三种状态**必须区分**，否则会重演本 skill 反复踩的"假绿"：
      - 文件不存在 → 空集 + 无错。绿地项目，ARCH010/011 不适用（正常）。
      - 文件存在且有条目 → 正常启用。
      - **文件存在但解析不出条目 → 报错**。写了这个文件说明在做棕地，
        空文件一定是搞错了；静默放行会让 ARCH010 空转，
        而它给出的 "0 error" 与"检查通过"长得一模一样。
    """
    path = root / LEGACY_FILE
    if not path.is_file():
        return frozenset(), None
    try:
        # utf-8-sig 而非 utf-8：Windows 记事本 / PowerShell `Out-File -Encoding utf8`
        # 存 UTF-8 都会带 BOM，BOM 黏在首个包名前面会让它匹配不上任何 import，
        # **整份配置静默失效**（实测踩过）。
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        return frozenset(), f"无法读取 {path}：{exc}{_encoding_hint(path)}"
    except OSError as exc:
        return frozenset(), f"无法读取 {path}：{exc}"

    names: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.add(line.split(".")[0])
    if not names:
        return frozenset(), (
            f"{path} 存在但没有解析出任何包名——ARCH010/ARCH011 会处于**空转**状态，"
            f"它报的 0 error 不等于这两条规则通过了。"
            f"请填入遗留系统的顶层包名（一行一个），或删掉该文件。"
        )
    return frozenset(names), None


def check_project(
    root: Path,
    enabled: frozenset[str],
    legacy: frozenset[str] = frozenset(),
) -> tuple[list[Finding], list[str], int]:
    """返回 (findings, skipped, scanned)；skipped 记录读取失败或语法错误的文件。"""
    findings: list[Finding] = []
    skipped: list[str] = []
    scanned = 0
    for path in _iter_python_files(root):
        scanned += 1
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            skipped.append(f"{path}: 读取失败（{exc}）")
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, ValueError) as exc:
            line = getattr(exc, "lineno", 0) or 0
            msg = getattr(exc, "msg", str(exc))
            skipped.append(f"{path}:{line}: 无法解析，跳过（{msg}）")
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
        findings.extend(Checker(rel, source.splitlines(), enabled, legacy).run(tree))
    findings.sort(key=lambda f: (f.file, f.line, f.rule.id))
    return findings, skipped, scanned


# ============================================================
# 输出
# ============================================================
def _counts(findings: list[Finding]) -> tuple[int, int]:
    errors = sum(1 for f in findings if f.rule.severity == "error")
    return errors, len(findings) - errors


def _legacy_note(legacy: frozenset[str]) -> str:
    """把棕地模式写进输出。

    没有这行的话，"ARCH010 报 0 条"与"ARCH010 根本没启用"在输出上完全一样——
    又是一种假绿。启用了就必须让人看见。
    """
    if not legacy:
        return "棕地模式：未启用（无 .arch-legacy）——ARCH010/ARCH011 未参与本次校验"
    return (f"棕地模式：遗留顶层包 {', '.join(sorted(legacy))}"
            f"——ARCH010/ARCH011 已启用")


def render_text(
    findings: list[Finding],
    skipped: list[str],
    root: Path,
    legacy: frozenset[str] = frozenset(),
) -> None:
    for f in findings:
        tag = "ERROR  " if f.rule.severity == "error" else "WARNING"
        print(f"{f.file}:{f.line}:{f.col + 1}  {tag}  {f.rule.id} {f.rule.name}")
        print(f"    {f.detail}")
        print(f"    → {f.rule.hint}")

    if not findings and not skipped:
        print(f"✅ 架构校验通过：{root} 未发现违规")
        print(f"   {_legacy_note(legacy)}")
        return

    errors, warnings = _counts(findings)
    print()
    print(f"合计：{errors} error / {warnings} warning"
          + (f"，{len(skipped)} 个文件跳过" if skipped else ""))
    print(f"{_legacy_note(legacy)}")
    for s in skipped:
        print(f"  ⚠ {s}")


def render_json(
    findings: list[Finding],
    skipped: list[str],
    root: Path,
    legacy: frozenset[str] = frozenset(),
) -> None:
    errors, warnings = _counts(findings)
    print(json.dumps({
        "root": str(root),
        "summary": {
            "total": len(findings),
            "errors": errors,
            "warnings": warnings,
            "skipped": len(skipped),
        },
        "brownfield": {
            "enabled": bool(legacy),
            "legacy_packages": sorted(legacy),
        },
        "findings": [f.as_dict() for f in findings],
        "skipped_files": skipped,
    }, ensure_ascii=False, indent=2))


def render_rules() -> None:
    print("可用规则（--select / --ignore 用 ID）：\n")
    for r in RULES:
        print(f"  {r.id}  [{r.severity:7s}]  {r.name}")
        print(f"      {r.title}")
        print(f"      → {r.hint}\n")


# ============================================================
# 自检（--self-test）：内置 fixture 验证"规则仍能触发"且"合规代码不误报"
# ============================================================
# 存在意义：检查器最危险的失败模式是**静默失效**——某条规则因为重构而不再触发，
# 输出与"检查通过"完全一样。本 skill 开发中已踩过一次（重复定义 visit_ClassDef
# 导致 ARCH002 整条失效）。故规则改动后必须跑 --self-test，而非只看"没报错"。
_BAD_FIXTURE: dict[str, str] = {
    "agents/policy/agent.py": '''\
from __future__ import annotations

import time

from langchain_core.messages import SystemMessage

from backend.services import PolicyService


async def policy_node(state, mcp_client=None):
    if state["user_query"] == "查政策":
        msg = SystemMessage(content="你是一个政务政策助手，必须严格依据检索到的证据回答用户问题，不得编造任何未在证据中出现的事实。若证据不足，请明确告知用户并建议其咨询当地经办机构。回答需分点陈述，并在每条结论后标注来源编号。同时保持语气客观中立。")
    time.sleep(0.1)
    return {}
''',
    "agents/policy/schema.py": '''\
from __future__ import annotations

from operator import add
from typing import Annotated, TypedDict


class PolicyState(TypedDict):
    trace_id: str
    evidence: Annotated[list, add]
    citations: list
    a2a_tasks: list  # arch: ignore[ARCH002] A2A 整表替换语义，不加 append reducer
''',
    "orchestration/saver.py": '''\
from langgraph.checkpoint.base import BaseCheckpointSaver


class FileCheckpointSaver(BaseCheckpointSaver):
    def __init__(self, path: str) -> None:
        self.path = path
''',
    "backend/handlers.py": '''\
from __future__ import annotations

import time

import requests


async def fetch_and_log(url: str) -> None:
    try:
        requests.get(url)
    except Exception:
        pass
    print("done")
    time.sleep(1)


def sync_helper() -> None:
    time.sleep(1)


if __name__ == "__main__":
    print("smoke ok")
''',
    # --- 棕地：ARCH010（绕过 ACL 直连遗留）+ ARCH011（遗留反向依赖新层）---
    "agents/billing/agent.py": '''\
from __future__ import annotations

from legacy_erp.client import ErpClient


async def billing_node(state, mcp_client=None):
    client = ErpClient()
    return {"invoice": client.get_invoice(state["id"])}
''',
    "legacy_erp/service.py": '''\
from __future__ import annotations

from agents.billing.agent import billing_node


def run(state) -> None:
    billing_node(state)
''',
}

# 合规样例：对齐 patterns.md §1/§3/§5.1 的标准写法——任何 finding 都是**误报**
_GOOD_FIXTURE: dict[str, str] = {
    "agents/policy/agent.py": '''\
from __future__ import annotations

from loguru import logger

from orchestration.langgraph.state import AgentState
from tools.mcp.client import MCPClient


async def policy_node(state: AgentState, mcp_client: MCPClient | None = None) -> dict:
    from governance.trace import AgentTracer, SpanKind

    def _sync_retry_delay() -> None:
        # 同步工具函数：这里的 time.sleep **不在** async 上下文里（ARCH007 反例）。
        # 曾经 _async_depth 不因 FunctionDef 归零，这种很常见的写法会被误报。
        import time

        time.sleep(0.01)

    if state["query"] == "policy":
        # state["query"] 是**状态字段**，不是用户原始输入（ARCH006 反例）。
        # 曾经关键字里有裸 "query"，这条正常的状态路由会被误报成硬编码路由。
        return {"mode": "direct"}

    _sync_retry_delay()
    with AgentTracer.span(SpanKind.AGENT, "policy"):
        if mcp_client is None:
            logger.warning("mcp_client 不可用，降级为 stub 模式")
            return {"evidence": [], "mode": "stub"}
        raw = await mcp_client.call_tool("search_policy", {"query": state["user_query"]})
    return raw
''',
    "orchestration/graph.py": '''\
from __future__ import annotations

import time
from operator import add
from typing import Annotated, TypedDict

from governance.metrics import record_agent_call
from governance.trace import AgentTracer, SpanKind
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class AgentState(TypedDict):
    trace_id: str
    intent: str
    task_plan: Annotated[list, add]
    evidence: Annotated[list, add]


async def policy_node(state: AgentState, mcp_client=None) -> dict:
    t0 = time.perf_counter()
    success = False
    try:
        with AgentTracer.span(SpanKind.AGENT, "policy"):
            if state["intent"] == "policy":
                result = await mcp_client.call_tool("search_policy", {"query": state["q"]})
            else:
                result = {"evidence": []}
        success = True
        return {"evidence": result["evidence"]}
    except Exception as e:
        return {"error": f"policy failed: {e}"}
    finally:
        record_agent_call("policy", success, (time.perf_counter() - t0) * 1000, state["trace_id"])


async def make_graph(dsn: str):
    pool = AsyncConnectionPool(conninfo=dsn, min_size=2, max_size=10,
                               kwargs={"autocommit": True, "row_factory": dict_row})
    await pool.open()
    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    return saver
''',
    # 反腐蚀层：**唯一**允许 import 遗留系统的地方（brownfield.md §2）
    "tools/mcp/servers/erp/acl/adapter.py": '''\
from __future__ import annotations

from loguru import logger

from legacy_erp.client import ErpClient


class ErpAcl:
    """反腐蚀层：把遗留 ERP 的模型与错误翻译成新架构的契约。"""

    def __init__(self, client: ErpClient) -> None:
        self._client = client

    async def fetch_invoice(self, invoice_id: str) -> dict:
        try:
            raw = await self._client.get_invoice(invoice_id)
        except Exception as exc:
            logger.warning("legacy_erp 调用失败，降级为空结果: {}", exc)
            return {"invoice": None, "mode": "degraded"}
        # 遗留系统用「分」且键名全大写，翻译成新架构的契约
        return {"invoice": {"id": raw["ID"], "amount": raw["AMT"] / 100}}
''',
}


# 棕地 fixture 用到的遗留包名。**故意不写成 .arch-legacy 文件**——
# 走 --legacy 参数这条路径，顺便验证它就是 .arch-legacy 的等价入口。
_LEGACY_PKGS = frozenset({"legacy_erp"})


def check_skill_sync(skill_md: Path) -> list[str]:
    """核对 `RULES` 与 `SKILL.md §10` 的 `⚙ ARCHxxx` 标注一一对应。

    `SKILL_MAINTENANCE.md §7` 把这条写成了硬要求（"增删规则时两边同改"），
    但在本次加规则之前**没有任何东西强制它**——又一处"靠自觉"。
    规则加了没标 §10，评审的人就永远看不到它；标了 §10 却没有规则，评审的人会去跑一个
    不存在的 ID。两种都是静默失效。

    ⚠️ 一行里可以挂多个 ID（`⚙ ARCH001 / ARCH005`），**必须收该行全部 ID**。
    只取 `⚙` 后第一个会误报 ARCH005/ARCH008"漏标"——已踩过一次。
    """
    text = skill_md.read_text(encoding="utf-8")
    # ⚠️ 只扫 §10 段落。曾经扫的是整个 SKILL.md —— 那是"存在性检查"冒充"§10 检查"：
    # 任何一个 ⚙ 只要出现在文件别处，§10 里的对应条目被删掉也照样通过。
    m = re.search(r"^## 10\..*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not m:
        return ["SKILL.md 里找不到 `## 10.` 段落——对应性检查无法进行（不是通过）"]
    marked: set[str] = set()
    for line in m.group(0).splitlines():
        if "⚙" in line:
            marked |= set(re.findall(r"ARCH\d{3}", line))
    known = {r.id for r in RULES}

    problems = []
    for rid in sorted(known - marked):
        problems.append(f"{rid} 在 RULES 里但 SKILL.md §10 没标 ⚙ —— 评审查不到这条规则")
    for rid in sorted(marked - known):
        problems.append(f"SKILL.md §10 标了 {rid}，但 RULES 里没有 —— 会让人去跑一个不存在的 ID")
    return problems


def run_self_test() -> int:
    """断言全部规则可触发（无静默失效）+ 合规样例零误报 + 棕地规则确实按项目启用。"""
    import tempfile

    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="archcheck-selftest-") as tmp:
        roots = {}
        for label, group in (("bad", _BAD_FIXTURE), ("good", _GOOD_FIXTURE)):
            root = Path(tmp) / label
            for rel, content in group.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            roots[label] = root

        enabled = frozenset(RULES_BY_ID)
        bad_findings, _, _ = check_project(roots["bad"], enabled, _LEGACY_PKGS)
        good_findings, _, _ = check_project(roots["good"], enabled, _LEGACY_PKGS)
        # 元验证：**不开棕地模式**跑同一份 bad fixture。fixture 是写死的常量，
        # 只有真的把 ARCH010/011 关掉才会不报——报出来了就说明这两条不是按项目启用的，
        # 那样它们会在每个绿地项目里误报。
        green_findings, _, _ = check_project(roots["bad"], enabled)

    triggered = {f.rule.id for f in bad_findings}
    for rule in RULES:
        if rule.id not in triggered:
            failures.append(f"规则静默失效（未触发）：{rule.id} {rule.name}")

    for f in good_findings:
        failures.append(f"合规样例误报：{f.rule.id} {f.file}:{f.line} — {f.detail}")

    leaked = {f.rule.id for f in green_findings} & {"ARCH010", "ARCH011"}
    if leaked:
        failures.append(
            f"棕地规则在未启用时仍然触发：{', '.join(sorted(leaked))}"
            f"——绿地项目会误报，规则必须是 opt-in"
        )

    # 规则表 ↔ SKILL.md §10 对应性。只在 skill 仓库里有意义：
    # scaffold 把本脚本复制进下游项目，那里没有 SKILL.md。
    skill_md = Path(__file__).resolve().parent.parent / "SKILL.md"
    synced = "（未找到 SKILL.md，跳过 §10 对应性检查）"
    if skill_md.is_file():
        sync_problems = check_skill_sync(skill_md)
        failures.extend(sync_problems)
        synced = ("§10 对应性已核对" if not sync_problems
                  else f"§10 对应性有 {len(sync_problems)} 处不符")

    if failures:
        print("❌ 自检失败：")
        for item in failures:
            print(f"  - {item}")
        return 1

    print(f"✅ 自检通过：{len(RULES)} 条规则全部可触发，"
          f"合规样例零误报，棕地规则确认 opt-in，{synced}")
    return 0


# ============================================================
# 入口
# ============================================================
def _resolve_rules(select: str | None, ignore: str | None) -> frozenset[str] | str:
    """返回启用的规则 ID 集合；参数非法时返回错误信息字符串。"""
    enabled = set(RULES_BY_ID)
    if select:
        chosen = {s.strip().upper() for s in select.split(",") if s.strip()}
        unknown = chosen - set(RULES_BY_ID)
        if unknown:
            return f"未知规则 ID：{', '.join(sorted(unknown))}"
        enabled = chosen
    if ignore:
        dropped = {s.strip().upper() for s in ignore.split(",") if s.strip()}
        unknown = dropped - set(RULES_BY_ID)
        if unknown:
            return f"未知规则 ID：{', '.join(sorted(unknown))}"
        enabled -= dropped
    # 0 条规则 = 什么都没查，而输出与"检查通过"一模一样（`--select ","`、
    # 或脚本拼接参数时漏了变量都会走到这里）。本文件的 main() 为"0 个文件"和
    # "空 .arch-legacy"都写了防线，唯独漏了"0 条规则"——补上。
    if not enabled:
        return ("没有启用任何规则（`--select` / `--ignore` 的组合把规则筛空了）——"
                "本次校验无意义，不是通过")
    return frozenset(enabled)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="架构一致性校验：把 SKILL.md §10 的评审 checklist 变成可失败的断言",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/check_architecture.py ./my_agent_app\n"
            "  python scripts/check_architecture.py . --select ARCH001,ARCH002\n"
            "  python scripts/check_architecture.py . --format json --fail-on warning\n"
        ),
    )
    parser.add_argument("path", nargs="?", default=".", help="项目根目录（默认当前目录）")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="输出格式")
    parser.add_argument("--select", default=None, help="只启用这些规则 ID（逗号分隔）")
    parser.add_argument("--ignore", default=None, help="禁用这些规则 ID（逗号分隔）")
    parser.add_argument("--fail-on", choices=("error", "warning", "never"), default="error",
                        help="达到该严重度即返回退出码 1（默认 error）")
    parser.add_argument("--list-rules", action="store_true", help="列出所有规则后退出")
    parser.add_argument("--self-test", action="store_true",
                        help="用内置 fixture 自检规则是否仍有效（改规则后必跑）")
    parser.add_argument("--legacy", default=None,
                        help=f"遗留系统顶层包名（逗号分隔），与 {LEGACY_FILE} 取并集；"
                             f"棕地项目用，绿地项目不用管")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    if args.list_rules:
        render_rules()
        return 0

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"错误：路径不是目录：{root}", file=sys.stderr)
        return 2

    enabled = _resolve_rules(args.select, args.ignore)
    if isinstance(enabled, str):
        print(f"错误：{enabled}", file=sys.stderr)
        return 2

    legacy, legacy_err = load_legacy(root)
    if args.legacy:
        legacy |= {s.strip().split(".")[0] for s in args.legacy.split(",") if s.strip()}
    if legacy_err and not legacy:
        # 不静默降级：空 .arch-legacy 会让 ARCH010/011 空转，而输出与"通过"一样
        print(f"错误：{legacy_err}", file=sys.stderr)
        return 2

    findings, skipped, scanned = check_project(root, enabled, legacy)

    # 防线：检查器"没检查成"与"检查通过"必须是两种输出，否则静默失效会被读成绿灯
    if scanned == 0:
        print(f"错误：{root} 下未找到任何 .py 文件，检查未实际执行", file=sys.stderr)
        return 2
    if len(skipped) == scanned:
        print(f"错误：{scanned} 个 .py 文件全部无法解析，检查未实际生效"
              f"（首个原因：{skipped[0]}）", file=sys.stderr)
        return 1

    if args.format == "json":
        render_json(findings, skipped, root, legacy)
    else:
        render_text(findings, skipped, root, legacy)

    if args.fail_on == "never" or not findings:
        return 0
    if args.fail_on == "warning":
        return 1
    return 1 if any(f.rule.severity == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
