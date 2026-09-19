"""事实核查：把 skill 里的易漂移断言变成可执行检查 + 过期告警。

## 为什么需要它

skill 里写了大量关于**外部库/协议**的事实断言（版本号、导入路径、API 存在与否、
参数契约）。这些断言会以两种方式失效：

1. **虚构**——写的时候就没核实过。实例：本 skill 曾断言"0.x 用 `operator.add`、
   1.x 改用 `add`"，而 `langgraph.graph` 从未导出过 `add`（核实于 1.1.6）。
   错误扩散到 4 个文件。
2. **漂移**——写的时候是对的，上游改了。

⚠️ **光有 `last_verified` 日期拦不住第 1 种**：一个凭印象写下的日期反而制造
"已核实"的假象。真正能拦住虚构的，是**把断言写成可执行的形式**——写不出检查
代码，往往说明当初就没真验过。

所以本工具分两级，并且**明确标注哪级弱**：

| 级别 | 手段 | 能拦虚构？ | 能拦漂移？ |
|---|---|---|---|
| A 可执行 | 对已安装的包真跑一段断言代码 | ✅ | ✅ |
| B 人工   | 只有 last_verified 日期 | ❌ | ⚠️ 只能提醒"该看了" |

## 用法

    python scripts/check_facts.py                 # 核查 + 过期告警
    python scripts/check_facts.py --strict        # CI 用：SKIP 也算失败
    python scripts/check_facts.py --verbose       # 逐条列出（含通过项）
    python scripts/check_facts.py --max-age 90    # 改过期阈值（天，默认 180）
    python scripts/check_facts.py --stamp         # 把今天写回"已通过"的条目
    python scripts/check_facts.py --list          # 只看清单，不执行
    python scripts/check_facts.py --self-test     # 自检：能否识别出假断言

Exit code：0 全部通过 / 1 有断言失败、条目过期或（`--strict` 下）有跳过 /
2 清单缺失、解析为空**或有解析问题**——都**不是通过**。

`--stamp` 只对**本轮真跑通过**的 A 级条目写回日期——不是无脑刷新，日期是挣来的。
B 级条目必须显式点名：`--stamp F-010`（你刚手工核对过那一条）。
"""
from __future__ import annotations

import argparse
import ast
import importlib.metadata
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_FACTS = SCRIPT_DIR.parent / "FACTS.md"
PY = sys.executable
DEFAULT_MAX_AGE = 180

# Windows GBK 控制台下输出 ✓ ⚠ ✅ 这类符号会 UnicodeEncodeError（中文本身 GBK 编得出，
# 崩的是符号）。**scripts/ 下每个可执行脚本都要有这一段**——CI 跑在 Linux 上永远发现不了。
# stdout 与 stderr 都要处理——错误信息走 stderr，只改 stdout 会让报错反而乱码。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass

# 条目头：## F-001 · 标题
_ENTRY_RE = re.compile(r"^##\s+(F-\d{3})\s*[·:.：]\s*(.+?)\s*$", re.M)
_CODE_RE = re.compile(r"```python\n(.*?)```", re.S)
_MANUAL_RE = re.compile(r"-\s*\*\*核实方式\*\*\s*[：:]\s*人工")
_FIELD_TMPL = r"-\s*\*\*{key}\*\*\s*[：:]\s*(.+?)\s*$"


def _field(body: str, key: str) -> str:
    m = re.search(_FIELD_TMPL.format(key=re.escape(key)), body, re.M)
    return m.group(1).strip() if m else ""


