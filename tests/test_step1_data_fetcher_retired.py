"""墓碑（v0.45.546）：编排器 Step 1（data_fetcher）已删，它的两个读者改了源。

Step 1 每天给写死的 NVDA / VKTX / TSLA 三只抓 Yahoo / SEC / Google Trends / SeekingAlpha 指标，写 `realtime_metrics.json`
与 `cache/{social,yahoo,gtrends,sec_form4,seekingalpha}#<t>.json`、`cache/metrics_<T>_<date>.json`。蜂群一个都不读
（全仓按文件名 + 对 cache 目录的 glob 逐一核过，见 CHANGELOG v0.45.546）。二次检查审计判它「近乎死链」，**漏了一个活读者**：
Slack 日报（Step 7 `push_report_to_slack` → `_format_rich_daily_mrkdwn`）的价格兜底与「(5d ±x%)」只从
`metrics_<T>_<date>.json` 来 ⇒ 只有那三只有。现在改读扫描自己的 `.swarm_results`（Scout `price` / Buzz `momentum_5d`，
30 只全有；10-16 实测三只与 Step 1 的值逐位相等）。另一个读者 `generate_ml_report.main()`（Step 3 兜底）不再读 realtime_metrics.json——
写它的步骤没了，留在数据根里的旧文件读一次就是一份过期价格。

v0.45.571：那个 Slack 读者连同 Step 7 / `push_report_to_slack.py` / `_format_rich_daily_mrkdwn` 一起删了
（它只在手动 `--force` 时跑，生产从未调到）⇒ 原来钉「Slack 改读 .swarm_results」的 `TestSlackReadsScanResults` 随之删除；
「没人再读 realtime_metrics.json / 没人再拼 metrics_<T>_<date>.json」两条全仓扫描继续守。
"""
from __future__ import annotations

import ast
from pathlib import Path

from tests import _ast_cache
from tests._orchestrator import repo_orchestrator_text
from tests._repo_files import own_python_files

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__
#: 只是在**备份 / 迁移清单**里列着这个旧文件名（数据根里那份还在、照常备份），不是读者
BACKUP_LISTS = {"data_backup/export.py", "data_backup/migrate_data_root.py"}


def _mentions_realtime_metrics(tree) -> list[int]:
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and "realtime_metrics.json" in n.value]


def _builds_metrics_cache_name(tree) -> list[int]:
    """f"metrics_{ticker}_{date}.json" 这种拼法（data_fetcher / Slack 当年就是这么对上暗号的）。"""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr) and n.values and isinstance(n.values[0], ast.Constant) \
                and str(n.values[0].value).startswith("metrics_"):
            out.append(n.lineno)
    return out


def _scan(detector, skip=()):
    files, _how = own_python_files(REPO_ROOT)
    hits = []
    for f in files:
        rel = f.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(("tests/", "experiments/")) or rel in skip:
            continue
        try:
            tree = _ast_cache.parse_file(f, errors="replace")
        except SyntaxError:
            continue
        hits += [f"{rel}:{ln}" for ln in detector(tree)]
    return hits


class TestStep1StaysRetired:

    def test_module_is_gone(self):
        assert not (REPO_ROOT / "data_fetcher.py").exists()

    def test_orchestrator_has_no_step1(self):
        orch = repo_orchestrator_text()
        run_lines = [ln for ln in orch.splitlines() if ln.strip().startswith("run_step")]
        assert not any("data_fetcher.py" in ln for ln in run_lines)
        assert "step1_data_fetcher" not in "\n".join(ln for ln in orch.splitlines() if not ln.lstrip().startswith("#"))
        assert any("alert_manager.py" in ln for ln in run_lines)   # 正对照：同一解析认得出仍在跑的步骤

    def test_pipeline_start_is_defined_before_use(self):
        """删 Step 1 时差点把 `STEP1_START=$(date +%s)` 一起删了：编排器 `set -u`，write_status 与 TOTAL_DURATION 都减它 ⇒
        扫描后写状态时 "unbound variable" 整个退出。（当时是 test_scan_catchup 拿它当位置锚点碰巧红了才发现。）"""
        import re
        orch = repo_orchestrator_text()
        assert re.search(r"(?m)^\s*set -[a-z]*u", orch), "前提变了：编排器不再 set -u（本条判据要重想）"
        code = [ln for ln in orch.splitlines() if not ln.lstrip().startswith("#")]
        assign = [i for i, ln in enumerate(code) if re.match(r"\s*STEP1_START=", ln)]
        uses = [i for i, ln in enumerate(code) if "STEP1_START" in ln and not re.match(r"\s*STEP1_START=", ln)]
        assert len(assign) == 1, f"STEP1_START 赋值应恰好一处，实际 {len(assign)}"
        assert uses, "正对照：应能找到用它的地方（write_status / TOTAL_DURATION）"
        assert assign[0] < min(uses), "STEP1_START 必须在第一次使用之前赋值"

    def test_nobody_reads_realtime_metrics(self):
        assert not _scan(_mentions_realtime_metrics, skip=BACKUP_LISTS)

    def test_nobody_builds_metrics_cache_name(self):
        assert not _scan(_builds_metrics_cache_name)


class TestDetectorsHaveTeeth:

    def test_realtime_detector(self):
        assert _mentions_realtime_metrics(ast.parse('open("realtime_metrics.json")\n'))
        assert not _mentions_realtime_metrics(ast.parse('open("fear_greed.json")\n'))

    def test_metrics_name_detector(self):
        assert _builds_metrics_cache_name(ast.parse('p = f"metrics_{ticker}_{date_str}.json"\n'))
        assert not _builds_metrics_cache_name(ast.parse('p = f"analysis-{ticker}-ml-{d}.json"\n'))
