"""墓碑（v0.45.552）：`--samples-only` 样本积累模式整个退役——连同只为它服务的 `--extended-pool` / `--max-tickers`。

来源：v0.23.1（2026-04-17）「混合双轨」——周日定时任务 `alpha-hive-sample-accumulator` 跑
`--swarm --no-llm --extended-pool --max-tickers 50 --samples-only`，扫 101 只扩展池、只写 pheromone.db，
另存一份「供 Hive app 读取」的 `.samples-only-<date>.json`（仓内从来没有这个读者；v0.45.543 读者登记表
登记为 PENDING_DEAD）。定时任务 v0.42.1 停用、v0.45.316 删除；生产库最后一个周日样本 2026-05-17。

删掉的（四组，每组一条断言）：
  1. CLI 三个参数：`--samples-only` / `--extended-pool` / `--max-tickers`（及 `args.<它们>` 的读取）。
     `--extended-pool` 实测只给积累器用过（候补池标的在生产库只出现在 4 个周日、工作日 0 行）；
     脱离 `--samples-only` 的短路单独用 = 101 只、会部署 gh-pages、写概率账本的生产扫描。
  2. 产物：`.samples-only-<date>.json`（比**完整文件名**，f-string 占位渲染成 `{}`；只比片段会误中注释里的
     `--samples-only` 字样——同 `test_dead_chains_batch2_retired` 首版的假红）。
  3. `health_check.check_sample_accumulator`：定时任务没了，它恒报 fail（「上周日 … 仅 0 笔」）。

刻意保留的（第 4 组正面钉住，免得下一个「顺手清理」把它们带走）：
  - backtester / dashboard_renderer / portfolio_backtest 的 `exclude_nontrading_days`：库里 110 行周日样本还在。
  - `.gitignore` 的 `.samples-only-*.json`：开发检出根目录曾有 04-26 ~ 05-17 的 4 个旧文件（10-19 已归档到数据根
    `_archive/samples_only/`，sha256 记录同目录）；这行留作保险——从归档拷回检出里查东西时不会被 `git add -A` 带走。
  - `config.WATCHLIST_EXTENDED`：候补池（降级标的的名称 / 板块 / 失效条件），要扫就升进 WATCHLIST。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _repo_files import own_python_files  # noqa: E402
from tests import _ast_cache  # noqa: E402
from tests.test_dead_chains_batch2_retired import string_constants  # noqa: E402

RETIRED_OPTIONS = ("--samples-only", "--extended-pool", "--max-tickers")
RETIRED_ARGS = {"samples_only", "extended_pool", "max_tickers"}
RETIRED_ARTIFACT = ".samples-only-{}.json"


# ═════════════════════════════ 判据（纯函数，喂合成源码自证有牙） ═════════════════════════════

def cli_options(tree) -> set:
    """`*.add_argument('--x', ...)` 的选项名。"""
    out = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "add_argument"):
            out |= {a.value for a in n.args if isinstance(a, ast.Constant) and isinstance(a.value, str)}
    return out


def args_reads(tree) -> set:
    """`args.x` 与 `getattr(args, "x", ...)` 读到的名字（只认名叫 args 的对象）。"""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "args":
            out.add(n.attr)
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr"
              and len(n.args) >= 2 and isinstance(n.args[0], ast.Name) and n.args[0].id == "args"
              and isinstance(n.args[1], ast.Constant)):
            out.add(n.args[1].value)
    return out


def _tree(rel: str):
    return _ast_cache.parse_file(REPO_ROOT / rel)


# ═════════════════════════════ 1–3. 删掉的别回来 ═════════════════════════════

class TestSamplesOnlyModeStaysRetired:

    def test_cli_options_are_gone(self):
        back = cli_options(_tree("alpha_hive_daily_report.py")) & set(RETIRED_OPTIONS)
        assert not back, (f"退役的 CLI 参数又回来了：{sorted(back)}（v0.45.552 删除）。`--extended-pool` 脱离 "
                          "`--samples-only` 的短路就是 101 只、会部署的生产扫描；要扩池把标的升进 config.WATCHLIST")

    def test_args_are_not_read(self):
        back = args_reads(_tree("alpha_hive_daily_report.py")) & RETIRED_ARGS
        assert not back, f"main() 又在读退役参数：{sorted(back)}"

    def test_artifact_name_is_gone_repo_wide(self):
        files, _ = own_python_files(REPO_ROOT)
        back = [str(p.relative_to(REPO_ROOT)) for p in files
                if "tests" not in p.relative_to(REPO_ROOT).parts
                and RETIRED_ARTIFACT in string_constants(_ast_cache.parse_file(p))]
        assert not back, f"`.samples-only-<date>.json` 的写入又出现了：{back}——它全仓零读者（v0.45.552 删除）"

    def test_health_check_no_longer_checks_the_retired_task(self):
        tree = _tree("health_check.py")
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "check_sample_accumulator" not in names | called, (
            "check_sample_accumulator 回来了——定时任务 v0.45.316 已删，它只会每次报 fail")


@pytest.fixture
def cli(monkeypatch):
    import alpha_hive_daily_report as adr
    import yf_gate

    calls = []

    class SpyReporter:
        date_str = "2026-09-14"

        def __init__(self, date_override=None):
            calls.append("__init__")

        def run_swarm_scan(self, focus_tickers=None):
            calls.append(("run_swarm_scan", tuple(focus_tickers or ())))
            # 空报告 ⇒ 空扫描护栏早退，不碰 save_report / 部署
            return {"swarm_metadata": {"tickers_analyzed": 0}, "opportunities": []}

        def __getattr__(self, name):
            def _record(*a, **k):
                calls.append(name)
                return {}
            return _record

    monkeypatch.setattr(adr, "AlphaHiveDailyReporter", SpyReporter)
    monkeypatch.setattr(yf_gate, "install", lambda: False)
    monkeypatch.setattr(adr._timing, "write", lambda *a, **k: None)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["alpha_hive_daily_report.py", *argv])
        return adr.main()

    return SimpleNamespace(run=run, calls=calls)


class TestCliRefusesRetiredFlags:
    """行为面：退役参数大声失败（argparse exit 2），**不**静默跑一次生产扫描。"""

    @pytest.mark.parametrize("extra", [["--samples-only"], ["--extended-pool"], ["--max-tickers", "3"]],
                             ids=["samples-only", "extended-pool", "max-tickers"])
    def test_exits_before_constructing_the_reporter(self, cli, extra, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.run("--swarm", "--force", "--no-llm", "--tickers", "NVDA", *extra)
        assert exc.value.code == 2
        assert cli.calls == [], f"退役参数没被拒、走到了 reporter：{cli.calls}"
        assert "unrecognized arguments" in capsys.readouterr().err

    def test_plain_swarm_scan_still_runs(self, cli):
        """正对照：同一条命令去掉退役参数就照常扫描，且标的就是 --tickers 给的那几只。"""
        cli.run("--swarm", "--force", "--no-llm", "--tickers", "NVDA", "TSLA")
        assert cli.calls == ["__init__", ("run_swarm_scan", ("NVDA", "TSLA"))]


# ═════════════════════════════ 4. 刻意保留的别被顺手删掉 ═════════════════════════════

class TestKeptOnPurpose:

    @pytest.mark.parametrize("rel", ["backtester.py", "dashboard_renderer.py", "portfolio_backtest.py"])
    def test_nontrading_day_filters_remain(self, rel):
        assert "exclude_nontrading_days" in (REPO_ROOT / rel).read_text(encoding="utf-8"), (
            f"{rel} 的非交易日过滤没了——样本来源退役了，但 pheromone.db 里 2026-03~05 的周日样本还在，"
            "门面口径仍靠这道过滤")

    def test_gitignore_still_covers_old_files(self):
        lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert ".samples-only-*.json" in lines, "留作保险：从 _archive/samples_only/ 拷回检出里的旧文件不该被 git add -A 带走"

    def test_candidate_pool_remains(self):
        import config
        assert len(config.WATCHLIST_EXTENDED) > 0 and not set(config.WATCHLIST) & set(config.WATCHLIST_EXTENDED)


# ═════════════════════════════ 有牙：判据认得出它要拦的东西 ═════════════════════════════

class TestTombstoneHasTeeth:

    _OLD = (
        "import argparse, json\n"
        "def main(reporter):\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('--samples-only', action='store_true')\n"
        "    parser.add_argument('--extended-pool', action='store_true')\n"
        "    parser.add_argument('--max-tickers', type=int)\n"
        "    args = parser.parse_args()\n"
        "    n = getattr(args, 'max_tickers', None)\n"
        "    if getattr(args, 'extended_pool', False): pass\n"
        "    if args.samples_only:\n"
        "        open(reporter.report_dir / f'.samples-only-{reporter.date_str}.json', 'w')\n"
    )

    def test_negative_control_old_shape_is_caught(self):
        tree = ast.parse(self._OLD)
        assert cli_options(tree) >= set(RETIRED_OPTIONS)
        assert args_reads(tree) >= RETIRED_ARGS
        assert RETIRED_ARTIFACT in string_constants(tree)

    def test_prose_mentions_are_not_caught(self):
        """注释 / docstring 里提到 `--samples-only` 不算回来（本仓退役说明都这么写）。"""
        tree = ast.parse('def f(args):\n    """已退役的 `--samples-only` 写过 .samples-only-<date>.json"""\n    return args.tickers\n')
        assert not cli_options(tree) & set(RETIRED_OPTIONS)
        assert not args_reads(tree) & RETIRED_ARGS
        assert RETIRED_ARTIFACT not in string_constants(tree)

    def test_positive_control_live_cli_is_seen(self):
        tree = _tree("alpha_hive_daily_report.py")
        assert {"--swarm", "--all-watchlist", "--tickers"} <= cli_options(tree)
        assert {"swarm", "all_watchlist", "tickers"} <= args_reads(tree)