def _has_assertion(code: str) -> bool:
    """代码块里有没有"会失败"的东西。

    ⚠️ 只看子进程 returncode 是不够的：一段只 `print` 的代码跑完必然 exit 0，
    于是一条从未被检验的"断言"也会显示为"通过"。这正是本工具存在的理由
    （拦"凭印象写下的断言"），工具自己不能制造这一类。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # 语法错误会让它在执行路径上 FAIL，不必在这里重复报一遍。
        return True
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assert, ast.Raise)):
            return True
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "exit"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sys"):
            return True
    return False


@dataclass
class Fact:
    fid: str
    title: str
    claim: str = ""
    used_in: str = ""
    requires: list[str] = field(default_factory=list)
    code: str = ""
    verified_version: str = ""
    last_verified: date | None = None
    risk: str = ""

    @property
    def executable(self) -> bool:
        return bool(self.code)

    def age_days(self, today: date) -> int | None:
        return (today - self.last_verified).days if self.last_verified else None


@dataclass
class Result:
    fact: Fact
    status: str          # OK / FAIL / SKIP / STALE
    detail: str = ""


def parse_facts(text: str) -> tuple[list[Fact], list[str]]:
    """解析 FACTS.md。返回 (条目, 解析告警)。"""
    problems: list[str] = []
    matches = list(_ENTRY_RE.finditer(text))
    if not matches:
        return [], ["未匹配到任何 `## F-NNN ·` 条目头"]

    facts: list[Fact] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end]
        fid, title = m.group(1), m.group(2)

        raw_date = _field(body, "last_verified")
        last_verified: date | None = None
        if raw_date:
            try:
                last_verified = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                problems.append(f"{fid}: last_verified 不是 YYYY-MM-DD —— {raw_date!r}")
        else:
            problems.append(f"{fid}: 缺少 last_verified")

        code_match = _CODE_RE.search(body)
        is_manual = bool(_MANUAL_RE.search(body))
        # 代码块嵌在列表项下会带缩进，直接执行会 IndentationError——
        # 那样断言会"失败"，但失败原因是解析而不是断言本身（假信号）。
        code = textwrap.dedent(code_match.group(1)).strip() if code_match else ""
        if code and is_manual:
            problems.append(f"{fid}: 同时标了「可执行」和「人工」——二选一")
            code = ""
        elif code and not _has_assertion(code):
            problems.append(
                f"{fid}: 代码块里没有 assert / raise / sys.exit —— "
                "跑完必然通过，等于没检查"
            )

        requires_raw = _field(body, "依赖")
        requires = (
            [p.strip() for p in re.split(r"[,，/]", requires_raw) if p.strip()]
            if requires_raw and requires_raw not in {"-", "无", "—"}
            else []
        )

        facts.append(Fact(
            fid=fid,
            title=title,
            claim=_field(body, "断言"),
            used_in=_field(body, "被用于"),
            requires=requires,
            code=code,
            verified_version=_field(body, "核实版本"),
            last_verified=last_verified,
            risk=_field(body, "漂移风险"),
        ))
    return facts, problems


def _is_installed(pkg: str) -> bool:
    """装了没有？导入名与发行名都可能命中，两个都查。

    只看 `find_spec(pkg.replace("-", "_"))` 会漏判：导入名 ≠ 发行名的包一大把
    （python-dotenv→dotenv、pyyaml→yaml、pillow→PIL、beautifulsoup4→bs4）。
    漏判的后果是"已安装却报 SKIP"——覆盖被静默削掉，比假绿更难发现。
    """
    try:
        if importlib.util.find_spec(pkg.replace("-", "_")) is not None:
            return True
    except (ImportError, ValueError):
        pass
    try:
        importlib.metadata.distribution(pkg)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def _missing_packages(fact: Fact) -> list[str]:
    return [pkg for pkg in fact.requires if not _is_installed(pkg)]


def run_fact(
    fact: Fact, today: date, max_age: int, timeout: int = 60, root: Path | None = None
) -> Result:
    """跑单个条目。A 级真执行；B 级只比日期。

    `root` 是断言的**工作目录**，默认 skill 根目录——条目里用相对路径（如 `scripts/`）
    是常态。⚠️ 曾经把工作目录设成脚本所在的临时目录，结果 `Path("scripts").glob("*.py")`
    扫到 0 个文件却报"通过"（F-008 空转过一段时间）。工作目录错了，断言可能**恒真**。
    """
    stale = (age := fact.age_days(today)) is not None and age > max_age

    if not fact.executable:
        if fact.last_verified is None:
            return Result(fact, "STALE", "缺少 last_verified 日期，无法判断是否过期")
        if stale:
            return Result(fact, "STALE", f"人工核实已 {age} 天未更新（阈值 {max_age}）")
        return Result(fact, "OK", f"人工条目，{age} 天前核实（阈值 {max_age}）")

    missing = _missing_packages(fact)
    if missing:
        # 关键：未安装 ≠ 通过。跳过要说清楚为什么，否则就是"静默通过"。
        return Result(fact, "SKIP", f"未安装 {'、'.join(missing)}，本机无法验证")

    workdir = Path(root) if root is not None else SCRIPT_DIR.parent
    with tempfile.TemporaryDirectory(prefix="facts-") as tmp:
        script = Path(tmp) / "assertion.py"
        script.write_text(fact.code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [PY, str(script)], capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace", cwd=workdir,
                # 子进程的 stdout 是管道时会按 locale 编码写（Windows 上是 GBK），
                # 父进程按 utf-8 解 + errors="replace" → 不抛异常、静静产出乱码，
                # 而 FAIL 时取的正是那一行报错详情。CI 在 Linux 上永远发现不了。
                # 用 PYTHONIOENCODING 而不是 -X utf8：后者会连文件读取的默认编码
                # 一起改，可能改变断言本身的行为（断言必须只被事实影响）。
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        except subprocess.TimeoutExpired:
            return Result(fact, "FAIL", f"断言超时（{timeout}s）")
        except OSError as exc:
            return Result(fact, "FAIL", f"无法执行：{exc}")

    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip().splitlines()
        return Result(fact, "FAIL", tail[-1] if tail else f"exit {proc.returncode}")

    note = f"断言通过（{fact.verified_version or '未标注版本'}）"
    if stale:
        return Result(fact, "STALE", f"{note}，但已 {age} 天未复核（阈值 {max_age}）")
    return Result(fact, "OK", note)


# ------------------------------------------------------------------ stamp

def stamp(path: Path, facts: list[Fact], passed_ids: set[str], today: date) -> int:
    """把 today 写回指定条目的 last_verified。只动那一行。"""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    current: str | None = None
    changed = 0
    out: list[str] = []
    for line in lines:
        m = _ENTRY_RE.match(line.rstrip("\r\n"))
        if m:
            current = m.group(1)
        if current in passed_ids and re.match(r"-\s*\*\*last_verified\*\*", line):
            new = re.sub(r"\d{4}-\d{2}-\d{2}", today.isoformat(), line)
            if new != line:
                changed += 1
            out.append(new)
            continue
        out.append(line)
    if changed:
        path.write_text("".join(out), encoding="utf-8")
    return changed


# --------------------------------------------------------------- self-test

_SELF_TEST_REGISTRY = """# 自检用清单

