#!/usr/bin/env python3
"""由生产解释器推出 `requirements.txt` 的传递闭包，写成 `constraints.txt`（v0.45.617）。

为什么
------
v0.45.610 起 `requirements.txt` 只钉直接依赖（`==` 生产版本），传递依赖仍由 pip 每次现解析 ⇒ CI 一直往前漂：
10-22 实测 52 个传递依赖里 37 个与生产不同，python-dateutil 2.9 → 3.0（还因此多拉进 tzdata）、protobuf 5 → 7……
同一个提交过几周可能在 CI 上无故变红，而这种漂移没有任何东西会红。本工具把整棵依赖树按生产钉死，CI 安装时
`pip install -r requirements.txt -c constraints.txt pytest pytest-timeout ruff`。

闭包怎么算
----------
- 根 = `requirements.txt` 的有效行 + CI 另装的测试工具 `TOOLING`（pytest / pytest-timeout / ruff 的版本同样会漂，
  ruff 升级还会改 lint 结果）。
- 沿已装发行版的元数据（`importlib.metadata.requires`）走，按环境标记与所需 extras 取舍；`PLATFORMS` 里
  macOS（生产）与 Linux（CI）各走一遍取并集——两边的版本都取**本解释器上装着的**（即生产版本）。
- 只在 Linux 上才需要、生产没装的包无法按生产钉 ⇒ `closure()` 把它们报为缺失，`--write` 拒绝写（不猜版本）。

用法（**必须用生产解释器跑**）
------------------------------
    /usr/local/bin/python3 tools/constraints_lock.py           # 只比对：constraints.txt 与本机闭包是否一致
    /usr/local/bin/python3 tools/constraints_lock.py --write   # 重写 constraints.txt

生产上升级 / 新装包后：同一提交改 `requirements.txt`（直接依赖）并 `--write`；
守卫 `tests/test_requirements_match_installed.py` 在生产解释器上核对两份文件，漂了就红。
"""
from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from packaging.markers import default_environment
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
CONSTRAINTS = ROOT / "constraints.txt"

#: CI 在 requirements 之外另装的测试工具（tests.yml / clockshift.yml）；它们的版本也会漂，一并锁住
TOOLING = ("pytest", "pytest-timeout", "ruff")

#: 闭包按这两种平台各走一遍：生产 = macOS，CI = Linux（ubuntu-latest, x86_64）
PLATFORMS: Dict[str, Dict[str, str]] = {
    "darwin": {"sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix"},
    "linux": {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
              "platform_machine": "x86_64", "platform_release": "", "platform_version": ""},
}

HEADER = """\
# 由 tools/constraints_lock.py 生成，勿手改（v0.45.617）。
# 内容 = requirements.txt + CI 测试工具（{tooling}）在生产解释器 /usr/local/bin/python3 上的完整传递闭包，
# 版本一律取生产上装着的那个；macOS 与 Linux 两种环境标记取并集（只在 macOS 用到的包 CI 上会被忽略）。
# CI 安装：pip install -r requirements.txt -c constraints.txt {tooling}
# 生产上升级 / 新装包 ⇒ 同一提交改 requirements.txt 并跑 `/usr/local/bin/python3 tools/constraints_lock.py --write`；
# tests/test_requirements_match_installed.py 在生产解释器上核对，漂了就红。
"""


