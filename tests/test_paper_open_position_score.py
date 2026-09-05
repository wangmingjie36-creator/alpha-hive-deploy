"""开仓时拿不到分数必须不开仓，不能把 None 记成 0.0 分（v0.45.111 回归）

背景
----
`_open_position` 曾写 `score=_snapshot_score(snapshot) or 0.0`，注释辩解说
「上游 `_should_open` 已保证分数存在，这里只是类型收口」。两点都站不住：

1. **它是个会落盘的伪造值。** `Position.score` 经 `_close_position` 传进
   `ClosedTrade`，写进 `closed_trades.jsonl`，再被 `ibkr_sync`（导出给用户下单的
   actions）、`alpha_hive_mcp`、`chart_engine` 读走。缺分记成 0.0 不是收口，
   是往账本里写一个从没发生过的分数——而 0.0 在这套量表里恰好是**最强看空**，
   是所有可能的谎话里最糟的一个。
2. **「上游保证过」不是不检查的理由**，正是 v0.45.3 判据的反面教材：
   问「这个默认值会不会让下游误以为掌握了信息」——会。

修法取本函数已有的降级惯例（`size_usd` / `entry_price` 两道守卫同款）：
非法输入 → `return None`，调用方 `run_for_date` 跳过该候选。

⚠️ 判别力全在**成对**断言上
------------------------
只断言「缺分要拒绝」是不够的：一个偷懒的 `if not score: return None` 同样能让
那半边全绿，但它会把**真实的 0.0 分**也一起拒掉（0.0 是合法分数，在这套量表里
是最强看空信号）。所以必须同时断言：

    缺分 / None / NaN / ±inf / 非数  → 不开仓
    真实的 0.0                      → **照常开仓，且 Position.score 就是 0.0**

同 v0.45.96 记的「缓存/分桶类断言必须成对」——少一半，键换成常量照样全绿。
"""

import math

import pytest

import paper_portfolio as pp

NAV = 50_000.0
AS_OF = "2026-08-31"

DEGENERATE = [
    pytest.param({}, id="缺键"),
    pytest.param({"composite_score": None}, id="None"),
    pytest.param({"composite_score": float("nan")}, id="NaN"),
    pytest.param({"composite_score": float("inf")}, id="+inf"),
    pytest.param({"composite_score": float("-inf")}, id="-inf"),
    pytest.param({"composite_score": "N/A"}, id="非数字符串"),
]


@pytest.fixture(autouse=True)
def _fixed_size(monkeypatch):
    """把仓位计算钉死，让这组测试只测分数这一条路径"""
    monkeypatch.setattr(pp, "_compute_position_size", lambda *a, **k: (1000.0, "tier"))


def _snap(direction="bullish", **kw):
    d = {"ticker": "TSLA", "direction": direction, "entry_price": 367.95}
    d.update(kw)
    return d


class TestDegenerateScoreYieldsNoPosition:
    @pytest.mark.parametrize("extra", DEGENERATE)
    def test_no_position(self, extra):
        assert pp._open_position(_snap(**extra), NAV, AS_OF, {}, []) is None

    @pytest.mark.parametrize("extra", DEGENERATE)
    def test_never_fabricates_zero_score(self, extra):
        """核心断言：宁可不开仓，也不能开出一个 score=0.0 的仓。
        `or 0.0` 下这里拿到的是 Position(score=0.0)。"""
        got = pp._open_position(_snap(**extra), NAV, AS_OF, {}, [])
        assert not (got is not None and got.score == 0.0), (
            f"{extra} 开出了 score=0.0 的仓位——这是个会写进 closed_trades.jsonl "
            "的伪造分数，且 0.0 在本量表里是最强看空"
        )


class TestRealScoresStillOpen:
    """成对的另一半。少了这组，`if not score: return None` 也能全绿。"""

    @pytest.mark.parametrize("raw", [0.0, 3.78, 4.85, 6.5, 7.8, 8.74])
    def test_finite_score_opens_and_is_recorded_verbatim(self, raw):
        direction = "bearish" if raw <= 5 else "bullish"
        got = pp._open_position(_snap(direction, composite_score=raw), NAV, AS_OF, {}, [])
        assert got is not None, f"合法分数 {raw} 被拒了"
        assert got.score == pytest.approx(raw)
        assert math.isfinite(got.score)

    def test_legit_zero_is_not_treated_as_missing(self):
        """0.0 是合法分数不是哨兵——这条专门挡 `if not score` 式的偷懒修法。
        （生产 1051 条快照里 composite_score 为 0 的有 0 条，此分支不触发，
        但它是本组测试唯一能把「拒 None」和「拒 falsy」分开的断言。）"""
        got = pp._open_position(_snap("bearish", composite_score=0.0), NAV, AS_OF, {}, [])
        assert got is not None and got.score == 0.0


class TestRationaleShowsRealScore:
    def test_rationale_carries_the_score(self):
        got = pp._open_position(_snap(composite_score=7.8), NAV, AS_OF, {}, [])
        assert "score=7.8" in got.rationale

    def test_no_na_branch_remains(self):
        """v0.45.111 删掉了 rationale 里的 `else "N/A"`：加了顶部守卫后它不可达，
        留着会让人以为「缺分也能开仓、只是显示 N/A」，与实际行为相反。
        这里反向固化——缺分时压根没有 Position，自然也没有 rationale。"""
        assert pp._open_position(_snap(), NAV, AS_OF, {}, []) is None


class TestScoreReachesTheLedger:
    """说明危害为什么值得堵：score 会一路流进 closed_trades.jsonl。"""

    def test_score_propagates_into_closed_trade(self):
        pos = pp._open_position(_snap(composite_score=7.8), NAV, AS_OF, {}, [])
        trade, _pnl = pp._close_position(pos, "TP", 400.0, "2026-09-02")
        assert trade.score == pytest.approx(7.8)
        assert "score" in trade.to_dict()
