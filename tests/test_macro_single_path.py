"""宏观报价全部没取到时不再整份换成兜底常量——三条返回路径合一（v0.45.669）

根因（用户 10-28「照取」）：`fred_macro._fetch_macro_data` 有三条各自拼装的返回路径——报价（财政部 / Finnhub / Yahoo）
一项都没有 ⇒ 提前返回兜底字典（VIX 20.0 elevated / 10Y 4.5 high / 美元·大盘 neutral / 黄金 stable、涨跌 0.0 / 宏观分 5.0 neutral），
CBOE 给了 VIX ⇒ 只把 VIX 换成真值、其余照抄兜底；只靠 `data_source == "fallback"` 告诉读者「别信」，每个读者自己记得查
（Guard v0.43.24 / 日报 v0.45.54 / 仪表板 v0.45.43，各自出过事才补）。连带两处毛病：提前返回时板块轮动（另一次独立的
Yahoo 取数）与 FRED 都不取；部分降级那天宏观分恒 5.0，连那个真 VIX 都没算进去（09-24 / 25：14.21 < 15 是 low，本该 +2 ⇒ 7.0）。

第 0 步只读实测（2026-10-28）：137 份日报 15 份走过兜底（06-22 ~ 08-14 整份常量 13 份；09-24 / 25 只有 CBOE VIX，两天都是
网络故障：SSL 断连 + Yahoo 限流 / DNS 解析失败）；09-26 起 0 份；那两天 `fred_macro` 自己一条 WARNING 都没打（只有仪表板打了）。

本文件守：
  A. 报价全空：观测量全是 None / "unknown"，宏观分只由观测到的分项算（一项都没有 ⇒ None），板块轮动与 FRED 照取，源头 WARNING；
  B. 正常路径上「哪儿都没有 VIX」也是 None（旧 `.get("last", 20.0)`）；
  C. 取数代码自己出错的整份出口：值全缺失 + `error`；
  D. Guard 读这条路径：报价全空那天板块票可能出现（唯一经 Guard 的变化），曲线 / 黄金照旧不记；
  E. 读者逐格显示：日报宏观表照印、缺的格「—」；仪表板显示取到的真 VIX；
  F. 守卫：兜底字典里没有数字、状态只许 "unknown"；`_fetch_macro_data` 的 try 里只有一个 return（不许再长出提前返回）。
"""
import ast
import logging
import pathlib

import pytest

import fred_macro as fm

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOGGER = "alpha_hive.fred_macro"
D = "2026-10-27"
ROTATION = {"hot": [["XLK", "科技", 2.0]], "cold": [["XLE", "能源", -2.0]], "full": {}, "as_of": D, "gaps": {}}


@pytest.fixture(autouse=True)
def rotation_calls(monkeypatch, stub_yfinance, stub_cboe_vix):
    """离线：Yahoo / CBOE 下载钉死（conftest 另钉财政部 + Finnhub 与 FRED key）；锚定日钉 D；板块轮动换成记账的桩。"""
    fm.set_macro_snapshot(None)
    fm._CACHE, fm._CACHE_TS = {}, 0.0
    monkeypatch.setattr(fm, "_realtime_as_of", lambda: D)
    calls = []

    def _rot(yf_module=None, as_of=None):
        calls.append(as_of)
        return {k: (list(v) if isinstance(v, list) else v) for k, v in ROTATION.items()}
    monkeypatch.setattr(fm, "_fetch_sector_rotation", _rot)
    yield calls
    fm.set_macro_snapshot(None)
    fm._CACHE, fm._CACHE_TS = {}, 0.0


def _vix_rows(monkeypatch, rows):
    import cboe_vix
    monkeypatch.setattr(cboe_vix, "get_vix_history",
                        lambda max_days=None, force_refresh=False: rows[-max_days:] if max_days else rows)


def _quotes(monkeypatch, data, src):
    monkeypatch.setattr(fm, "_same_day_macro_data", lambda as_of=None: (dict(data), dict(src)))


def _fred(monkeypatch, values):
    monkeypatch.setattr(fm, "_load_fred_key", lambda: "k")
    monkeypatch.setattr(fm, "_fetch_fred_series", lambda key, vintage=None: dict(values))


MISSING = {"vix": None, "vix_regime": "unknown", "treasury_10y": None, "rate_environment": "unknown",
           "dollar_trend": "unknown", "market_trend": "unknown", "spx_change_pct": None, "gold_price": None,
           "gold_change_pct": None, "gold_trend": "unknown", "yield_curve": "unknown"}


# ══════════════════════════════════════════════════════════════════════════
# A. 报价全空：同一条路径
# ══════════════════════════════════════════════════════════════════════════

