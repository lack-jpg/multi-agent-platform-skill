#!/usr/bin/env python3
"""发布纪律校验：`VERSION` ↔ `CHANGELOG.md` 必须一致。

用法：
    python scripts/check_release.py                  # 校验（CI 用，退出码 0/1）
    python scripts/check_release.py --bump patch     # 升版本号 + 在 CHANGELOG 开新条目
    python scripts/check_release.py --self-test      # 自检：塞入已知坏样例，必须报错

为什么需要它：`SKILL_MAINTENANCE.md §5` 定义了"什么是破坏性变更"、§10 定义了主/次/修订
的节奏，但在本脚本出现之前**没有任何东西承载它们**——版本号只存在于人的记忆里。
校验器把"版本号"从约定变成可失败的断言。

零依赖（只用标准库），和 `check_architecture.py` 同款纪律：`--self-test` 必须同时验证
"坏样例能报错"与"好样例不误报"两个方向。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
VERSION_FILE = ROOT / "VERSION"
CHANGELOG_FILE = ROOT / "CHANGELOG.md"

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# 形如 `## [1.2.3] - 2026-09-19` 或 `## [Unreleased]`
RELEASE_RE = re.compile(r"^##\s+\[([^\]]+)\]\s*(?:-\s*(\d{4}-\d{2}-\d{2}))?\s*$", re.M)
UNRELEASED = "Unreleased"


def _semver_key(v: str) -> tuple[int, int, int]:
    m = SEMVER_RE.match(v)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)  # type: ignore[return-value]


def parse_releases(text: str) -> list[tuple[str, str | None]]:
    """按出现顺序返回 [(版本名, 日期)]。

    先剥掉围栏代码块：本文档大量用 ``` 举"条目长什么样"的例子，那些示例行
    （`## [1.2.3] - 2026-01-01`）会被当成真实条目混进版本序列，把降序校验弄脏。
    """
    text = re.sub(r"^```.*?^```", "", text, flags=re.MULTILINE | re.DOTALL)
    return [(m.group(1), m.group(2)) for m in RELEASE_RE.finditer(text)]


def check(version_text: str, changelog_text: str) -> list[str]:
    """返回问题列表；空列表 = 通过。"""
    problems: list[str] = []
    version = version_text.strip()

    if not SEMVER_RE.match(version):
        return [f"VERSION 不是合法 semver（应为 X.Y.Z）：{version!r}"]

    releases = parse_releases(changelog_text)
    if not releases:
        return ["CHANGELOG.md 里没有任何 `## [x.y.z]` 条目"]

    names = [n for n, _ in releases]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        problems.append(f"CHANGELOG.md 有重复版本条目：{dup}")

    if UNRELEASED not in names:
        problems.append(f"CHANGELOG.md 缺少 `## [{UNRELEASED}]` 段——新改动会没地方记")

    released = [(n, d) for n, d in releases if n != UNRELEASED]
    if not released:
        return problems + ["CHANGELOG.md 只有 Unreleased，没有任何已发布版本"]

    top, top_date = released[0]
    if top != version:
        problems.append(
            f"VERSION={version} 与 CHANGELOG 最新条目 [{top}] 不一致"
            f"（要么把 VERSION 改成 {top}，要么先给 {version} 补一条 CHANGELOG）"
        )
    if not top_date:
        problems.append(f"CHANGELOG 条目 [{top}] 缺日期（应写成 `## [{top}] - YYYY-MM-DD`）")
    else:
        try:
            date.fromisoformat(top_date)
        except ValueError:
            problems.append(f"CHANGELOG 条目 [{top}] 的日期不合法：{top_date}")

    for (n1, _), (n2, _) in zip(released, released[1:]):
        if _semver_key(n2) >= _semver_key(n1):
            problems.append(f"已发布版本未按降序排列：[{n1}] 之后出现 [{n2}]")

    return problems


def run_check() -> int:
    if not VERSION_FILE.exists():
        print(f"✗ 找不到 {VERSION_FILE.name}")
        return 1
    if not CHANGELOG_FILE.exists():
        print(f"✗ 找不到 {CHANGELOG_FILE.name}")
        return 1

    problems = check(
        VERSION_FILE.read_text(encoding="utf-8"),
        CHANGELOG_FILE.read_text(encoding="utf-8"),
    )
    if problems:
        print("✗ 发布纪律校验失败：")
        for p in problems:
            print(f"    - {p}")
        return 1

    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    print(f"✓ 发布纪律校验通过：VERSION={version} 与 CHANGELOG 一致")
    return 0


def bump(part: str) -> int:
    """升版本号，并在 CHANGELOG 的 Unreleased 之后插入新条目。"""
    if not VERSION_FILE.exists() or not CHANGELOG_FILE.exists():
        print("✗ 缺少 VERSION 或 CHANGELOG.md")
        return 1

    old = VERSION_FILE.read_text(encoding="utf-8").strip()
    # ⚠️ 必须先校验再推算。`_semver_key` 对非法值兜底成 (0,0,0)，于是 `VERSION=v1.0`
    # 会被"成功地"升成 0.0.1 并**覆盖掉真实版本号**，还打印 ✓ —— 已实测。
    # `check()` 会拦非法 VERSION，`bump()` 却绕过了它；写坏的是唯一的版本号来源。
    if not SEMVER_RE.match(old):
        print(f"✗ VERSION 不是合法 semver（应为 X.Y.Z）：{old!r}")
        print("  —— 拒绝据此推算新版本号（非法值会被当成 0.0.0，把真实版本覆盖成 0.0.1）")
        return 1
    major, minor, patch = _semver_key(old)
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    new = f"{major}.{minor}.{patch}"
    today = date.today().isoformat()

    text = CHANGELOG_FILE.read_text(encoding="utf-8")
    anchor = re.search(rf"^##\s+\[{UNRELEASED}\].*$", text, re.M)
    if not anchor:
        print(f"✗ CHANGELOG.md 里找不到 `## [{UNRELEASED}]`，无法插入")
        return 1

    entry = (
        f"\n\n## [{new}] - {today}\n\n"
        f"### Added\n\n- \n\n"
        f"### Changed\n\n- \n\n"
        f"### Fixed\n\n- \n"
    )
    text = text[: anchor.end()] + entry + text[anchor.end():]
    CHANGELOG_FILE.write_text(text, encoding="utf-8")
    VERSION_FILE.write_text(new + "\n", encoding="utf-8")
    print(f"✓ 已升版本 {old} → {new}（{today}）")
    print(f"  CHANGELOG.md 已插入 [{new}] 条目——**记得填内容**，空条目等于没记")
    return 0


# ---------------------------------------------------------------- 自检

_GOOD_VERSION = "1.0.0"
_GOOD_CHANGELOG = """# Changelog

