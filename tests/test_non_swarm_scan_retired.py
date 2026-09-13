"""非蜂群扫描已于 v0.45.213 退役——这组闸防它回来（同 `test_no_crewai_dependency` 的先例）。

退役的是 `alpha_hive_daily_report.py` 不带 `--swarm` 时走的 `run_daily_scan`
规则引擎路径。它写穿生产有**两层**，且第一层在 `save_report` 之前：

  1. **扫描过程中**：`_analyze_ticker_safe → generate_ml_enhanced_report →
     probability_scorecard.record_published`，不带蜂群方向。账本按 (date, ticker)
     **先写者占位** ⇒ 规则引擎先跑，同日蜂群扫描真正印出去的那个数被拒收，
     账本里留下 direction=None 的一行（v0.45.213 沙箱实测）。
  2. **save_report**：日报产物留在生产工作区，被下一次生产扫描的白名单提交带走
     （2026-03-13 的蜂群日报至今被规则引擎版顶着，见
     `test_git_failures_are_visible::TestNonProductionScanIsNotDeployed`）。

所以「照 `--samples-only` 在 save_report 之前短路」只堵得住第 2 层——闸必须在扫描之前。

三组，各堵一处：
  1. CLI：不带 `--swarm` 的扫描在**构造 reporter 之前**就退出（exit 2）。
     正对照：`--swarm` 真的构造并调 `run_swarm_scan`；`--check-earnings` 不被误伤。
     ⚠️ 负例一律带 `--force`：不带的话，周末/假日时旧代码会被交易日护栏先挡掉、
     同样不构造 reporter —— 这组对旧 bug 就只在交易日才红。
     另：部署判定回 `deploy_env="none"`（蜂群报告没被认出来）时 CLI 不许报「生产环境」。
  2. 账本写入者：全仓生产代码里每个 `generate_ml_enhanced_report(...)` 都带
     `swarm_direction`。**不点名** `run_daily_scan`——换个名字重建同一形状照样红。
     局限：AST 只看得见关键字在不在，看不见 `swarm_direction=None` 这种显式传空。
  3. 名字：`AlphaHiveDailyReporter` 上不许再有 `run_daily_scan` / `_analyze_ticker_safe`。
"""

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import alpha_hive_daily_report as adr  # noqa: E402
from _repo_files import own_python_files  # noqa: E402


# ═════════════════════════════ 1. CLI 闸在扫描之前 ═════════════════════════════

@pytest.fixture
def cli(monkeypatch):
    calls = []
    # 默认扫描返回空报告 ⇒ 走空扫描护栏早退，不碰 save_report / 部署；个别测试改写它
    state = SimpleNamespace(
        swarm_report={"swarm_metadata": {"tickers_analyzed": 0}, "opportunities": []},
        deploy_result={})

    class SpyReporter:
        """记下 main() 对 reporter 做的每一件事。"""

        date_str = "2026-09-14"

        def __init__(self, date_override=None):
            calls.append("__init__")

        def run_swarm_scan(self, focus_tickers=None):
            calls.append("run_swarm_scan")
            return state.swarm_report

        def check_earnings_updates(self, tickers=None):
            calls.append("check_earnings_updates")
            return {"reporting_today": [], "updated": []}

        def save_report(self, report):
            calls.append("save_report")
            return "alpha-hive-daily-2026-09-14.md"

        def auto_commit_and_notify(self, report):
            calls.append("auto_commit_and_notify")
            return state.deploy_result

        def __getattr__(self, name):
            # run_daily_scan …… 以及任何没料到的方法，被调到就记名
            def _record(*a, **k):
                calls.append(name)
                return {"opportunities": [{"ticker": "NVDA"}]}
            return _record

    import yf_gate
    monkeypatch.setattr(adr, "AlphaHiveDailyReporter", SpyReporter)
    monkeypatch.setattr(yf_gate, "install", lambda: False)      # 不改全局 yfinance
    monkeypatch.setattr(adr._timing, "write", lambda *a, **k: None)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["alpha_hive_daily_report.py", *argv])
        return adr.main()

    return SimpleNamespace(run=run, calls=calls, state=state)