class TestNoQuotesTakesTheSamePath:

    def test_nothing_observed_is_missing_not_a_number(self, monkeypatch):
        """旧：VIX 20.0 / 10Y 4.5（恰好 high）/ 涨跌 0.0 / 宏观分 5.0 neutral。变红的变异：恢复提前返回兜底常量。"""
        _vix_rows(monkeypatch, [])
        r = fm.get_macro_context()
        assert {k: r[k] for k in MISSING} == MISSING
        assert (r["macro_score"], r["macro_regime"], r["macro_score_inputs"]) == (None, "unknown", [])
        assert r["macro_score_missing"] == list(fm.MACRO_SCORE_PARTS)
        assert r["data_source"] == "fallback" and r["vix_source"] == "fallback" and "error" not in r

    def test_sector_rotation_and_fred_are_still_fetched(self, monkeypatch, rotation_calls):
        """唯一经 Guard 的变化：板块轮动照取（旧提前返回时不取 ⇒ 那天 Guard 板块票必缺）。FRED 也照取。
        宏观分：六项都没观测到、只有 CPI / HY ⇒ 5.0 − 0.3（HY ≥ 300bp）= 4.7；政体由前六项定 ⇒ unknown。
        变红的变异：恢复提前返回（两项都不取）。"""
        _vix_rows(monkeypatch, [])
        _fred(monkeypatch, {"cpi_yoy": 3.7, "hy_spread_bp": 330.0})
        r = fm.get_macro_context()
        assert rotation_calls == [None] and [h[0] for h in r["sector_rotation"]["hot"]] == ["XLK"]
        assert r["fred_extras"]["cpi_yoy"] == 3.7 and r["cpi_yoy"] == 3.7
        assert (r["macro_score_inputs"], r["macro_score"], r["macro_regime"]) == (["cpi", "hy"], 4.7, "unknown")
        assert r["data_source"] == "fallback"      # 报价一项都没有 ⇒ 仍是这个标签（FRED 供没供上看 fred_extras）

    def test_cboe_vix_counts_in_the_score(self, monkeypatch):
        """09-24 / 25 形状：只有 CBOE 的 VIX 14.21。旧：宏观分 5.0 neutral（照抄兜底、没算进 VIX）；
        现：14.21 < 15 是 low +2 ⇒ 7.0 ⇒ risk_on（`_classify_vix`，不心算）。变红的变异：只有 VIX 时仍走部分降级拷贝。"""
        _vix_rows(monkeypatch, [("2026-10-26", 15.0), ("2026-10-27", 14.21)])
        r = fm.get_macro_context()
        assert (r["vix"], r["vix_regime"], r["vix_source"]) == (14.21, fm._classify_vix(14.21), "cboe") == (14.21, "low", "cboe")
        assert (r["macro_score_inputs"], r["macro_score"], r["macro_regime"]) == (["vix"], 7.0, "risk_on")
        assert r["treasury_10y"] is None and r["rate_environment"] == "unknown"

    def test_no_quotes_warns_at_the_source(self, monkeypatch, caplog):
        """09-24 / 25 那两天 `fred_macro` 自己一条 WARNING 都没打。变红的变异：降成 debug / 删掉。"""
        _vix_rows(monkeypatch, [])
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            r = fm.get_macro_context()
        assert any("一项都没供上" in m and D in m for m in caplog.messages), caplog.messages
        assert r["summary"].startswith("宏观报价全部没取到")

    def test_partial_quotes_do_not_warn_all_missing(self, monkeypatch, caplog):
        _vix_rows(monkeypatch, [])
        _quotes(monkeypatch, {"TNX": {"last": 4.9, "prev": None, "change_pct": None}}, {"TNX": f"treasury_gov@{D}"})
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            r = fm.get_macro_context()
        assert not any("一项都没供上" in m for m in caplog.messages)
        assert r["data_source"] == "treasury" and not r["summary"].startswith("宏观报价全部没取到")


# ══════════════════════════════════════════════════════════════════════════
# B. 正常路径：哪儿都没有 VIX
# ══════════════════════════════════════════════════════════════════════════

class TestNoVixAnywhere:

    def test_vix_is_none_not_20(self, monkeypatch):
        """旧 `data.get("VIX", {}).get("last", 20.0)` + elevated。变红的变异：恢复 20.0。
        宏观分只由 10Y（high −1）算 ⇒ 4.0 ⇒ risk_off（≤ 4）。"""
        _vix_rows(monkeypatch, [])
        _quotes(monkeypatch, {"TNX": {"last": 4.9, "prev": None, "change_pct": None}}, {"TNX": f"treasury_gov@{D}"})
        r = fm.get_macro_context()
        assert (r["vix"], r["vix_regime"], r["vix_source"]) == (None, "unknown", "fallback")
        assert (r["macro_score_inputs"], r["macro_score"], r["macro_regime"]) == (["rate"], 4.0, "risk_off")
        assert r["summary"].startswith("VIX —")