## F-001 · 应当通过的假条目

- **断言**：1 + 1 == 2
- **核实方式**：可执行
  ```python
  assert 1 + 1 == 2
  ```
- **last_verified**：__TODAY__

## F-002 · 应当失败的假条目（虚构断言）

- **断言**：`langgraph.graph` 导出 `add`（**已知为假**，见 F-001）
- **核实方式**：可执行
  ```python
  # ⚠️ 故意不声明任何依赖。**自检必须在干净环境里也能跑**：曾经这条写的是
  # `import langgraph.graph` 并把 langgraph 声明为依赖，于是在未装 langgraph 的
  # 机器上它降级为 SKIP 而非 FAIL，自检报「F-002 期望 FAIL 实际 SKIP」——
  # 一个只在 CI 装了依赖时才成立的假绿，反过来说就是干净环境里的假红。
  #
  # 注：上面那句**不能**写成字面的依赖字段（连注释里也不行）——解析按行正则匹配，
  # 注释里出现该字段会真的被当成本条目的依赖声明。这是踩过的坑。
  assert False, "langgraph.graph 没有 add（F-001 已核实：从未导出过该符号）"
  ```
- **last_verified**：__TODAY__

## F-005 · 应当报「无断言」的假条目

- **断言**：一段跑得通但什么都没检查的代码
- **核实方式**：可执行
  ```python
  print("nothing checked here")
  ```
