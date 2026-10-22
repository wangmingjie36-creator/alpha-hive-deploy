"""requirements.txt 钉的版本 = 跑测试的解释器上实际装着的版本（v0.45.610）。

为什么有这条
------------
v0.45.610 前 `requirements.txt` 写 `pandas<3` / `numpy<2` / `yfinance<0.3`，生产 Mac 从 2026-02 起是 pandas 3.0.1 /
numpy 2.4.2 / yfinance 1.2.0。CI 照文件装 ⇒ 八个月里 CI 测的都不是生产，v0.45.601 的 buzz_v1 差分对拍只在 CI 红
（`pct_change` 缺省在两个大版本里换义）。没有任何东西会因为「文件与生产对不上」而红——这条就是那个会红的东西。

在哪红、红什么
--------------
- **生产 Mac**（`/usr/local/bin/python3` 跑整套）：有人 `pip install -U` 了某个包、没同步改 `requirements.txt` ⇒ 红，
  报出哪个包、钉的是几、装的是几。修法：确认新版本能用后改文件里的版本号（同一提交）。
- **CI / 照文件建的 venv**：包就是照文件装的，恒绿——那里本来就不会漂。
- 另守一件事：直接依赖一律 `==`（不许再写范围；范围正是这次漂了八个月的原因）。

`problems()` 是纯函数（文本 + 取版本函数 → 问题列表），下面的「有牙」组用合成文本证明每种漂移都会报。

v0.45.617：传递依赖也锁了（`constraints.txt`，由 `tools/constraints_lock.py` 用生产解释器生成）。
`TestConstraints` 按**本平台**核对：依赖闭包里的每个包都要锁、钉的 = 装着的；两份文件不许打架；过时条目要清。
生产 Mac 上核的是生产；CI 上能抓住「只在 Linux 冒出来、却没锁的传递依赖」（此前 python-dateutil 3.0 就这样多拉进 tzdata）。
"""
from __future__ import annotations

import importlib.util
import types
from importlib import metadata
from pathlib import Path
from typing import Callable, List, Optional

from packaging.requirements import Requirement

REPO = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO / "requirements.txt"
CONSTRAINTS = REPO / "constraints.txt"

_spec = importlib.util.spec_from_file_location("constraints_lock", REPO / "tools" / "constraints_lock.py")
CL = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CL)


def _installed(name: str) -> Optional[str]:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def problems(text: str, installed: Callable[[str], Optional[str]] = _installed) -> List[str]:
    """逐条核对：只认 `==` 钉死、环境标记为真的条目必须装着且版本相等。注释行 / 空行跳过。"""
    out = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        req = Requirement(line)
        specs = list(req.specifier)
        if len(specs) != 1 or specs[0].operator != "==":
            out.append(f"第 {lineno} 行 {req.name}：没有钉死（`{req.specifier}`）——直接依赖一律写 `==生产版本`")
            continue
        if req.marker is not None and not req.marker.evaluate():
            continue
        want = specs[0].version
        got = installed(req.name)
        if got is None:
            out.append(f"第 {lineno} 行 {req.name}=={want}：这个解释器上没装")
        elif got != want:
            out.append(f"第 {lineno} 行 {req.name}：文件钉 {want}，实际装的是 {got}——升级了就同一提交改文件")
    return out


def test_requirements_match_this_interpreter():
    found = problems(REQUIREMENTS.read_text(encoding="utf-8"))
    assert not found, "\n".join(found)


class TestHasTeeth:
    """合成文本：每种漂移都要报出来；没漂的不报。"""

    @staticmethod
    def _env(**versions):
        return lambda name: versions.get(name)

    def test_clean(self):
        assert problems("pandas==3.0.1\n# numpy<2\n\n", self._env(pandas="3.0.1")) == []

    def test_version_drift(self):
        (p,) = problems("pandas==2.3.3\n", self._env(pandas="3.0.1"))
        assert "2.3.3" in p and "3.0.1" in p

    def test_not_installed(self):
        (p,) = problems("plotly==5.24.1\n", self._env())
        assert "没装" in p

    def test_range_is_rejected(self):
        (p,) = problems("pandas>=2.1.0,<3\n", self._env(pandas="3.0.1"))
        assert "没有钉死" in p

    def test_false_marker_is_skipped_but_still_must_be_pinned(self):
        assert problems('pywebview==6.2.1; sys_platform == "nonexistent"\n', self._env()) == []
        (p,) = problems('pywebview>=6.2; sys_platform == "nonexistent"\n', self._env())
        assert "没有钉死" in p

    def test_trailing_comment_is_ignored(self):
        assert problems("numpy==2.4.2  # 数值计算\n", self._env(numpy="2.4.2")) == []


