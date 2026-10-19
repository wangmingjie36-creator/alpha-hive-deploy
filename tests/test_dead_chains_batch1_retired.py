"""墓碑（v0.45.534）：死链清理第一批——别让它们悄悄回来。

2026-10-19 二次检查全仓「算了 / 写了却没人读」的链（三路只读审计 + 主会话复核，正对照见 CHANGELOG v0.45.534）。
本批只收**不碰评分**的五项：

1. **编排器 Step 8** `metrics_collector.py --record --status-json`：模块没有 `__main__` / argparse，每天跑 0 秒什么都不做，
   status.json 却记 `step8_metrics_collection: success` —— 假成功。
2. **metrics.db 链**（`MetricsCollector` 的 `scan_metrics` / `ticker_metrics` / `slo_violations`）：后两张表零读者；
   `scan_metrics` 只喂 30s SLO 检查（实际 P95 ≈ 1917s ⇒ 天天「违规」）的一行日志；`get_summary` / `get_ticker_history` /
   `cleanup` 零调用者 ⇒ 从 2026-02 起无界增长。扫描耗时另有 `scan_timing` 落盘。
3. **`reasoning_sessions`**（`MemoryStore.save_session`）：一个 SELECT 都没有，只有 180 天清理的 DELETE。
4. **`sentiment_baseline.db`**（`_upsert_sentiment` / `_get_sentiment_baseline` / `_check_sentiment_spike`）：v0.45.340 起
   情绪动量改读 signal_archive，它只剩一行 WARNING 日志 + 零读者的 `sentinel_spike` 字段；且任何 Buzz 运行都会写它。
   位于 BuzzBee 分数算完之后，**不改 score / direction / details**。
5. **逐标的 `sector_sentiment`**：在 .swarm_results / 快照 / 档案落盘之后才写进 swarm_results，生产里从未被保存。
   板块均值汇总 `sector_sentiment_contagion` 已发布在 gh-pages 日报 JSON，**保留**。

生产数据根里的旧 `metrics.db` / `sentiment_baseline.db` / `reasoning_sessions` 表原样留着（删 = 改生产数据，未做；备份照旧）。
`barrier_outcomes`（代码注释写明「只写不读、待攒样本」）本批**刻意不动**。
"""

from __future__ import annotations

import ast
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from tests import _ast_cache
from tests._orchestrator import repo_orchestrator_text
from tests._repo_files import own_python_files

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__
_SELF = Path(__file__).resolve()

RETIRED_MODULES = {"metrics_collector"}
RETIRED_TABLES = ("scan_metrics", "ticker_metrics", "slo_violations", "reasoning_sessions", "sentiment_baseline")
RETIRED_CALLS = {
    "record_scan", "record_ticker", "check_slo",          # MetricsCollector
    "save_session",                                       # reasoning_sessions
    "_upsert_sentiment", "_get_sentiment_baseline", "_check_sentiment_spike", "_init_sentiment_db",
}
_SQL_TABLE = re.compile(
    r"(?i)\b(from|into|update|join|table(\s+if\s+not\s+exists)?)\s+[\"`]?(" + "|".join(RETIRED_TABLES) + r")\b")


def _imports_retired(tree):
    out = []
    for n in ast.walk(tree):
        names = ([a.name for a in n.names] if isinstance(n, ast.Import)
                 else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
        if any(x.split(".")[0] in RETIRED_MODULES for x in names):
            out.append(n.lineno)
        # optional_import("metrics_collector", ...) 这种字符串导入也算
        if isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant) \
                and n.args[0].value in RETIRED_MODULES:
            out.append(n.lineno)
    return out


def _retired_calls(tree):
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            if name in RETIRED_CALLS:
                out.append(n.lineno)
        # 当回调传走（`_submit_bg(self.memory_store.save_session, ...)`）也算
        if isinstance(n, ast.Attribute) and n.attr in RETIRED_CALLS and isinstance(n.ctx, ast.Load):
            out.append(n.lineno)
    return sorted(set(out))


def _sql_on_tables(tree):
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and _SQL_TABLE.search(n.value)]


def _writes_key(tree, key):
    """`x[key] = ...` / 字典字面量里的 key —— 认结构，不认子串。"""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value == key:
                    out.append(n.lineno)
        if isinstance(n, ast.Dict):
            if any(isinstance(k, ast.Constant) and k.value == key for k in n.keys):
                out.append(n.lineno)
    return out


def _scan(detector, *, include_tests=False):
    files, _how = own_python_files(REPO_ROOT)
    offenders = []
    for f in files:
        rel = f.relative_to(REPO_ROOT).as_posix()
        if f.resolve() == _SELF or (not include_tests and rel.startswith("tests/")):
            continue
        if rel.startswith("experiments/"):
            continue   # 历史一次性实验脚本读过旧生产表（如 10-12 重放），不是生产链
        try:
            tree = _ast_cache.parse_file(f, errors="replace")
        except SyntaxError:
            continue
        offenders += [f"{rel}:{ln}" for ln in detector(tree)]
    return offenders


