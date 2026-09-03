"""self_analyst NaN 价格防护回归测试（v0.45.93）。

背景：2026-09 月度自诊断简报把 `T+7 平均收益` 印成 `+nan%`，追下去发现 645 条
快照里有 2 条 `entry_price=NaN`（2026-06-26 META / RKLB）。真正的破坏不在那个
显眼的 `+nan%`，而在两处不显眼的地方：

  1. `classify()` 把 NaN 快照判成 "wrong"（`nan > 0` 和 `nan < 0` 同时为 False），
     悄悄污染胜率分母；
  2. `compute_dimension_ic()` 把 NaN 喂进 `_spearman()` 的排序，**整张 IC 表位移
     最多 0.26**——Guard +0.066→+0.296、Chronos +0.012→+0.272、Queen +0.050→+0.252，
     凭空造出一份「高 IC 蜂」排名。

根因是 `bool(float('nan')) is True`，所以 `if ep and t7` 这类真值判断挡不住 NaN。
下面每个测试都对应「喂退化数据看它红」：把 `_is_valid_price` 改回 `bool(...)`
语义，这些测试必须失败。
"""

import math

import pytest

import self_analyst as sa


# ---------------------------------------------------------------- _is_valid_price

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"),
                                 0, 0.0, -1.5, None, "32.94", True])
def test_invalid_prices_rejected(bad):
    """NaN/inf/非正数/None/字符串/bool 一律不是可用价格。

    `True` 单列：`isinstance(True, int)` 为真且 `True > 0`，不显式排除会让
    布尔值冒充价格 1.0。
    """
    assert sa._is_valid_price(bad) is False


@pytest.mark.parametrize("good", [32.94, 1, 0.01, 1e6])
def test_valid_prices_accepted(good):
    assert sa._is_valid_price(good) is True


def test_truthiness_alone_would_not_catch_nan():
    """守卫本身的理由：真值判断挡不住 NaN。

    这条断言的是 Python 语义（不是我们的代码），用来锁住「为什么不能写
    `if price:`」——若哪天有人把 `_is_valid_price` 换回真值判断，上面的测试会红，
    而这条负责解释红的原因。
    """
    assert bool(float("nan")) is True


# ---------------------------------------------------------------- classify

def _snap(direction, entry, t7):
    return {"direction": direction, "entry_price": entry,
            "actual_prices": {"t7": t7}}


def test_nan_entry_price_is_unknown_not_wrong():
    """NaN 快照必须落 unknown，不能落 wrong（否则污染胜率分母）。"""
    for direction in ("bullish", "bearish", "long", "short"):
        assert sa.classify(_snap(direction, float("nan"), 603.12)) == "unknown"


def test_nan_t7_price_is_unknown():
    assert sa.classify(_snap("bullish", 100.0, float("nan"))) == "unknown"


def test_valid_prices_still_classify_normally():
    """护栏不能误伤正常样本。"""
    assert sa.classify(_snap("bullish", 100.0, 110.0)) == "correct"
    assert sa.classify(_snap("bullish", 100.0, 90.0)) == "wrong"
    assert sa.classify(_snap("bearish", 100.0, 90.0)) == "correct"
    assert sa.classify(_snap("bearish", 100.0, 110.0)) == "wrong"
    assert sa.classify(_snap("neutral", 100.0, 110.0)) == "neutral"


# ---------------------------------------------------------------- compute_stats

def test_avg_return_not_poisoned_by_nan():
    """一条 NaN 就能把整个均值传染成 nan——这正是简报里 `+nan%` 的来历。"""
    snaps = [_snap("bullish", 100.0, 110.0),
             _snap("bullish", 100.0, 90.0),
             _snap("bullish", float("nan"), 105.0)]
    stats = sa.compute_stats(snaps)
    assert math.isfinite(stats["avg_ret_7d"]), "NaN 泄漏进 avg_ret_7d"
    assert stats["avg_ret_7d"] == pytest.approx(0.0)


def test_invalid_price_rows_are_counted_not_silently_dropped():
    """剔除必须留痕：静默丢弃只是把盲区换个地方藏。"""
    snaps = [_snap("bullish", 100.0, 110.0),
             _snap("bullish", float("nan"), 105.0),
             _snap("bearish", 100.0, float("nan"))]
    stats = sa.compute_stats(snaps)
    assert stats["invalid_price"] == 2
    assert stats["n_directional"] == 1, "坏行不得进入胜率分母"


# ---------------------------------------------------------------- _spearman / IC

def test_spearman_drops_nan_pairs_instead_of_scrambling():
    """NaN 会让 sorted() 的比较关系自相矛盾，产出未定义名次。

    这里构造一段单调序列 + 2 条 NaN：剔除后应恢复完美相关 1.0。
    """
    xs = list(range(30))
    ys = [float(i) for i in range(30)]
    ys[7] = float("nan")
    ys[19] = float("nan")
    assert sa._spearman(xs, ys) == pytest.approx(1.0)


def test_spearman_returns_zero_when_too_few_valid_pairs():
    xs = [1.0, 2.0, 3.0]
    ys = [float("nan"), float("nan"), 3.0]
    assert sa._spearman(xs, ys) == 0.0


def test_dimension_ic_unmoved_by_added_nan_rows():
    """核心回归：追加纯 NaN 行不得改变任何一只蜂的 IC。

    修复前，645 条真实样本里的 2 条坏行让全表位移最多 0.26。
    """
    base = []
    for i in range(40):
        base.append({
            "date": f"2026-07-{(i % 28) + 1:02d}",
            "entry_price": 100.0,
            "actual_prices": {"t7": 100.0 + (i % 11) - 5},
            "agent_votes": {"OracleBeeEcho": 5.0 + (i % 7) * 0.3,
                            "BuzzBeeWhisper": 4.0 + (i % 5) * 0.5},
        })
    before = sa.compute_dimension_ic(base)

    polluted = base + [
        {"date": "2026-06-26", "entry_price": float("nan"),
         "actual_prices": {"t7": 603.12},
         "agent_votes": {"OracleBeeEcho": 9.9, "BuzzBeeWhisper": 9.9}},
        {"date": "2026-06-26", "entry_price": float("nan"),
         "actual_prices": {"t7": 83.35},
         "agent_votes": {"OracleBeeEcho": 1.1, "BuzzBeeWhisper": 1.1}},
    ]
    after = sa.compute_dimension_ic(polluted)

    assert set(before) == set(after)
    for agent in before:
        assert after[agent]["ic"] == pytest.approx(before[agent]["ic"]), \
            f"{agent} 的 IC 被 NaN 行改动了"
        assert after[agent]["n"] == before[agent]["n"], \
            f"{agent} 的样本数把 NaN 行算进去了"
