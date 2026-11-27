"""v0.45.819：不按行权价过滤的近端 P/C（≤45 天全部到期日、OI 直接相加、不含当天到期）——只展示，不进评分。

为什么有它：日报原有的两个 P/C 都随现价划行权价窗口（评分用 `put_call_ratio` 0.3～1.7×、全链 P/C 0.6～1.45×），
暴跌暴涨时 OI 一张不动读数也会跳（v0.45.798 实测 NVDA 1.21 → 1.70）。这个读数不划窗口。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

import cboe_options as CO
import dashboard_renderer as DR

ROOT = Path(__file__).resolve().parents[1]
BASE = date(2026, 11, 24)


def _sym(root: str, expiry: str, cp: str, strike: float) -> str:
    y, m, d = expiry.split("-")
    return f"{root}{y[2:]}{m}{d}{cp}{int(round(strike * 1000)):08d}"


def _opt(expiry: str, cp: str, strike: float, oi, root: str = "NVDA") -> dict:
    return {"option": _sym(root, expiry, cp, strike), "open_interest": oi}


# ═══════════════════════════════ 纯函数 ═══════════════════════════════

class TestPureFunction:
    def test_far_strikes_count_and_oi_is_summed_unweighted(self):
        """行权价不过滤：现价约 180 时 5 与 900 的合约照样算；两个到期日的 OI 直接相加。"""
        opts = [
            _opt("2026-11-28", "C", 180, 100), _opt("2026-11-28", "P", 180, 50),
            _opt("2026-11-28", "P", 5, 30),          # 远低于 0.3× 的遗留合约
            _opt("2026-12-19", "C", 900, 100),       # 远高于 1.7×
            _opt("2026-12-19", "P", 170, 120),
        ]
        r = CO.pc_near_unfiltered(opts, BASE)
        assert r["put_oi"] == 200 and r["call_oi"] == 200
        assert r["ratio"] == 1.0 and r["expiries"] == 2
        assert r["window_days"] == 45 and r["base_date"] == "2026-11-24" and r["source"] == "cboe"

    def test_window_is_1_to_45_calendar_days_from_the_data_date(self):
        """当天到期的不算（扫描在收盘后，那批合约已经没了）；第 45 天算、第 46 天不算。"""
        opts = [
            _opt("2026-11-24", "P", 180, 10_000),    # DTE 0
            _opt("2026-11-25", "C", 180, 100),       # DTE 1
            _opt("2027-01-08", "P", 180, 40),        # DTE 45
            _opt("2027-01-09", "P", 180, 9_999),     # DTE 46
            _opt("2026-11-20", "P", 180, 9_999),     # 已过期
        ]
        r = CO.pc_near_unfiltered(opts, BASE)
        assert (r["put_oi"], r["call_oi"], r["expiries"]) == (40, 100, 2)

    def test_no_call_oi_or_no_expiry_in_window_is_none_not_neutral(self):
        assert CO.pc_near_unfiltered([_opt("2026-11-28", "P", 180, 10)], BASE) is None
        assert CO.pc_near_unfiltered([_opt("2027-06-18", "C", 180, 10)], BASE) is None
        assert CO.pc_near_unfiltered([], BASE) is None

    def test_bad_rows_and_non_standard_roots_are_skipped(self):
        """坏 OI / 坏符号跳过；带数字的 root（CVX1 调整合约、B1 二元期权）同 `_parse_occ` 一样不认。"""
        opts = [
            _opt("2026-11-28", "C", 180, 100), _opt("2026-11-28", "P", 180, 20),
            _opt("2026-11-28", "P", 180, "abc"), _opt("2026-11-28", "P", 180, None),
            _opt("2026-11-28", "P", 180, 500, root="CVX1"),
            {"option": "garbage", "open_interest": 7},
        ]
        r = CO.pc_near_unfiltered(opts, BASE)
        assert (r["put_oi"], r["call_oi"]) == (20, 100)
        assert r["bad_oi"] == 1, "读不成数的 OI 要计数，不无声跳过（None 是「没有」，不计）"

    def test_nan_and_inf_oi_are_counted_not_raised(self):
        """inf 曾会抛 OverflowError、整只票记 None（v0.45.820）。"""
        opts = [_opt("2026-11-28", "C", 180, 100), _opt("2026-11-28", "P", 180, 30),
                _opt("2026-11-28", "P", 180, float("nan")), _opt("2026-11-28", "P", 180, float("inf"))]
        r = CO.pc_near_unfiltered(opts, BASE)
        assert (r["put_oi"], r["bad_oi"], r["ratio"]) == (30, 2, 0.3)

    def test_price_does_not_enter(self):
        """要治的正是「现价一动读数就跳」——函数签名里根本没有现价。"""
        import inspect
        assert "price" not in " ".join(inspect.signature(CO.pc_near_unfiltered).parameters)


class TestFetch:
    def test_base_date_is_the_payload_trade_date(self, monkeypatch):
        """日期基准取 payload 自己的成交日期：11-24 的数据，11-25 才到期的算 DTE 1。"""
        payload = {"last_trade_time": "2026-11-24T16:00:00",
                   "options": [_opt("2026-11-24", "P", 180, 999), _opt("2026-11-25", "C", 180, 100),
                               _opt("2026-11-25", "P", 180, 70)]}
        monkeypatch.setattr(CO, "_SNAPSHOT_PROVIDER", None)
        monkeypatch.setattr(CO, "_fetch_cboe_payload", lambda t, timeout: payload)
        r = CO.fetch_cboe_pc_near_unfiltered("NVDA")
        assert r["base_date"] == "2026-11-24" and r["ratio"] == 0.7

    def test_unavailable_or_stale_payload_is_none(self, monkeypatch):
        """`_fetch_cboe_payload` 对失败与陈旧文件都返回 None ⇒ 这里也是 None，不编一个数。"""
        monkeypatch.setattr(CO, "_SNAPSHOT_PROVIDER", None)
        monkeypatch.setattr(CO, "_fetch_cboe_payload", lambda t, timeout: None)
        assert CO.fetch_cboe_pc_near_unfiltered("NVDA") is None

    def test_analyzer_only_computes_it_for_cboe_chains(self):
        """只在主链来自 CBOE 时算（payload 已在进程缓存里，零额外请求）；yfinance / 样本链 ⇒ None。"""
        src = (ROOT / "options_analyzer.py").read_text(encoding="utf-8")
        block = src.split("pc_near_unfiltered = None", 1)[1].split("# P1-⑤", 1)[0]
        assert 'options_chain.get("_source") == "cboe"' in block
        assert '"pc_near_unfiltered": pc_near_unfiltered,' in src


# ═══════════════════════════════ 只展示 ═══════════════════════════════

def _prod_py():
    """git 跟踪的本仓 .py（`tests/_repo_files.own_python_files`，唯一实现）去掉测试与实验。

    v0.45.820：原先裸 `ROOT.rglob`——开发检出里 iCloud 造的 `cboe_options 2.py` 一类副本、未跟踪的旧目录
    都会被算成「读它的生产文件」，守卫在开发检出里无故变红、在 worktree / CI 里是绿的。
    """
    from tests._repo_files import own_python_files
    files, _how = own_python_files(ROOT)
    for p in files:
        rel = p.relative_to(ROOT)
        if {"tests", "experiments", "_archive"} & set(rel.parts):
            continue
        yield rel.as_posix(), p


class TestDisplayOnly:
    def test_only_producer_and_renderer_refer_to_it(self):
        """评分冻结期内它不许进 `final_score` 上游：除了算它的两处和日报渲染，谁都不许按名字读它。
        变红的变异：哪只蜂 / 蒸馏 / 归档把它接进了评分或存档。"""
        pat = re.compile(r"\bpc_near_unfiltered\b|\bfetch_cboe_pc_near_unfiltered\b")
        users = {rel for rel, p in _prod_py() if pat.search(p.read_text(encoding="utf-8"))}
        assert users == {"cboe_options.py", "options_analyzer.py", "dashboard_renderer.py"}, users

    def test_scoring_inputs_unchanged(self):
        """评分仍只读 `put_call_ratio`，计算函数没被换成新口径。"""
        src = (ROOT / "options_analyzer.py").read_text(encoding="utf-8")
        assert "put_call_ratio = self.analyzer.calculate_put_call_ratio(calls_df, puts_df)" in src


# ═══════════════════════════════ 日报渲染 ═══════════════════════════════

class TestRender:
    GOOD = {"ratio": 0.736, "put_oi": 736, "call_oi": 1000, "expiries": 11,
            "window_days": 45, "base_date": "2026-11-24", "source": "cboe"}

    def test_good_value_is_shown_with_its_scope(self):
        d = DR._pc_near_unfiltered({"pc_near_unfiltered": self.GOOD})
        html = DR._pc_near_unfiltered_html(d)
        assert "不过滤行权价 0.74" in html and "≤45天 · 11 个到期日" in html and "不进评分" in html

    def test_old_record_without_the_key_renders_nothing(self):
        assert DR._pc_near_unfiltered({"put_call_ratio": 0.7}) is None
        assert DR._pc_near_unfiltered(None) is None
        assert DR._pc_near_unfiltered_html(None) == ""

    @pytest.mark.parametrize("bad", [{}, "0.7", {"ratio": None}, {"ratio": float("nan")},
                                     {"ratio": float("inf")}, {"ratio": -1}, {"ratio": True}])
    def test_bad_value_shows_a_dash_not_a_number(self, bad):
        """值坏了 ⇒ 写「—（读数异常）」，不拿假数顶，也不悄悄消失。"""
        d = DR._pc_near_unfiltered({"pc_near_unfiltered": bad, "chain_source": "cboe"})
        assert d == {"why": "读数异常"}
        html = DR._pc_near_unfiltered_html(d)
        assert "不过滤行权价 —（读数异常）" in html and not re.search(r"\d\.\d\d", html)

    def test_none_says_why(self):
        """11-25 生产 30 只里 18 只走 yfinance 链 ⇒ 页面要写明「没算」，与「CBOE 上算不出」分开（v0.45.820）。"""
        yf = DR._pc_near_unfiltered({"pc_near_unfiltered": None, "chain_source": "yfinance"})
        assert "yfinance" in DR._pc_near_unfiltered_html(yf)
        cb = DR._pc_near_unfiltered({"pc_near_unfiltered": None, "chain_source": "cboe"})
        assert "CBOE 链上算不出" in DR._pc_near_unfiltered_html(cb)

    def test_card_template_calls_the_helper(self):
        src = (ROOT / "dashboard_renderer.py").read_text(encoding="utf-8")
        assert '{_pc_near_unfiltered_html(_detd.get("pc_near_unfiltered"))}' in src
        assert '"pc_near_unfiltered": _pc_near_unfiltered(oracle),' in src
