"""测试里不许写死 `/usr/local/bin/python3` 当可执行文件（v0.45.377）。

CLAUDE.md 的「一律用 /usr/local/bin/python3」是给**人手动跑**的规矩；测试要起子进程时
该用 `sys.executable`（= 正在跑这套测试的解释器）。在 Mac 上按规矩跑 pytest 时两者相同，
所以写死的版本在本机永远是绿的；GitHub runner 上那个路径不存在 ⇒ FileNotFoundError。

同一形状已经出现两次：
  · v0.45.117 `test_no_undefined_names.py`（CI 首跑 7 errors，改成 `PY = sys.executable`）
  · v0.45.317 `test_ghpages_data_root_migration.py`（照抄了写死路径，CI 上红了一路，v0.45.377 修）
第二次说明「修一处、写一段注释」挡不住下一次，所以这里做成会红的守卫。

判据：AST 里**恰好等于**该路径的字符串常量才算（那是被当成 argv[0] 用的形状）；
写在较长的说明文字 / 报错提示里的（如「请运行 /usr/local/bin/python3 deploy_orchestrator.py」）不算。
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_BANNED = "/usr/local/bin/" + "python3"   # 拼接：免得本守卫自己命中自己


def _hits(source: str) -> list[int]:
    return [n.lineno for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Constant) and n.value == _BANNED]


def test_no_test_hardcodes_the_mac_interpreter():
    bad = {}
    for p in sorted(_TESTS.rglob("*.py")):
        lines = _hits(p.read_text(encoding="utf-8"))
        if lines:
            bad[str(p.relative_to(_TESTS))] = lines
    assert not bad, (
        f"这些测试把 {_BANNED} 写死成可执行文件（CI 上不存在）：{bad}\n"
        "改成 sys.executable —— 见 test_no_undefined_names.py 的 PY。")


class TestGuardHasTeeth:
    def test_flags_argv_usage(self):
        assert _hits(f'subprocess.run(["{_BANNED}", "-O", "-c", "x"])') == [1]

    def test_ignores_path_inside_prose(self):
        assert _hits(f'msg = "请运行 {_BANNED} deploy.py"') == []

    def test_scan_actually_sees_this_directory(self):
        """反向自证：扫描确实读到了测试文件，否则「没有命中」恒真。"""
        assert len(list(_TESTS.rglob("test_*.py"))) > 50
