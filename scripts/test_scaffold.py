"""Scaffold 产物验收回归：生成 → 冒烟 → 测试 → 架构校验。

这是 scaffold_project.py 的验收标准。它存在的理由：
scaffold 曾经生成过一套**开箱即坏**的代码（async 节点配同步 .invoke()，
smoke test 与 pytest 双双 exit 1），而当时没有任何测试覆盖生成产物——
`test_demo.py` 只测 demo_end_to_end.py。产物坏了没人知道。

分级执行（依赖缺失自动降级，绝不假装通过）：
  L1 静态（永远执行）：文件齐全 + 每个 .py 能 ast.parse + 产物 `ruff check .` +
                      check_architecture 自检与校验（**跑产物里的副本**，
                      下游 CI 用的就是副本，只验 skill 原件等于没验真正会跑的那份）
  L2 运行（需 langgraph 等依赖）：python -m orchestration.langgraph.graph + pytest
  L3 服务（需 fastapi）：import backend.main

用法：
    python scripts/test_scaffold.py            # 有依赖跑全部，没依赖只跑 L1
    python scripts/test_scaffold.py --strict   # 任一级被跳过即视为失败（CI 用）
"""
from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCAFFOLD = SCRIPT_DIR / "scaffold_project.py"
CHECKER = SCRIPT_DIR / "check_architecture.py"
PY = sys.executable

PASSED: list[str] = []
FAILED: list[str] = []
SKIPPED: list[str] = []


def _report(ok: bool, label: str, detail: str = "") -> bool:
    (PASSED if ok else FAILED).append(label)
    mark = "  ok  " if ok else " FAIL "
    print(f"[{mark}] {label}" + (f"\n         {detail}" if detail and not ok else ""))
    return ok


def _skip(label: str, why: str) -> None:
    SKIPPED.append(label)
    print(f"[ skip ] {label} —— {why}")