# ══════════════════════════════════════════════════════════════════════════
# C. 取数代码自己出错：唯一的整份出口
# ══════════════════════════════════════════════════════════════════════════

class TestErrorExit:

    def test_error_exit_is_all_missing_and_named(self, monkeypatch, caplog):
        """变红的变异：兜底字典写回常量；不带 `error`；降成 debug。"""
        _vix_rows(monkeypatch, [])
        _quotes(monkeypatch, {"TNX": {"last": 4.9, "prev": None, "change_pct": None}}, {"TNX": f"treasury_gov@{D}"})

        def boom(*_a, **_k):
            raise RuntimeError("boom")
        monkeypatch.setattr(fm, "_compose_data_source", boom)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            r = fm.get_macro_context()
        assert r["error"] == "RuntimeError: boom" and r["data_source"] == "fallback"
        assert {k: r[k] for k in MISSING} == MISSING
        assert (r["macro_score"], r["macro_regime"], r["macro_score_inputs"]) == (None, "unknown", [])
        assert any("boom" in m for m in caplog.messages), caplog.messages


# ══════════════════════════════════════════════════════════════════════════
# D. Guard 读这条路径
# ══════════════════════════════════════════════════════════════════════════

def _guard(monkeypatch, macro, ticker):
    from unittest.mock import patch
    import vix_term_structure
    from swarm_agents.guard_bee import GuardBeeSentinel
    monkeypatch.setattr(vix_term_structure, "get_vix_term_structure", lambda force_refresh=False: {"structure": ""})
    g = GuardBeeSentinel.__new__(GuardBeeSentinel)
    with patch("fred_macro.get_macro_context", return_value=macro):
        return g._calc_macro_adjustment(ticker)


def _ticker_in(etf):
    from config import WATCHLIST
    return next(t for t in WATCHLIST if fm.get_sector_etf_for_ticker(t) == etf)


class TestGuardOnTheSinglePath:

    def test_no_quotes_day_can_now_cast_the_sector_vote(self, monkeypatch):
        """唯一经 Guard 的变化（登边界 10-19 的那一处）：报价全空那天板块轮动照取 ⇒ 板块票可能出现。
        曲线 / 黄金照旧不记（`data_source` 仍是 fallback，且两者这些天必为 unknown）。"""
        _vix_rows(monkeypatch, [])
        macro = fm.get_macro_context()
        r = _guard(monkeypatch, macro, _ticker_in("XLK"))
        det = r["details"]
        assert det["sector_rotation"] == "hot" and r["regime_votes"]["risk_on"] >= 1
        assert "yield_curve" not in det and "gold_trend" not in det and "vix" not in det
        assert det["macro_data_source"] == "fallback"


# ══════════════════════════════════════════════════════════════════════════
# E. 读者逐格显示
# ══════════════════════════════════════════════════════════════════════════

