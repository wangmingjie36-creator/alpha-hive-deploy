"""`experiments/reclassify_option_quarantine_from_swarm_20261012.py` 判据的单元测试（v0.45.479）。

每条写明哪个变异会让它变红；纯函数、零外部依赖。
"""
import importlib.util
from pathlib import Path

_P = Path(__file__).resolve().parent.parent / "experiments" / "reclassify_option_quarantine_from_swarm_20261012.py"
_spec = importlib.util.spec_from_file_location("reclass", _P)
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

CLOSES = {"2026-06-08": 29.24, "2026-06-09": 29.23, "2026-06-10": 29.5}


def _det(px, **kw):
    d = {"_snapshot_stock_price": px, "total_oi": 1000, "put_call_ratio": 0.8,
         "key_levels": {"support": [{"strike": 28.0, "oi": 500.0, "iv": 0.61}, {"strike": 29.0, "oi": 400.0, "iv": 0.6}],
                        "resistance": [{"strike": 30.0, "oi": 300.0, "iv": 0.59}]}}
    d.update(kw)
    return d


def test_single_day_integer_ratio_is_not_a_split():
    """06-09 VKTX：假价 145 / 真收盘 29.23 = 4.96，单日一个比值不算拆股。

    变红的变异：把拆股判定改回「单日比值 ≈ 整数 ±1% 就算」。
    """
    assert R.classify(_det(145.0), CLOSES, 29.23, "2026-06-09") == ("full", "constant_price")
    assert R.split_factors({"VKTX": [4.96]}) == {}


def test_repeated_integer_ratio_is_a_split():
    """CRWD：四条记录比值都是 4.000 ⇒ 拆股调整差，不是假价。

    变红的变异：拆股要求的条数阈值调到 > 4；或删掉 `split` 豁免。
    """
    assert R.split_factors({"CRWD": [4.0, 4.0001, 3.9998, 4.0]}) == {"CRWD": 4}
    closes = {"2026-04-24": 112.0325}
    assert R.classify(_det(448.13), closes, None, "2026-04-26", split=4) == (None, "")


def test_strike_shaped_price_off_grid_is_real():
    """MU 08-26：952.0 不在步长 5 的行权价网格中点上 ⇒ 不可能是行权价中位数。

    变红的变异：去掉 `_on_strike_grid` 那道判断。
    """
    det = _det(952.0, key_levels={"support": [{"strike": 940.0, "oi": 1.0, "iv": 0.5}, {"strike": 945.0, "oi": 1.0, "iv": 0.5}],
                                  "resistance": [{"strike": 955.0, "oi": 1.0, "iv": 0.5}]})
    assert R.classify(det, {"2026-08-26": 938.4}, 938.4, "2026-08-26") == (None, "")


def test_caller_price_equality_only_counts_for_small_deviation():
    """蜂群当天用的价 = 快照价，只在偏离 ≤ 20% 时证明是真价。

    变红的变异：去掉 `dev <= 0.20` 那个条件。
    """
    assert R.classify(_det(100.0), CLOSES, 100.0, "2026-06-09")[0] == "full"


def test_empty_chain_and_sample_chain():
    """空链（OI 0）与样本链（85200 / 0.54）整份隔离。

    变红的变异：删掉 `empty_chain` 或 `sample_chain` 分支。
    """
    assert R.classify(_det(29.23, total_oi=0), CLOSES, None, "2026-06-09") == ("full", "empty_chain")
    assert R.classify(_det(29.23, total_oi=85200, put_call_ratio=0.54), CLOSES, None, "2026-06-09") == ("full", "sample_chain")


def test_yfinance_fingerprint_only_from_cboe_era():
    """Yahoo 长浮点 IV 指纹只在 06-29 起判（之前本来就是 yfinance 主源，走世代边界）。

    变红的变异：把 `CBOE_FROM` 提前或删掉日期条件。
    """
    det = _det(29.23, key_levels={"support": [{"strike": 29.0, "oi": 1.0, "iv": 0.8906260937499999}]})
    assert R.classify(det, CLOSES, None, "2026-09-01") == ("gex", "yfinance_chain_fingerprint")
    assert R.classify(det, CLOSES, None, "2026-06-09") == (None, "")