## [Unreleased]

## [1.0.0] - 2026-09-19

- 首个版本
"""

# (用例名, VERSION 内容, CHANGELOG 内容, 期望报错数——精确值)
_CASES: list[tuple[str, str, str, int]] = [
    ("好样例必须零误报", _GOOD_VERSION, _GOOD_CHANGELOG, 0),
    (
        "VERSION 与 CHANGELOG 顶条目不一致",
        "1.0.1",
        _GOOD_CHANGELOG,
        1,
    ),
    (
        "缺 Unreleased 段",
        _GOOD_VERSION,
        "# Changelog\n\n## [1.0.0] - 2026-09-19\n",
        1,
    ),
    ("VERSION 不是 semver", "v1.0", _GOOD_CHANGELOG, 1),
    (
        "条目缺日期",
        _GOOD_VERSION,
        "# Changelog\n\n## [Unreleased]\n\n## [1.0.0]\n",
        1,
    ),
    (
        "版本未降序",
        "2.0.0",
        "# Changelog\n\n## [Unreleased]\n\n## [2.0.0] - 2026-09-19\n\n"
        "## [1.0.0] - 2026-01-01\n\n## [1.5.0] - 2026-02-01\n",
        1,
    ),
    (
        "重复版本条目",
        _GOOD_VERSION,
        "# Changelog\n\n## [Unreleased]\n\n## [1.0.0] - 2026-09-19\n\n## [1.0.0] - 2026-08-01\n",
        2,   # 重复 + 由重复导致的"未降序"
    ),
]


def self_test() -> int:
    print("check_release 自检：")
    ok = True
    for name, vtext, ctext, expected in _CASES:
        problems = check(vtext, ctext)
        # 精确条数，不是"至少几条"。下界写法只拦得住"没报出来"，
        # 拦不住"多报"——而误报同样会让校验器失去信任，两类都要钉住。
        passed = len(problems) == expected
        detail = (f"报出 {len(problems)} 条" if passed
                  else f"期望 {expected} 条，实报 {len(problems)} 条：{problems}")
        ok &= passed
        print(f"  [{'ok' if passed else 'FAIL'}] {name} —— {detail}")
    print("✅ 自检通过：坏样例能报错，好样例不误报" if ok else "✗ 自检失败")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="发布纪律校验（VERSION ↔ CHANGELOG）")
    ap.add_argument("--bump", choices=["major", "minor", "patch"], help="升版本号并开新条目")
    ap.add_argument("--self-test", action="store_true", help="自检校验器本身")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.bump:
        return bump(args.bump)
    return run_check()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass
    raise SystemExit(main())