class TestReadersRenderPerField:

    def test_report_prints_the_table_with_dashes(self, monkeypatch):
        """旧：`data_source == "fallback"` ⇒ 整段不印。变红的变异：恢复整段跳过；评分 None 时格式化崩。"""
        import report_formatters as rf
        _vix_rows(monkeypatch, [])
        md = "\n".join(rf._build_macro(fm.get_macro_context()))
        assert "宏观报价一项都没取到" in md and "**宏观政体**：不可得 | **评分**：—" in md
        assert "| VIX | — | 不可得 |" in md and "| 10Y利率 | — | 不可得 |" in md

    def test_report_names_the_error_exit_as_an_error(self, monkeypatch):
        """取数代码自己出错 ≠ 报价没取到（二次检查补）：提示写「取数出错」并带错误。变红的变异：不分两种失败。"""
        import report_formatters as rf
        _vix_rows(monkeypatch, [])
        _quotes(monkeypatch, {"TNX": {"last": 4.9, "prev": None, "change_pct": None}}, {"TNX": f"treasury_gov@{D}"})

        def boom(*_a, **_k):
            raise RuntimeError("boom")
        monkeypatch.setattr(fm, "_compose_data_source", boom)
        md = "\n".join(rf._build_macro(fm.get_macro_context()))
        assert "宏观取数出错（RuntimeError: boom）" in md and "一项都没取到" not in md
        assert "| VIX | — | 不可得 |" in md and "**评分**：—" in md

    def test_report_shows_the_real_cboe_vix(self, monkeypatch):
        import report_formatters as rf
        _vix_rows(monkeypatch, [("2026-10-26", 15.0), ("2026-10-27", 14.21)])
        md = "\n".join(rf._build_macro(fm.get_macro_context()))
        assert "| VIX | 14.2 | low" in md and "**宏观政体**：RISK_ON | **评分**：7.0/10" in md

    def test_dashboard_shows_the_real_cboe_vix(self, monkeypatch, tmp_path):
        """09-24 / 25：仪表板整块「—」，连 CBOE 的真 VIX 也藏了。变红的变异：恢复 `if fallback: … else:` 整块跳过。"""
        from dashboard_renderer import render_dashboard_html
        _vix_rows(monkeypatch, [("2026-10-26", 15.0), ("2026-10-27", 14.21)])
        macro = fm.get_macro_context()
        assert macro["data_source"] == "fallback"
        # v0.45.674：网站读这份日报自己的宏观快照（`report["macro_context"]`），不再调 `get_macro_context()`
        report = {"opportunities": [{"ticker": "NVDA", "direction": "bullish", "score": 7.0, "confidence": 0.6,
                                     "dimension_scores": {}}],
                  "swarm_metadata": {"tickers_analyzed": 1, "total_agents": 7, "resonances_detected": 0},
                  "macro_context": macro}
        html = render_dashboard_html(report=report, date_str=D, report_dir=tmp_path,
                                     opportunities=report["opportunities"])
        assert "<li>VIX 14.2</li>" in html


# ══════════════════════════════════════════════════════════════════════════
# F. 源码守卫
# ══════════════════════════════════════════════════════════════════════════

_SRC = ROOT / "fred_macro.py"
#: 兜底字典里允许是字符串常量、且不是 "unknown" 的键（标签 / 文案，不是观测）
_LABEL_KEYS = {"summary", "data_source", "vix_source", "as_of_mode"}


def _fetch_fn(tree):
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_fetch_macro_data")


def _base_problems(fn):
    """→ 兜底字典里写成数字、或状态不是 "unknown" 的键。"""
    base = next(n.value for n in ast.walk(fn) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "base" for t in n.targets))
    out = []
    for k, v in zip(base.keys, base.values):
        if not isinstance(k, ast.Constant) or not isinstance(v, ast.Constant):
            continue
        if isinstance(v.value, (int, float)) and not isinstance(v.value, bool):
            out.append((k.value, v.value))
        elif isinstance(v.value, str) and k.value not in _LABEL_KEYS and v.value != "unknown":
            out.append((k.value, v.value))
    return out


def _try_returns(fn):
    """→ (try 体里的 return 个数, except 里的 return 个数)；不数嵌套函数（`_set_curve`）里的。"""
    tr = next(n for n in fn.body if isinstance(n, ast.Try))

    def count(stmts):
        n, todo = 0, list(stmts)
        while todo:
            s = todo.pop()
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(s, ast.Return):
                n += 1
            todo.extend(ast.iter_child_nodes(s))
        return n
    return count(tr.body), sum(count(h.body) for h in tr.handlers)


class TestSourceGuards:

    def test_base_has_no_fabricated_values(self):
        assert _base_problems(_fetch_fn(ast.parse(_SRC.read_text(encoding="utf-8")))) == []

    def test_single_return_path(self):
        """try 里只有最后那一个 return；except 里一个（取数出错的整份出口）。
        变红的变异：长回 `if not data: return …` 提前返回。"""
        assert _try_returns(_fetch_fn(ast.parse(_SRC.read_text(encoding="utf-8")))) == (1, 1)

    @pytest.mark.parametrize("snippet,which", [
        ("def _fetch_macro_data():\n    base = {'vix': 20.0, 'summary': 'x'}\n    try:\n        return 1\n"
         "    except Exception:\n        return base\n", "base"),
        ("def _fetch_macro_data():\n    base = {'gold_trend': 'stable'}\n    try:\n        return 1\n"
         "    except Exception:\n        return base\n", "base"),
        ("def _fetch_macro_data():\n    base = {}\n    try:\n        if not d:\n            return base\n        return 1\n"
         "    except Exception:\n        return base\n", "returns"),
    ])
    def test_guard_has_teeth(self, snippet, which):
        """每种旧形状喂进去都要被认出来（正对照：守卫不是恒真）。"""
        fn = _fetch_fn(ast.parse(snippet))
        if which == "base":
            assert _base_problems(fn)
        else:
            assert _try_returns(fn) != (1, 1)
