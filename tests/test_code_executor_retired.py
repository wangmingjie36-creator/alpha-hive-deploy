"""墓碑（v0.45.750）：CodeExecutorAgent（Phase 3 P4「代码执行蜂」）退役，别让它悄悄回到蜂群。

它在每只标的上起子进程跑 yfinance 取一个月日线，按「RSI 超买 / 超卖，否则价在 20 日均线上 / 下」投一张方向票。
维度是 `technical`（不进五维加权），但方向票照样进 `QueenDistiller._compute_direction_vote`。
2026-09-10 ~ 11-06 生产 1200 行（用户 11-09 定退役）：
  · 失败（取数 / 解析 / 异常）约 10%；
  · 拿掉它这一票，`rule_direction` 约 14% 改变（多为「看空 ↔ 中性」）；
  · 同日横截面对 T+7 收盘收益：看多组 − 看空组 −0.18pp，t −0.35，命中 50.5% ⇒ 无可测信息。
重新设计成别的信号（分析师修正）的方案用户 11-09 看过，决定不做。

守的是：执行器四个模块不在了、扫描主流程不再组装它、配置项不在了、归档不再从它取 `fund.*`。
历史记录里的 `agent_details.CodeExecutorAgent` 原样留着——共振加成 / F&G 敞口门两份前瞻检验的重放
都从记录里的蜂结果重建投票，旧记录带它、新记录不带，各自自洽。
"""
from __future__ import annotations

import ast
from pathlib import Path

from tests import _ast_cache
from tests._repo_files import own_python_files

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__
RETIRED_MODULES = ("code_executor_agent", "code_executor", "code_generator", "debugger")
#: 退役前写成的历史重放脚本：import 已包在 try 里、模块不在就不打补丁
IMPORT_ALLOWED = {"experiments/replay_1012_full_day_v0484.py"}


def _importers() -> dict:
    files, _how = own_python_files(REPO_ROOT)
    out = {}
    for f in files:
        rel = f.relative_to(REPO_ROOT).as_posix()
        if rel.startswith("tests/") or rel in IMPORT_ALLOWED:
            continue
        try:
            tree = _ast_cache.parse_file(f, errors="replace")
        except SyntaxError:
            continue
        hit = _imports(tree)
        if hit:
            out[rel] = hit
    return out


def _imports(tree) -> list:
    hits = []
    for n in ast.walk(tree):
        names = ([a.name for a in n.names] if isinstance(n, ast.Import)
                 else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
        hits += [x for x in names if x.split(".")[0] in RETIRED_MODULES]
    return hits


class TestCodeExecutorStaysRetired:

    def test_modules_are_gone(self):
        back = [m for m in RETIRED_MODULES if (REPO_ROOT / f"{m}.py").exists()]
        assert not back, f"已退役的模块又回来了：{back}"

    def test_nobody_imports_them(self):
        assert _importers() == {}

    def test_scan_does_not_assemble_it(self):
        src = (REPO_ROOT / "alpha_hive_daily_report.py").read_text(encoding="utf-8")
        assert "code_executor_agent" not in src and "CODE_EXECUTION_CONFIG" not in src

    def test_config_entry_is_gone(self):
        import config
        assert not hasattr(config, "CODE_EXECUTION_CONFIG")

    def test_archive_no_longer_reads_it(self):
        import signal_archive as sa
        assert not any(k.startswith("fund.") for k in sa.SIGNAL_EXTRACTORS)

    def test_phase1_peers_drop_it(self):
        from real_data_sources import _PHASE1_PEERS
        assert "CodeExecutorAgent" not in _PHASE1_PEERS


class TestDetectorHasTeeth:

    def test_import_detector(self):
        assert _imports(ast.parse("from code_executor import CodeExecutor\n"))
        assert _imports(ast.parse("import debugger as d\n"))
        assert not _imports(ast.parse("import code_version\n"))