class TestCliRejectsNonSwarmScanBeforeScanning:

    @pytest.mark.parametrize("argv", [
        ["--force"],
        ["--force", "--no-llm"],
        ["--force", "--no-llm", "--tickers", "NVDA", "TSLA"],
        ["--force", "--all-watchlist"],
        ["--force", "--extended-pool", "--max-tickers", "3"],
        ["--force", "--no-llm", "--samples-only"],
    ], ids=["bare", "no-llm", "tickers", "all-watchlist", "extended-pool", "samples-only"])
    def test_exits_before_constructing_the_reporter(self, cli, argv, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.run(*argv)
        assert exc.value.code == 2
        assert cli.calls == [], (
            f"不带 --swarm 的扫描走到了 reporter：{cli.calls} —— reporter 一构造、一扫描，"
            "概率账本就被占位（save_report 之前）")
        err = capsys.readouterr().err
        assert "--swarm" in err and "退役" in err, err

    def test_swarm_scan_still_runs(self, cli):
        """正对照：闸不是把整个 CLI 堵死了。"""
        cli.run("--swarm", "--force", "--no-llm", "--tickers", "NVDA")
        assert cli.calls == ["__init__", "run_swarm_scan"]

    def test_check_earnings_does_not_need_swarm(self, cli):
        """正对照：--check-earnings 不扫描，不该被这道闸误伤。"""
        cli.run("--check-earnings")
        assert cli.calls == ["__init__", "check_earnings_updates"]

    def test_unrecognized_report_is_not_announced_as_production(self, cli, capsys):
        """CLI 只跑蜂群之后，部署判定回 `deploy_env="none"` 就是回归（蜂群报告没被认出来）。
        CLI 不许照旧打「🧠 生产环境」。v0.45.210 变异 M12（判断改回 `== "test"`）当时 0 红，
        因为 main() 这段没有测试；本条补上。"""
        cli.state.swarm_report = {"opportunities": [{"ticker": "NVDA"}]}   # 缺 swarm_metadata
        cli.state.deploy_result = {
            "deploy_env": "none",
            "git_push": {"success": False, "skipped": "non_production", "remote": None},
            "uncommitted_report_artifacts": ["alpha-hive-daily-2026-09-14.json"],
        }
        cli.run("--swarm", "--force", "--no-llm", "--tickers", "NVDA")
        out = capsys.readouterr().out
        assert cli.calls == ["__init__", "run_swarm_scan", "save_report", "auto_commit_and_notify"]
        assert "生产环境" not in out, out
        assert "未推送" in out and "alpha-hive-daily-2026-09-14.json" in out, out


# ═════════════════════════════ 2. 不带方向的账本写入者 ═════════════════════════════

CALL = "generate_ml_enhanced_report"


def _directionless_calls(source: str):
    """调 `generate_ml_enhanced_report(...)` 却不传 `swarm_direction` 的行号。"""
    return [
        node.lineno for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) == CALL
        and "swarm_direction" not in {k.arg for k in node.keywords}
    ]


def _production_sources():
    files, _ = own_python_files(_ROOT)
    out = []
    for p in files:
        rel = p.relative_to(_ROOT)
        if rel.parts[0] == "tests":
            continue
        try:
            out.append((rel.as_posix(), p.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return out


class TestNoDirectionlessLedgerWriter:

    def test_every_production_call_passes_swarm_direction(self):
        bad = [f"{rel}:{line}" for rel, src in _production_sources()
               for line in _directionless_calls(src)]
        assert not bad, (
            "这些调用不带蜂群方向 —— generate_ml_enhanced_report 会把 direction=None 记进"
            "概率账本，而账本按 (date, ticker) 先写者占位，同日蜂群扫描的真实记录会被拒收：\n  "
            + "\n  ".join(bad))

    def test_scanner_sees_the_live_call_sites(self):
        """正对照：扫描器扫得到两个真实调用点，上一条不是在空集上恒真。"""
        sites = {rel for rel, src in _production_sources()
                 for node in ast.walk(ast.parse(src))
                 if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == CALL}
        assert {"alpha_hive_daily_report.py", "generate_ml_report.py"} <= sites

    def test_scanner_flags_the_retired_call(self):
        """有牙：退役前 `_analyze_ticker_safe` 的原句被抓；带方向的蜂群句放行。"""
        src = ("ml_report = self.ml_generator.generate_ml_enhanced_report(\n"
               "    ticker, realtime_metrics\n"
               ")\n"
               "enhanced = self.ml_generator.generate_ml_enhanced_report(\n"
               "    ticker, metrics, swarm_direction=_sr.get('direction'))\n")
        assert _directionless_calls(src) == [1]


# ═════════════════════════════ 3. 名字 ═════════════════════════════

@pytest.mark.parametrize("name", ["run_daily_scan", "_analyze_ticker_safe"])
def test_retired_method_is_gone(name):
    assert not hasattr(adr.AlphaHiveDailyReporter, name), (
        f"AlphaHiveDailyReporter.{name} 回来了——非蜂群扫描已于 v0.45.213 退役，"
        "理由见本文件 docstring 与 CHANGELOG")