- **last_verified**：__TODAY__

## F-003 · 应当跳过的假条目（依赖不存在）

- **断言**：某未安装的包有某属性
- **核实方式**：可执行
  ```python
  import definitely_not_a_real_package_xyz as m
  assert m.thing
  ```
- **依赖**：definitely_not_a_real_package_xyz
- **last_verified**：__TODAY__

## F-004 · 应当过期的人工条目

- **断言**：某历史事实
- **核实方式**：人工 —— 查官方 release notes
- **last_verified**：2000-01-01
"""


def self_test(max_age: int) -> int:
    """自检：核查器必须能区分 通过 / 失败 / 跳过 / 过期，并拦住"无断言的代码块"。

    最危险的失败模式是"把失败报成通过"，所以这里**主动塞入已知坏的样例**：
    一条必假的断言（必须判 FAIL）、一条无断言的代码块（必须被解析报出）。
    """
    today = date.today()
    text = _SELF_TEST_REGISTRY.replace("__TODAY__", today.isoformat())
    with tempfile.TemporaryDirectory(prefix="facts-selftest-") as tmp:
        path = Path(tmp) / "FACTS.md"
        path.write_text(text, encoding="utf-8")
        facts, problems = parse_facts(path.read_text(encoding="utf-8"))
        results = {r.fact.fid: r for r in (run_fact(f, today, max_age) for f in facts)}

    ok = True
    expected = {"F-001": "OK", "F-002": "FAIL", "F-003": "SKIP", "F-004": "STALE"}
    for fid, want in expected.items():
        got = results[fid].status if fid in results else "<缺失>"
        mark = "✓" if got == want else "✗"
        if got != want:
            ok = False
        print(f"  {mark} {fid} 期望 {want:5} 实际 {got:5} —— {results[fid].detail if fid in results else ''}")

    # F-005 是"跑得通但什么都没检查"的代码块：必须**在解析阶段**被报出来。
    # 它自己执行会 exit 0（这正是危险之处），所以只能靠这一条断言拦住。
    got_problems = {p.split(":", 1)[0] for p in problems}
    mark = "✓" if got_problems == {"F-005"} else "✗"
    if got_problems != {"F-005"}:
        ok = False
    print(f"  {mark} 解析告警 期望 {{'F-005'}} 实际 {got_problems or '{}'}"
          f" —— {problems if problems else '无'}")

    if not ok:
        print("\n❌ 自检失败：核查器无法正确区分四种结果，或漏放了无断言的代码块")
        return 1
    print("\n✅ 自检通过：能区分 通过 / 失败 / 跳过 / 过期，且会拒绝无断言的代码块")
    return 0


# -------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="skill 事实核查与防漂移")
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS, help="清单文件路径")
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE, help="过期阈值（天）")
    parser.add_argument("--verbose", "-v", action="store_true", help="逐条列出（含通过项）")
    parser.add_argument("--list", action="store_true", help="只列清单，不执行")
    parser.add_argument("--stamp", nargs="*", metavar="F-NNN",
                        help="写回核实日期：无参数则写回本轮通过的 A 级条目；"
                             "带 F-NNN 则仅写回点名的条目（用于人工核实过的 B 级）")
    parser.add_argument("--self-test", action="store_true", help="自检核查器本身")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--strict", action="store_true",
                        help="SKIP 也算失败。**CI 里必须加**——依赖少一个包就会让整条断言"
                             "静默降级为 SKIP，而 SKIP 不影响默认退出码，输出与「通过」无异")
    args = parser.parse_args()

    if args.self_test:
        return self_test(args.max_age)

    if not args.facts.exists():
        print(f"错误：找不到事实清单 {args.facts}", file=sys.stderr)
        return 2

    facts, problems = parse_facts(args.facts.read_text(encoding="utf-8"))
    if not facts:
        print(f"错误：{args.facts} 未解析出任何条目——核查未实际执行", file=sys.stderr)
        for p in problems:
            print(f"  · {p}", file=sys.stderr)
        return 2

    if problems:
        # 清单自身的伤必须非零退出。**曾经这里是静默的**：problems 只在 facts 为空
        # 那一支被用到，facts 非空时被整个丢弃——于是 last_verified 写成 2026/09/19
        # 的条目 age_days() 返回 None、永不 STALE，而输出与"全部通过"一模一样。
        print(f"错误：{args.facts} 有 {len(problems)} 处问题——核查结果不可信", file=sys.stderr)
        for p in problems:
            print(f"  · {p}", file=sys.stderr)
        return 2

    today = date.today()
    if args.list:
        for f in facts:
            tier = "A 可执行" if f.executable else "B 人工  "
            print(f"{f.fid}  [{tier}]  {f.title}")
        print(f"\n共 {len(facts)} 条（A 级 {sum(1 for f in facts if f.executable)}）")
        return 0

    results = [run_fact(f, today, args.max_age) for f in facts]

    if args.format == "json":
        import json
        print(json.dumps({
            "today": today.isoformat(),
            "max_age": args.max_age,
            "results": [
                {"id": r.fact.fid, "title": r.fact.title, "tier": "A" if r.fact.executable else "B",
                 "status": r.status, "detail": r.detail, "last_verified":
                     r.fact.last_verified.isoformat() if r.fact.last_verified else None}
                for r in results
            ],
        }, ensure_ascii=False, indent=2))
    else:
        icons = {"OK": "  ok  ", "FAIL": " FAIL ", "SKIP": " skip ", "STALE": "stale "}
        for r in results:
            if r.status == "OK" and not args.verbose:
                continue
            print(f"[{icons[r.status]}] {r.fact.fid} {r.fact.title}\n         {r.detail}")
        counts = {k: sum(1 for r in results if r.status == k) for k in ("OK", "FAIL", "SKIP", "STALE")}
        a_total = sum(1 for f in facts if f.executable)
        if not args.verbose:
            print(f"（{counts['OK']} 条通过未逐条列出，加 --verbose 查看）")
        print(f"\n共 {len(facts)} 条（A 级可执行 {a_total} / B 级人工 {len(facts) - a_total}）"
              f" → 通过 {counts['OK']} · 失败 {counts['FAIL']} · 跳过 {counts['SKIP']} · 过期 {counts['STALE']}")

    if args.stamp is not None:
        if args.stamp:
            targets = set(args.stamp)
            unknown = targets - {f.fid for f in facts}
            if unknown:
                print(f"错误：清单里没有 {sorted(unknown)}", file=sys.stderr)
                return 2
        else:
            targets = {r.fact.fid for r in results if r.fact.executable and r.status in {"OK", "STALE"}}
        changed = stamp(args.facts, facts, targets, today)
        print(f"\n已写回 {changed} 条的 last_verified = {today.isoformat()}"
              f"（{len(targets)} 条指定）")

    failed = [r for r in results if r.status == "FAIL"]
    stale = [r for r in results if r.status == "STALE"]
    if failed:
        print("\n先处理失败的断言——它们现在是**错的**，或上游已漂移：")
        for r in failed:
            print(f"  · {r.fact.fid} {r.fact.title}：{r.detail}")
    if stale:
        print(f"\n{len(stale)} 条已超过 {args.max_age} 天未复核（不是错，是没人看过）：")
        for r in stale:
            print(f"  · {r.fact.fid} {r.fact.title}")
        print("  → A 级：跑 --stamp 即可（日期由通过的断言挣得）；"
              "B 级：人工核对后 --stamp F-NNN")

    skipped = [r for r in results if r.status == "SKIP"]
    if args.strict and skipped:
        # SKIP 的字面意思是"本机没验"，不是"验过了"。CI 里依赖是齐的，出现 SKIP
        # 只可能是清单写错了依赖名——那时这条断言其实**从未被跑过**。
        print(f"\n--strict：{len(skipped)} 条被跳过——跳过不是通过：")
        for r in skipped:
            print(f"  · {r.fact.fid} {r.fact.title}：{r.detail}")

    if failed or stale or (args.strict and skipped):
        return 1
    print("\n✅ 事实核查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