def canon(name: str) -> str:
    """PEP 503 规范名：大小写与 `-_.` 不敏感。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def roots(requirements_text: str) -> List[Requirement]:
    out = []
    for raw in requirements_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(Requirement(line))
    return out + [Requirement(t) for t in TOOLING]


def _env(platform: str) -> Dict[str, str]:
    env = dict(default_environment())
    env.update(PLATFORMS[platform])
    return env


def closure(root_reqs: Iterable[Requirement], env: Mapping[str, str],
            dist: Callable[[str], "metadata.Distribution"] = metadata.distribution
            ) -> Tuple[Dict[str, Tuple[str, str]], Dict[str, str]]:
    """沿元数据走出闭包。返回 ({规范名: (发行名, 版本)}, {缺失的规范名: 谁要的})。"""
    found: Dict[str, Tuple[str, str]] = {}
    missing: Dict[str, str] = {}
    queue = [(r.name, frozenset(r.extras), "requirements.txt")
             for r in root_reqs if r.marker is None or r.marker.evaluate(dict(env, extra=""))]
    done = set()
    while queue:
        name, extras, parent = queue.pop()
        key = (canon(name), extras)
        if key in done:
            continue
        done.add(key)
        try:
            d = dist(name)
        except metadata.PackageNotFoundError:
            missing.setdefault(canon(name), parent)
            continue
        found[canon(name)] = (d.metadata["Name"], d.version)
        for spec in d.requires or []:
            r = Requirement(spec)
            if r.marker is None or any(r.marker.evaluate(dict(env, extra=e)) for e in (extras or {""})):
                queue.append((r.name, frozenset(r.extras), d.metadata["Name"]))
    return found, missing


def lock(requirements_text: str, platforms: Iterable[str] = tuple(PLATFORMS)
         ) -> Tuple[Dict[str, Tuple[str, str]], Dict[str, str]]:
    """各平台闭包的并集（版本都取本解释器上装着的）。"""
    found: Dict[str, Tuple[str, str]] = {}
    missing: Dict[str, str] = {}
    for p in platforms:
        f, m = closure(roots(requirements_text), _env(p))
        found.update(f)
        for k, v in m.items():
            missing.setdefault(k, f"{v}（{p}）")
    return found, missing


def render(found: Mapping[str, Tuple[str, str]]) -> str:
    lines = [f"{found[k][0]}=={found[k][1]}" for k in sorted(found)]
    return HEADER.format(tooling=" ".join(TOOLING)) + "\n".join(lines) + "\n"


def parse_pins(text: str) -> Dict[str, Tuple[str, Optional[str]]]:
    """constraints.txt → {规范名: (行原文, 钉的版本 / 不是 `==` 则 None)}。"""
    out: Dict[str, Tuple[str, Optional[str]]] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        r = Requirement(line)
        specs = list(r.specifier)
        pinned = specs[0].version if len(specs) == 1 and specs[0].operator == "==" else None
        out[canon(r.name)] = (line, pinned)
    return out


def check(requirements_text: str, constraints_text: str, env: Optional[Mapping[str, str]] = None,
          dist: Callable[[str], "metadata.Distribution"] = metadata.distribution) -> List[str]:
    """按**本平台**核对约束文件，返回问题列表（空 = 通过）。在生产 Mac 上核的是生产；在 CI 上能抓住
    「只在 Linux 冒出来、却没锁的传递依赖」（v0.45.617 前 python-dateutil 3.0 就这样多拉进 tzdata）。"""
    env = dict(default_environment()) if env is None else dict(env)
    pins = parse_pins(constraints_text)
    out = [f"constraints.txt `{line}`：没有钉死——一律 `==`" for line, ver in pins.values() if ver is None]
    found, missing = closure(roots(requirements_text), env, dist)
    for k, parent in sorted(missing.items()):
        out.append(f"{k}（{parent} 要它）：这个解释器上没装")
    for k, (name, ver) in sorted(found.items()):
        if k not in pins:
            out.append(f"{name}=={ver}：在依赖闭包里，constraints.txt 却没锁——用生产解释器跑 tools/constraints_lock.py --write")
        elif pins[k][1] is not None and pins[k][1] != ver:
            out.append(f"{name}：constraints.txt 钉 {pins[k][1]}，实际装的是 {ver}——升级了就同一提交重写约束文件")
    for r in roots(requirements_text):
        specs = list(r.specifier)
        k = canon(r.name)
        if len(specs) == 1 and specs[0].operator == "==" and k in pins and pins[k][1] not in (None, specs[0].version):
            out.append(f"{r.name}：requirements.txt 钉 {specs[0].version}，constraints.txt 钉 {pins[k][1]}——两份文件不一致")
    reachable = set(found)                            # 过时 = 哪个平台的闭包都走不到（只在别的平台用得到的不算）
    for p in PLATFORMS:
        reachable |= set(closure(roots(requirements_text), _env(p), dist)[0])
    for k, (line, _ver) in sorted(pins.items()):
        if k in reachable:
            continue
        try:
            dist(k)
        except metadata.PackageNotFoundError:
            continue                                  # 本平台用不到也没装（如 Linux 上的 pyobjc）：不归本平台管
        out.append(f"constraints.txt `{line}`：装着却已不在依赖闭包里——过时条目，重写约束文件")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--write", action="store_true", help="重写 constraints.txt（必须用生产解释器）")
    args = ap.parse_args(argv)
    found, missing = lock(REQUIREMENTS.read_text(encoding="utf-8"))
    if missing:
        for k, v in sorted(missing.items()):
            print(f"缺：{k}（{v} 要它）——本解释器没装，无法按生产版本钉", file=sys.stderr)
        return 2
    text = render(found)
    if args.write:
        CONSTRAINTS.write_text(text, encoding="utf-8")
        print(f"已写 {CONSTRAINTS.name}：{len(found)} 个包")
        return 0
    current = CONSTRAINTS.read_text(encoding="utf-8") if CONSTRAINTS.exists() else ""
    if current == text:
        print(f"{CONSTRAINTS.name} 与本解释器的闭包一致（{len(found)} 个包）")
        return 0
    print(f"{CONSTRAINTS.name} 与本解释器的闭包不一致；确认后用 --write 重写", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
