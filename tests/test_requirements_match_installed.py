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
"""
from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import Callable, List, Optional

from packaging.requirements import Requirement

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"


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
