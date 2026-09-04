"""generate_deep_v2._load_ticker_accuracy() 的 direction 词表回归（v0.45.106）。

背景：生产 report_snapshots/ 里的 direction 字段有两套并存的词表——
generate_deep_v2._save_report_snapshot 写 "Long"/"Short"/"Neutral"，
alpha_hive_daily_report 透传 QueenDistiller 的原始 "bullish"/"bearish"/
"neutral"。实测生产库（~/Desktop/Alpha Hive/report_snapshots/，1051 条）
100% 是后者，0 条 "Long"/"Short"。

修复前，`is_win = (s.direction == "Long" and ...) or (s.direction == "Short"
and ...)` 对小写词表恒为 False（win_rate 恒 0%），且 `adj_ret = ret if
s.direction == "Long" else -ret` 对**全部**快照取 else 分支——把每一笔
bullish 交易的真实收益符号取反（不是"归类成中性"，是把盈利算成亏损）。

同一物种的修法见 self_analyst.py 的 classify()（v0.45.85）：direction 统一
小写后同时接受 long/bullish、short/bearish 两套词表；neutral 单独成一类，
不当空头处理（对齐 feedback_loop.BacktestAnalyzer.calculate_accuracy() 里
"neutral: 不计入方向性收益" 的既有口径）。

按项目惯例（见 [[alpha-hive-distribution-guards]]）：每条不变式配一个
「喂退化数据看它红」的用例——这里直接对比修复前后的公式，验证新公式在
小写词表下不再退化。
"""
from pathlib import Path

import pytest


@pytest.fixture()
def snap_dir(tmp_path, monkeypatch):
    """把 ALPHAHIVE_DIR 指向一个没有 pheromone.db 的临时目录，
    这样 clean_t7 查不到干净价，_apply_clean_t7_prices 提前返回，
    不会覆盖 save_to_json 里手写的 actual_price_t7（见
    feedback_loop._load_close_t7_map 的 "missing" 分支）。
    """
    import generate_deep_v2 as g
    monkeypatch.setattr(g, "ALPHAHIVE_DIR", tmp_path)
    out_dir = tmp_path
    d = out_dir / "report_snapshots"
    d.mkdir(parents=True)
    return out_dir, d


def _save(snap_dir: Path, ticker: str, date: str, direction: str,
          entry: float, t7: float) -> None:
    from feedback_loop import ReportSnapshot
    snap = ReportSnapshot(ticker, date)
    snap.direction = direction
    snap.entry_price = entry
    snap.actual_price_t7 = t7
    snap.save_to_json(str(snap_dir))


# (direction, entry, t7, 期望 ret 符号意图)
FIXTURE_ROWS = [
    ("bullish", "2026-08-01", 100.0, 110.0),   # bullish + 涨 → win, adj_ret=+10
    ("bullish", "2026-08-02", 100.0, 90.0),    # bullish + 跌 → loss, adj_ret=-10
    ("bearish", "2026-08-03", 100.0, 90.0),    # bearish + 跌 → win, adj_ret=+10
    ("bearish", "2026-08-04", 100.0, 110.0),   # bearish + 涨 → loss, adj_ret=-10
    ("neutral", "2026-08-05", 100.0, 120.0),   # neutral → 不计入 win/adj_ret
]


class TestLowercaseDirectionVocabulary:
    """生产实际写入的 bullish/bearish/neutral 词表必须被正确识别。"""

    def test_win_rate_not_zeroed_by_lowercase_direction(self, snap_dir):
        out_dir, d = snap_dir
        for direction, date, entry, t7 in FIXTURE_ROWS:
            _save(d, "TESTX", date, direction, entry, t7)

        import generate_deep_v2 as g
        result = g._load_ticker_accuracy("TESTX", out_dir)

        assert result, f"应有结果，实际: {result}"
        # 2 胜（bullish 涨、bearish 跌）/ 5 条 = 40%。
        # 旧 bug 下 "bullish"/"bearish" 从不匹配 "Long"/"Short" 字面量，
        # is_win 恒 False，这里会退化成 0.0。
        assert result["win_rate"] == pytest.approx(40.0), (
            f"win_rate 应为 40.0（2/5），实际: {result}"
        )

    def test_profit_factor_not_polarity_flipped(self, snap_dir):
        """profit_factor 是本 bug 危害最大的下游指标：旧公式把全部
        bullish 交易的 adj_ret 符号取反，gross_profit/gross_loss 全错。
        """
        out_dir, d = snap_dir
        for direction, date, entry, t7 in FIXTURE_ROWS:
            _save(d, "TESTX", date, direction, entry, t7)

        import generate_deep_v2 as g
        result = g._load_ticker_accuracy("TESTX", out_dir)

        # 修复后：gross_profit = 10(bullish win) + 10(bearish win) = 20
        #        gross_loss   = 10(bullish loss) + 10(bearish loss) = 20
        #        neutral 的 +20% 不进入这组数字 → profit_factor = 1.0
        # 旧公式：全部取反 → gross_profit=20(两条巧合方向对的 bearish 分支)，
        #        gross_loss = 10+10+20(neutral 被当空头) = 40 → profit_factor = 0.5
        assert result["profit_factor"] == pytest.approx(1.0), (
            f"profit_factor 应为 1.0，实际: {result} "
            "（若为 0.5 说明退回了符号取反的旧公式）"
        )

    def test_avg_ret_7d_unaffected_by_direction_bug(self, snap_dir):
        """avg_ret_7d 用的是原始（非方向调整）收益，理论上不受此 bug 影响，
        用它做一个交叉检查：确认 5 条快照都被正常加载、没有被异常吞掉。
        """
        out_dir, d = snap_dir
        for direction, date, entry, t7 in FIXTURE_ROWS:
            _save(d, "TESTX", date, direction, entry, t7)

        import generate_deep_v2 as g
        result = g._load_ticker_accuracy("TESTX", out_dir)

        assert result["n"] == 5
        # (10 - 10 - 10 + 10 + 20) / 5 = 4.0
        assert result["avg_ret_7d"] == pytest.approx(4.0)

    def test_bullish_win_adj_ret_matches_raw_return(self, snap_dir):
        """单条 bullish 盈利快照：adj_ret 必须与原始 ret 同号（旧公式会取反）。"""
        out_dir, d = snap_dir
        _save(d, "TESTY", "2026-08-01", "bullish", 100.0, 105.0)
        _save(d, "TESTY", "2026-08-08", "bullish", 100.0, 106.0)  # 凑够 2 条避免除零

        import generate_deep_v2 as g
        result = g._load_ticker_accuracy("TESTY", out_dir)

        assert result["win_rate"] == pytest.approx(100.0)
        # 两条都是 bullish 盈利，profit_factor 应为极大值（无亏损）
        assert result["profit_factor"] == 999.0

    def test_legacy_capitalized_direction_still_works(self, snap_dir):
        """generate_deep_v2._save_report_snapshot 写的 Long/Short/Neutral（大写）
        必须继续被识别——修复不能只认小写词表，破坏另一条写入路径。
        """
        out_dir, d = snap_dir
        _save(d, "TESTZ", "2026-08-01", "Long", 100.0, 110.0)
        _save(d, "TESTZ", "2026-08-08", "Short", 100.0, 90.0)

        import generate_deep_v2 as g
        result = g._load_ticker_accuracy("TESTZ", out_dir)

        assert result["win_rate"] == pytest.approx(100.0)
