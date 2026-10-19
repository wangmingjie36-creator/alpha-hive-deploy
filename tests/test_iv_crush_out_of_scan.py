"""墓碑（v0.45.541）：IV-crush 抓取已移出扫描，只有深度报告在缺数据时现场抓。

ChronosBee 曾在每只标的上调 `iv_crush_scraper.get_iv_crush_data`（yfinance 财报日 / 3 年日线 / 期权，不走缓存；
A/B 实测每只 3 次、30 只共 90 次），结果只写进 `details.iv_crush` / `iv_crush_summary` 与财报催化剂上的三个附加键——评分不读，
全仓唯一读者是手动跑的 `generate_deep_v2`。而这些请求全排在 `yf_gate` 那一个 0.5 req/s 的全进程桶里。
重放 A/B（`experiments/iv_crush_ab_v0541.py`，30 只）证明拿掉之后 ChronosBee 的 score / direction / confidence /
data_quality / discovery 与板上条目逐位不变，见 CHANGELOG v0.45.541。

守的三件事：扫描侧谁也不许再 import 它（唯一许可的是 `generate_deep_v2`，双向钉住）；`extract()` 保持纯转换、不打网；
现场抓取三条路径（已有 / 缺了去抓 / 抓失败）各有一条行为测试，失败要打 warning。
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

from tests import _ast_cache
from tests._repo_files import own_python_files

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__
MODULE = "iv_crush_scraper"
#: 唯一许可的生产 importer（双向：多了红，少了也红——没人 import 就说明深度报告的现场抓取被删了）
ALLOWED_IMPORTERS = {"generate_deep_v2.py"}


def _imports(tree) -> list[int]:
    out = []
    for n in ast.walk(tree):
        names = ([a.name for a in n.names] if isinstance(n, ast.Import)
                 else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
        if any(x.split(".")[0] == MODULE for x in names):
            out.append(n.lineno)
    return out


def _production_importers() -> set[str]:
    files, _how = own_python_files(REPO_ROOT)
    found = set()
    for f in files:
        rel = f.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(("tests/", "experiments/")) or rel == f"{MODULE}.py":
            continue
        try:
            tree = _ast_cache.parse_file(f, errors="replace")
        except SyntaxError:
            continue
        if _imports(tree):
            found.add(rel)
    return found


class TestOutOfScan:

    def test_only_deep_report_imports_scraper(self):
        assert _production_importers() == ALLOWED_IMPORTERS

    def test_chronos_writes_no_iv_crush_fields(self):
        tree = _ast_cache.parse_file(REPO_ROOT / "swarm_agents" / "chronos_bee.py")
        keys = {k.value for n in ast.walk(tree) if isinstance(n, ast.Dict)
                for k in n.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        subs = {n.slice.value for n in ast.walk(tree)
                if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store)
                and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str)}
        leaked = (keys | subs) & {"iv_crush", "iv_crush_summary", "iv_crush_avg_move", "implied_move", "beat_rate"}
        assert not leaked, leaked
        # 正对照：同一探测法认得出 ChronosBee 仍在写的 details 键
        assert "catalysts" in keys


class TestDeepReportFetchesOnDemand:

    @staticmethod
    def _stub(monkeypatch, fn):
        import iv_crush_scraper
        monkeypatch.setattr(iv_crush_scraper, "get_iv_crush_data", fn)

    def test_extract_stays_offline(self, monkeypatch):
        import generate_deep_v2 as g
        calls = []
        self._stub(monkeypatch, lambda t, **k: calls.append(t) or {})
        ctx = g.extract({"ticker": "SYN", "timestamp": "2026-10-19", "swarm_results": {}})
        assert calls == [] and ctx["iv_crush"] == {}

    def test_existing_record_is_used_without_fetch(self, monkeypatch):
        import generate_deep_v2 as g
        calls = []
        self._stub(monkeypatch, lambda t, **k: calls.append(t) or {})
        ctx = {"ticker": "SYN", "iv_crush": {"avg_abs_move": 5.0}, "iv_crush_summary": "旧记录"}
        g._try_fetch_iv_crush(ctx)
        assert calls == [] and ctx["iv_crush_summary"] == "旧记录"

    def test_missing_is_fetched(self, monkeypatch):
        import generate_deep_v2 as g
        calls = []
        self._stub(monkeypatch, lambda t, **k: calls.append(t) or {
            "ticker": t, "historical_moves": [6.2, -7.1, 4.0], "avg_abs_move": 6.2,
            "up_count": 5, "down_count": 3, "avg_up_move": 5.0, "avg_down_move": -7.1})
        ctx = {"ticker": "SYN", "iv_crush": {}, "iv_crush_summary": ""}
        g._try_fetch_iv_crush(ctx)
        assert calls == ["SYN"] and ctx["iv_crush"]["avg_abs_move"] == 6.2 and ctx["iv_crush_summary"]

    def test_fetch_failure_is_loud_not_fatal(self, monkeypatch, caplog):
        import generate_deep_v2 as g

        def boom(t, **k):
            raise ConnectionError("Too Many Requests")
        self._stub(monkeypatch, boom)
        ctx = {"ticker": "SYN", "iv_crush": {}, "iv_crush_summary": ""}
        with caplog.at_level(logging.WARNING):
            g._try_fetch_iv_crush(ctx)
        assert ctx["iv_crush"] == {} and ctx["iv_crush_summary"] == ""
        assert "IV Crush 现场抓取失败" in caplog.text and "SYN" in caplog.text

    def test_empty_fetch_is_loud(self, monkeypatch, caplog):
        import generate_deep_v2 as g
        self._stub(monkeypatch, lambda t, **k: {"ticker": t, "avg_abs_move": None, "source": "yfinance:no_earnings_dates"})
        ctx = {"ticker": "SYN", "iv_crush": {}, "iv_crush_summary": ""}
        with caplog.at_level(logging.WARNING):
            g._try_fetch_iv_crush(ctx)
        assert "无可用数据" in caplog.text and "no_earnings_dates" in caplog.text


    def test_summary_format_failure_keeps_panel_data(self, monkeypatch, caplog):
        """格式化抛了 ⇒ 面板数据照留、摘要留空、打 warning（变红的变异：把格式化挪回同一个 try）。"""
        import generate_deep_v2 as g
        self._stub(monkeypatch, lambda t, **k: {"ticker": t, "avg_abs_move": 6.2,
                                                "historical_moves": [{"bad": "shape"}]})
        ctx = {"ticker": "SYN", "iv_crush": {}, "iv_crush_summary": ""}
        with caplog.at_level(logging.WARNING):
            g._try_fetch_iv_crush(ctx)
        assert ctx["iv_crush"]["avg_abs_move"] == 6.2 and ctx["iv_crush_summary"] == ""
        assert "摘要格式化失败" in caplog.text


class TestDetectorHasTeeth:

    def test_import_detector(self):
        assert _imports(ast.parse("from iv_crush_scraper import get_iv_crush_data\n"))
        assert _imports(ast.parse("import iv_crush_scraper as s\n"))
        assert not _imports(ast.parse("import pead_analyzer\n"))