class TestBatch1StaysRetired:

    def test_metrics_collector_module_is_gone(self):
        assert not (REPO_ROOT / "metrics_collector.py").exists()

    def test_nothing_imports_retired_modules(self):
        assert not _scan(_imports_retired, include_tests=True)

    def test_no_calls_to_retired_functions(self):
        assert not _scan(_retired_calls)

    def test_no_sql_on_retired_tables(self):
        """读也拦：复活的第一步往往是「先 SELECT 出来看看」。"""
        assert not _scan(_sql_on_tables)

    def test_buzz_no_longer_emits_sentinel_spike(self):
        assert not _scan(lambda t: _writes_key(t, "sentinel_spike"))

    def test_per_ticker_sector_sentiment_not_written(self):
        src = REPO_ROOT / "alpha_hive_daily_report.py"
        assert not _writes_key(_ast_cache.parse_file(src), "sector_sentiment")
        # 正对照：保留的板块汇总仍在日报里（gh-pages 已发布的字段）
        assert _writes_key(_ast_cache.parse_file(src), "sector_sentiment_contagion")

    def test_orchestrator_has_no_step8(self):
        orch = repo_orchestrator_text()
        assert "metrics_collector.py" not in [ln.strip().split('"')[1].rsplit("/", 1)[-1]
                                              for ln in orch.splitlines()
                                              if ln.strip().startswith("run_step") and '"' in ln]
        assert not re.search(r"(?m)^[^#\n]*step8_metrics_collection", orch), "step8 结局键又写回 status.json 了"
        # 正对照：同一解析方式认得出仍在跑的步骤
        assert any("data_fetcher.py" in ln for ln in orch.splitlines() if ln.strip().startswith("run_step"))


class TestMemoryStoreAfterRetirement:

    def test_fresh_store_has_no_reasoning_sessions(self, memory_store):
        conn = sqlite3.connect(memory_store.db_path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        assert "reasoning_sessions" not in tables
        assert "agent_memory" in tables
        assert not hasattr(memory_store, "save_session")

    def test_cleanup_still_deletes_old_memory_on_fresh_db(self, memory_store):
        """陷阱回归：只删建表、不删 cleanup 里那句 `DELETE FROM reasoning_sessions` ⇒ 新库上抛
        `no such table`，被 except 吞掉 ⇒ 同一事务里 agent_memory 的清理也**静默不生效**。"""
        old = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
        new = datetime.now().strftime("%Y-%m-%d")
        for d in (old, new):
            assert memory_store.save_agent_memory(
                {"date": d, "ticker": "NVDA", "agent_id": "ScoutBeeNova", "direction": "bullish",
                 "discovery": "x", "source": "x", "self_score": 7.0}, "s")
        assert memory_store.cleanup_old_data(180) == 1
        left = [m["date"] for m in memory_store.get_recent_memories("NVDA", days=10_000)]
        assert left == [new]


class TestDetectorsHaveTeeth:
    """正对照：每个探测器喂一段旧代码的真实形状，必须报出来。"""

    def test_import_detector(self):
        assert _imports_retired(ast.parse('MetricsCollector = optional_import("metrics_collector", "MetricsCollector")\n'))
        assert _imports_retired(ast.parse("from metrics_collector import MetricsCollector\n"))
        assert not _imports_retired(ast.parse('Backtester = optional_import("backtester", "Backtester")\n'))

    def test_call_detector(self):
        assert _retired_calls(ast.parse("self.metrics.record_scan(ticker_count=1)\n"))
        assert _retired_calls(ast.parse("self._submit_bg(self.memory_store.save_session, sid)\n"))
        assert _retired_calls(ast.parse("_upsert_sentiment(t, d, p)\n_spike = _check_sentiment_spike(t, p, d)\n"))
        assert not _retired_calls(ast.parse("ms.save_agent_memory(e, sid)\n"))

    def test_sql_detector(self):
        assert _sql_on_tables(ast.parse('"INSERT OR REPLACE INTO reasoning_sessions (session_id) VALUES (?)"\n'))
        assert _sql_on_tables(ast.parse('"SELECT AVG(sentiment_pct) FROM sentiment_baseline WHERE ticker=?"\n'))
        assert _sql_on_tables(ast.parse('"CREATE TABLE IF NOT EXISTS ticker_metrics (id INTEGER)"\n'))
        assert not _sql_on_tables(ast.parse('"SELECT * FROM signal_archive"\n'))
        assert not _sql_on_tables(ast.parse('"sentiment_baseline.db 已退役"\n'))

    def test_key_detector(self):
        assert _writes_key(ast.parse('_sd["sector_sentiment"] = {}\n'), "sector_sentiment")
        assert _writes_key(ast.parse('AgentResult(extras={"sentinel_spike": m})\n'), "sentinel_spike")
        assert not _writes_key(ast.parse('x = d.get("sentinel_spike")\n'), "sentinel_spike")