# ==================== v0.45.617：传递依赖的约束文件 ====================

def test_constraints_match_this_interpreter():
    found = CL.check(REQUIREMENTS.read_text(encoding="utf-8"), CONSTRAINTS.read_text(encoding="utf-8"))
    assert not found, "\n".join(found)


class TestConstraintsHaveTeeth:
    """合成依赖图：app → lib →（只在 Linux）leaf；lib[fast] → speedup。每种漂移都要报，不该报的不报。"""

    TOOLING_PINS = "".join(f"{t}==1.0\n" for t in CL.TOOLING)

    @staticmethod
    def _universe(**pkgs):
        """{名: (版本, [requires…])} → 假的 `metadata.distribution`。测试工具当叶子包一并装上。"""
        pkgs = {**{t: ("1.0", []) for t in CL.TOOLING}, **pkgs}
        table = {CL.canon(k): (k, v) for k, v in pkgs.items()}

        def dist(name):
            if CL.canon(name) not in table:
                raise metadata.PackageNotFoundError(name)
            real, (ver, reqs) = table[CL.canon(name)]
            return types.SimpleNamespace(metadata={"Name": real}, version=ver, requires=reqs)
        return dist

    def _check(self, constraints, *, platform="linux", req="app==1.0\n", **pkgs):
        base = {"app": ("1.0", ["lib>=1"]), "lib": ("1.0", ["leaf; sys_platform == 'linux'"]), "leaf": ("1.0", [])}
        return CL.check(req, self.TOOLING_PINS + constraints, CL._env(platform), self._universe(**{**base, **pkgs}))

    def test_clean(self):
        assert self._check("app==1.0\nlib==1.0\nleaf==1.0\n") == []

    def test_unpinned_transitive_dep(self):
        (p,) = self._check("app==1.0\nlib==1.0\n")
        assert "leaf==1.0" in p and "没锁" in p

    def test_version_drift(self):
        (p,) = self._check("app==1.0\nlib==1.0\nleaf==0.9\n")
        assert "钉 0.9" in p and "装的是 1.0" in p

    def test_range_is_rejected(self):
        assert any("没有钉死" in p for p in self._check("app==1.0\nlib>=1\nleaf==1.0\n"))

    def test_marker_false_here_is_not_required_and_not_stale(self):
        """leaf 只在 Linux 用到：在 macOS 上不要求锁，锁了也不算过时（v0.45.617 自查补的边界）。"""
        assert self._check("app==1.0\nlib==1.0\n", platform="darwin") == []
        assert self._check("app==1.0\nlib==1.0\nleaf==1.0\n", platform="darwin") == []

    def test_stale_entry_installed_but_unreachable(self):
        (p,) = self._check("app==1.0\nlib==1.0\nleaf==1.0\nold==1.0\n", old=("1.0", []))
        assert "old==1.0" in p and "过时" in p

    def test_stale_entry_not_installed_here_is_ignored(self):
        """如 Linux 上的 pyobjc：约束里有、本平台没装也用不到 ⇒ 不归本平台管。"""
        assert self._check("app==1.0\nlib==1.0\nleaf==1.0\npyobjc-core==1.0\n") == []

    def test_requirements_and_constraints_disagree(self):
        found = self._check("app==2.0\nlib==1.0\nleaf==1.0\n")
        assert any("两份文件不一致" in p for p in found) and any("钉 2.0" in p for p in found)

    def test_extras_are_followed(self):
        found = self._check("app==1.0\nlib==1.0\nleaf==1.0\n",
                            app=("1.0", ["lib[fast]>=1"]),
                            lib=("1.0", ["leaf; sys_platform == 'linux'", "speedup; extra == 'fast'"]),
                            speedup=("2.0", []))
        (p,) = found
        assert "speedup==2.0" in p and "没锁" in p

    def test_required_but_not_installed(self):
        found = self._check("app==1.0\nlib==1.0\n", lib=("1.0", ["ghost"]))
        assert any("ghost" in p and "没装" in p for p in found)