def _run(cmd: list[str], cwd: Path, timeout: int = 180) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace"
        )
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except OSError as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _has_module(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# ---------------------------------------------------------------- L1 静态

REQUIRED_FILES = [
    "backend/__init__.py",
    "backend/config.py",
    "backend/main.py",
    "orchestration/langgraph/state.py",
    "orchestration/langgraph/graph.py",
    "governance/metrics.py",
    "governance/trace.py",
    "tests/test_graph.py",
    "pytest.ini",
    "CLAUDE.md",
    ".env.example",
    ".github/workflows/ci.yml",
    "scripts/check_architecture.py",
    "requirements/requirements.txt",
    "requirements/requirements-dev.txt",
]


def check_lint(root: Path) -> None:
    """产物必须能过它自己 CI 里的 `ruff check .`。

    产物 CI 模板里有这一步，但此前**没有任何验收跑它**——于是"生成的目录带着
    未使用的导入、开箱即被自己的 CI 判红"能一路绿灯跑到用户手里。
    （反过来也成立：往 scaffold 复制进产物的脚本里写一句不合规的代码，
    比如 `re.M | re.S`，同样没人拦——ruff 会跟着副本一起进产物。）
    ruff 不是运行期依赖，未安装时按既有分级跳过（`--strict` 下算失败）。
    """
    if not _has_module("ruff") and shutil.which("ruff") is None:
        _skip("L1 产物 ruff check", "未安装 ruff（pip install ruff）")
        return
    code, out = _run(["ruff", "check", "."], root)
    _report(code == 0, "L1 产物 ruff check .（与产物 CI 同步）", out.strip()[-600:])


def check_static(root: Path) -> None:
    missing = [f for f in REQUIRED_FILES if not (root / f).exists()]
    _report(not missing, "L1 生成文件齐全", f"缺失: {missing}")

    # 每个 .py 必须能解析（语法错误在这里就该拦下，而不是等到 pytest）
    bad: list[str] = []
    for py in sorted(root.rglob("*.py")):
        try:
            ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError as exc:
            bad.append(f"{py.relative_to(root)}: {exc}")
    _report(not bad, "L1 全部 .py 语法正确", "; ".join(bad[:3]))

    check_lint(root)

    # 先自检检查器本身：规则静默失效时，下面的"0 error"会是假绿
    # （检查器最危险的失败模式——输出与"检查通过"完全一样）
    if not CHECKER.exists():
        _skip("L1 架构校验", "未找到 check_architecture.py")
        return

    code, out = _run([PY, str(CHECKER), "--self-test"], SCRIPT_DIR)
    _report(code == 0, "L1 检查器自检（skill 原件）", out.strip()[-400:])

    # **副本也要自检**：下游 CI 执行的是产物里的 scripts/check_architecture.py。
    # 只验原件时，副本若因模板转义/占位符替换而与原件不一致，谁也不知道。
    copied = root / "scripts" / "check_architecture.py"
    if copied.exists():
        code, out = _run([PY, str(copied), "--self-test"], root)
        found = re.search(r"(\d+)\s*条规则", out)
        # 规则条数从检查器的输出里取，不写死——写死的数字在加规则后必然过期
        # （曾经写死过"9 条规则"，加到 11 条后这句就成了错的，且没人会注意到）。
        label = (f"L1 副本检查器自检（{found.group(1)} 条规则均可触发）"
                 if found else "L1 副本检查器自检")
        _report(code == 0, label, out.strip()[-400:])
    else:
        _skip("L1 副本检查器自检", "产物里没有 scripts/check_architecture.py")

    # 架构校验必须 0 error（warning 不阻塞 scaffold 验收：下游项目自有 warning）
    code, out = _run([PY, str(CHECKER), str(root), "--fail-on", "error"], SCRIPT_DIR)
    _report(code == 0, "L1 架构校验 0 error", out.strip()[-500:])


# ---------------------------------------------------------------- L2 运行

def check_runtime(root: Path) -> None:
    heavy = [m for m in ("langgraph", "loguru") if not _has_module(m)]
    if heavy:
        _skip("L2 graph 冒烟测试", f"缺少依赖 {heavy}，pip install -r requirements/requirements.txt")
        _skip("L2 pytest", f"缺少依赖 {heavy}")
        return

    code, out = _run([PY, "-m", "orchestration.langgraph.graph"], root)
    _report(
        code == 0 and "smoke test ok" in out,
        "L2 graph 冒烟测试（python -m orchestration.langgraph.graph）",
        out.strip()[-600:],
    )

    if not _has_module("pytest"):
        _skip("L2 pytest", "未安装 pytest")
        return
    if not _has_module("pytest_asyncio"):
        _skip("L2 pytest", "未安装 pytest-asyncio（async 测试无法运行）")
        return
    code, out = _run([PY, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"], root)
    _report(code == 0, "L2 pytest（离线，无需 LLM/DB）", out.strip()[-600:])


# ---------------------------------------------------------------- L3 服务

def check_service(root: Path) -> None:
    if not all(_has_module(m) for m in ("fastapi", "pydantic_settings")):
        _skip("L3 FastAPI import", "缺少 fastapi / pydantic-settings")
        return
    code, out = _run(
        [PY, "-c", "from backend.main import app; print(sorted(r.path for r in app.routes))"],
        root,
    )
    _report(
        code == 0 and "/health" in out and "/api/chat" in out,
        "L3 FastAPI app 可导入且路由注册（/health + /api/chat）",
        out.strip()[-600:],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Scaffold 产物验收回归")
    parser.add_argument("--strict", action="store_true", help="任一级被跳过即失败（CI 用）")
    parser.add_argument("--keep", action="store_true", help="保留临时目录以便排查")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

    if not SCAFFOLD.exists():
        print(f"错误：找不到 {SCAFFOLD}", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="scaffold-verify-"))
    root = tmp / "probe_app"
    try:
        print(f"临时目录：{root}\n")
        code, out = _run(
            [PY, str(SCAFFOLD), "--name", "probe_app", "--domain", "gov", "--dir", str(tmp)],
            SCRIPT_DIR,
        )
        if code != 0:
            _report(False, "生成项目", out.strip()[-800:])
        else:
            _report(True, "生成项目")
            check_static(root)
            check_runtime(root)
            check_service(root)
    finally:
        if args.keep:
            print(f"\n临时目录保留：{root}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n通过 {len(PASSED)} · 失败 {len(FAILED)} · 跳过 {len(SKIPPED)}")
    if FAILED:
        print("失败项：" + "、".join(FAILED))
        return 1
    if args.strict and SKIPPED:
        print("--strict：存在被跳过的级别 —— " + "、".join(SKIPPED))
        return 1
    if SKIPPED:
        print("（跳过的级别需先装依赖才能验收；CI 请用 --strict）")
    print("✅ scaffold 产物验收通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
