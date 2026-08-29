"""OptionsAnalyzer + OptionsAgent 单元测试"""

import pytest
from options_analyzer import OptionsAnalyzer, OptionsAgent, OptionsDataFetcher


# ==================== OptionsAnalyzer 纯计算测试 ====================

class TestIVRank:
    def test_basic_iv_rank(self):
        analyzer = OptionsAnalyzer()
        hist = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65]
        rank, iv = analyzer.calculate_iv_rank(0.40, hist)
        # (0.40 - 0.20) / (0.65 - 0.20) = 0.2/0.45 ≈ 44.44
        assert 44 <= rank <= 45
        assert iv == 0.40

    def test_iv_rank_at_extremes(self):
        analyzer = OptionsAnalyzer()
        hist = [0.20] * 10 + [0.80] * 10
        rank_low, _ = analyzer.calculate_iv_rank(0.20, hist)
        rank_high, _ = analyzer.calculate_iv_rank(0.80, hist)
        assert rank_low == 0.0
        assert rank_high == 100.0

    def test_iv_rank_insufficient_data(self):
        analyzer = OptionsAnalyzer()
        rank, _ = analyzer.calculate_iv_rank(0.30, [0.25, 0.35])
        assert rank == 50.0  # 数据不足，返回中立值

    def test_iv_rank_flat_history(self):
        analyzer = OptionsAnalyzer()
        rank, _ = analyzer.calculate_iv_rank(0.30, [0.30] * 20)
        assert rank == 50.0  # max == min 时返回中立


class TestIVPercentile:
    def test_basic_percentile(self):
        analyzer = OptionsAnalyzer()
        hist = list(range(10, 110, 10))  # [10, 20, 30, ..., 100]
        pct = analyzer.calculate_iv_percentile(55, hist)
        # 有 5 个低于 55: [10,20,30,40,50] → 50%
        assert pct == 50.0

    def test_percentile_at_min(self):
        analyzer = OptionsAnalyzer()
        hist = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        pct = analyzer.calculate_iv_percentile(10, hist)
        assert pct == 0.0  # 没有比 10 更低的

    def test_percentile_insufficient_data(self):
        analyzer = OptionsAnalyzer()
        pct = analyzer.calculate_iv_percentile(50, [40, 60])
        assert pct == 50.0


class TestPutCallRatio:
    def test_basic_ratio(self):
        analyzer = OptionsAnalyzer()
        calls = [{"openInterest": 1000}, {"openInterest": 500}]
        puts = [{"openInterest": 800}, {"openInterest": 400}]
        ratio = analyzer.calculate_put_call_ratio(calls, puts)
        # put_oi=1200, call_oi=1500 → ratio=0.8
        assert abs(ratio - 0.8) < 0.01

    def test_zero_calls(self):
        analyzer = OptionsAnalyzer()
        calls = [{"openInterest": 0}]
        puts = [{"openInterest": 100}]
        ratio = analyzer.calculate_put_call_ratio(calls, puts)
        assert ratio >= 0  # 不应崩溃

    def test_empty_chains(self):
        analyzer = OptionsAnalyzer()
        ratio = analyzer.calculate_put_call_ratio([], [])
        assert ratio == 1.0  # 默认中立


class TestGammaExposure:
    def test_basic_gex(self):
        analyzer = OptionsAnalyzer()
        calls = [{"strike": 150, "openInterest": 1000, "gamma": 0.05}]
        puts = [{"strike": 140, "openInterest": 800, "gamma": 0.03}]
        gex = analyzer.calculate_gamma_exposure(calls, puts, 145.0)
        assert isinstance(gex, float)

    def test_gex_empty_chains(self):
        """v0.45.63：空链返回 None，不返回 0.0。

        旧断言写的是 `== 0.0` —— 那把 bug 固化成了契约。
        0.0 在 GEX 的量纲里是「做市商净 gamma 中性」这个**真实读数**，
        而它还会经 signal_archive 归档成一条真观测进 IC 数据集。
        """
        analyzer = OptionsAnalyzer()
        assert analyzer.calculate_gamma_exposure([], [], 145.0) is None


class TestIVSkew:
    def test_skew_with_data(self):
        analyzer = OptionsAnalyzer()
        # 构造 OTM puts（strike < stock_price）和 OTM calls（strike > stock_price）
        calls = [{"strike": 160, "impliedVolatility": 0.30, "openInterest": 100}]
        puts = [{"strike": 140, "impliedVolatility": 0.45, "openInterest": 100}]
        result = analyzer.calculate_iv_skew(calls, puts, 150.0)
        assert "skew_ratio" in result
        # puts IV > calls IV → skew > 1
        assert result["skew_ratio"] > 1.0

    def test_skew_empty_data(self):
        analyzer = OptionsAnalyzer()
        result = analyzer.calculate_iv_skew([], [], 150.0)
        assert result.get("skew_ratio") is None or result.get("skew_signal") == ""


class TestOptionsScore:
    def test_score_range(self):
        analyzer = OptionsAnalyzer()
        score, summary = analyzer.generate_options_score(
            iv_rank=75, put_call_ratio=0.8, gex=0.001, unusual=[]
        )
        assert 0 <= score <= 10
        assert isinstance(summary, str)


# ==================== OptionsAgent 集成测试 ====================

class TestOptionsAgent:
    def test_analyze_returns_required_keys(self, monkeypatch):
        """OptionsAgent.analyze() 应返回所有必需字段"""
        agent = OptionsAgent()

        # Mock fetch 方法，避免真实 API 调用
        monkeypatch.setattr(
            agent.fetcher, "fetch_options_chain",
            lambda ticker: {
                "calls": [
                    {"strike": 140, "openInterest": 500, "impliedVolatility": 0.35, "gamma": 0.04},
                    {"strike": 150, "openInterest": 800, "impliedVolatility": 0.30, "gamma": 0.05},
                    {"strike": 160, "openInterest": 300, "impliedVolatility": 0.28, "gamma": 0.03},
                ],
                "puts": [
                    {"strike": 130, "openInterest": 400, "impliedVolatility": 0.40, "gamma": 0.03},
                    {"strike": 140, "openInterest": 600, "impliedVolatility": 0.38, "gamma": 0.04},
                    {"strike": 150, "openInterest": 200, "impliedVolatility": 0.32, "gamma": 0.05},
                ],
                "expirations": ["2026-03-20", "2026-04-17"],
                "source": "real",
            }
        )
        monkeypatch.setattr(
            agent.fetcher, "fetch_historical_hv",
            lambda ticker: [0.25 + i * 0.02 for i in range(20)]
        )
        # 隔离缓存：阻止测试写入/读取生产 last_valid_iv 缓存文件
        monkeypatch.setattr(agent.fetcher, "_save_last_valid_iv", lambda ticker, iv: None)
        monkeypatch.setattr(agent.fetcher, "_read_last_valid_iv", lambda ticker: None)

        result = agent.analyze("NVDA", stock_price=145.0)

        required_keys = [
            "ticker", "data_quality", "iv_rank", "iv_percentile",
            "put_call_ratio", "gamma_exposure", "options_score",
            "flow_direction", "iv_skew_ratio",
        ]
        for key in required_keys:
            assert key in result, f"Missing key: {key}"

        assert result["ticker"] == "NVDA"
        assert 0 <= result["iv_rank"] <= 100
        assert result["put_call_ratio"] > 0

    def test_analyze_sample_data_quality(self, monkeypatch):
        """样本数据应标记为 data_quality='unavailable'"""
        agent = OptionsAgent()
        monkeypatch.setattr(
            agent.fetcher, "fetch_options_chain",
            lambda ticker: {
                "calls": [{"strike": 150, "openInterest": 100, "impliedVolatility": 0.30, "gamma": 0.05}],
                "puts": [{"strike": 140, "openInterest": 100, "impliedVolatility": 0.35, "gamma": 0.04}],
                "expirations": [],
                "source": "sample",  # 标记为样本数据
            }
        )
        monkeypatch.setattr(
            agent.fetcher, "fetch_historical_hv",
            lambda ticker: [0.30] * 20
        )
        # 隔离缓存：阻止测试写入/读取生产 last_valid_iv 缓存文件
        monkeypatch.setattr(agent.fetcher, "_save_last_valid_iv", lambda ticker, iv: None)
        monkeypatch.setattr(agent.fetcher, "_read_last_valid_iv", lambda ticker: None)

        result = agent.analyze("TEST", stock_price=145.0)
        assert result["data_quality"] == "unavailable"


# ==================== OptionsDataFetcher 缓存测试 ====================

class TestOptionsDataFetcher:
    def test_cache_write_and_read(self, tmp_path):
        fetcher = OptionsDataFetcher(cache_dir=str(tmp_path))
        # 写入缓存
        fetcher._write_cache("NVDA", "chain", {"calls": [], "puts": []})
        # 读取缓存
        cached = fetcher._read_cache("NVDA", "chain")
        assert cached is not None
        assert "calls" in cached

    def test_cache_miss(self, tmp_path):
        fetcher = OptionsDataFetcher(cache_dir=str(tmp_path))
        cached = fetcher._read_cache("NVDA", "nonexistent")
        assert cached is None


# ==================== v0.45.63：低价股的绝对价格地板 ====================

def _grid(strikes, iv, oi=5000, gamma=0.05):
    return [{"strike": s, "impliedVolatility": iv, "openInterest": oi,
             "gamma": gamma, "dte_weight": 1.0} for s in strikes]


class TestLowPricedUnderlying:
    """AMC（$2.70）自入库起 skew 恒为「数据不足」、gamma_exposure 恒为 0.0。

    两个独立原因，缺一个修都不够：
      ① `stock_price < 5` 绝对地板 —— 注释说防样本数据，但样本链在 analyze()
         里早就被 `source == "sample"` 拦掉了，这道守卫只剩误伤
      ② ±3% 容差是**股价的百分比**，行权价网格是**绝对间距**，两者随价格反向
         张缩：AMC ±$0.081 的窗口装不下 $0.50 一档的网格
    """

    # AMC 2026-08-27 实测行权价
    AMC_STRIKES = [1.0, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5]

    def test_skew_computed_for_low_priced_stock(self):
        analyzer = OptionsAnalyzer()
        r = analyzer.calculate_iv_skew(
            _grid(self.AMC_STRIKES, 1.25), _grid(self.AMC_STRIKES, 1.125), 2.70)
        assert r["skew_ratio"] is not None, "AMC 形状仍拿不到 skew"
        assert r["skew_signal"] != "数据不足"
        assert r["skew_basis"] == "nearest_strike"

    def test_gex_computed_for_low_priced_stock(self):
        analyzer = OptionsAnalyzer()
        gex = analyzer.calculate_gamma_exposure(
            _grid(self.AMC_STRIKES, 1.25, oi=9000, gamma=0.08),
            _grid(self.AMC_STRIKES, 1.125, oi=4000, gamma=0.05), 2.70)
        assert gex is not None and gex != 0.0, "低价股 GEX 仍被地板打成 0"

    def test_no_cliff_at_five_dollars(self):
        """$4.99 与 $5.01 不该是「有值」与「没值」的分界。"""
        analyzer = OptionsAnalyzer()
        ks = [3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5]
        for spot in (4.99, 5.01):
            r = analyzer.calculate_iv_skew(_grid(ks, 0.9), _grid(ks, 1.0), spot)
            assert r["skew_ratio"] is not None, f"${spot} 仍拿不到 skew"
            assert analyzer.calculate_gamma_exposure(
                _grid(ks, 0.9, oi=9000, gamma=0.08),
                _grid(ks, 1.0), spot) is not None

    def test_dense_grid_still_uses_window(self):
        """高价股必须逐字节走原路径 —— 否则其余 29 只产生口径世代边界。"""
        analyzer = OptionsAnalyzer()
        ks = [round(200 + 2.5 * i, 1) for i in range(30)]
        r = analyzer.calculate_iv_skew(_grid(ks, 0.33), _grid(ks, 0.36), 228.0)
        assert r["skew_basis"] == "window"

    def test_fallback_refuses_degenerate_chain(self):
        """退化链（两档且离目标极远）宁可不给，也不拿最近档凑一个数。"""
        analyzer = OptionsAnalyzer()
        far = _grid([1.0, 50.0], 0.8)
        assert analyzer.calculate_iv_skew(far, far, 2.70)["skew_ratio"] is None

    def test_fallback_stays_on_otm_side(self):
        """退化路径只认 OTM 一侧：put ≤ 现价、call ≥ 现价。

        取到 ITM 档算出来的就不是 skew 了 —— 那是另一个量，
        而它会以同一个字段名进评分投票（votes["B"]）。
        """
        v0_45_63_note = """变异检查抓到的假护栏：初版用 calls=[1.0,1.5]，
        看似在测 OTM 约束，实际那两档离目标 1.34 > 上限 0.675，**先被距离上限
        挡掉了**——去掉 OTM 约束这条测试照样绿。必须挑一个「过得了距离上限、
        但站错边」的档位，约束才是唯一起作用的那个条件。"""
        assert v0_45_63_note
        analyzer = OptionsAnalyzer()
        # 现价 2.70：call_target=2.835，距离上限 ±0.675 → 窗口 [2.16, 3.51]
        # 档位 2.5 在上限之内（差 0.335），但 2.5 < 2.70 是 ITM call
        r = analyzer.calculate_iv_skew(_grid([2.5], 0.9), _grid([2.5], 1.0), 2.70)
        assert r["skew_ratio"] is None, "ITM call 档被当成 OTM 用了"

    def test_zero_gamma_chain_is_none_not_zero(self):
        analyzer = OptionsAnalyzer()
        z = _grid(self.AMC_STRIKES, 1.25, oi=0, gamma=0.0)
        assert analyzer.calculate_gamma_exposure(z, z, 2.70) is None

    def test_zero_or_negative_price_is_none(self):
        analyzer = OptionsAnalyzer()
        g = _grid(self.AMC_STRIKES, 1.25)
        assert analyzer.calculate_gamma_exposure(g, g, 0.0) is None
        assert analyzer.calculate_iv_skew(g, g, 0.0)["skew_ratio"] is None


# ============ v0.45.63 二次检查：改返回类型契约的连带伤害 ============

class TestGexNoneContract:
    """`calculate_gamma_exposure` 从「失败返回 0.0」改成「失败返回 None」，
    而 **analyze() 里三处、generate_options_score 里两处**直接拿它做数值比较。

    这五处在原代码里是安全的（gex 恒为 float），改完全部 TypeError。
    更糟的是 analyze() 的调用方用宽 except（alpha_hive_daily_report:2210），
    崩溃会被吞成「该标的整份期权数据消失」—— 看着像取数失败，其实是类型错。

    教训：**改返回类型契约时，单测被改的那个函数远远不够，必须跑一遍调用方。**
    第一轮我给 calculate_gamma_exposure 写了 8 条测试，一条都没碰到调用方。
    """

    def test_options_score_accepts_none_gex(self):
        analyzer = OptionsAnalyzer()
        score, summary = analyzer.generate_options_score(50.0, 1.0, None, [])
        assert isinstance(score, float) and isinstance(summary, str)

    def test_none_gex_scores_same_as_zero_gex(self):
        """None 必须与改动前的 0.0 打同一个分 —— 否则历史样本产生口径世代边界。"""
        analyzer = OptionsAnalyzer()
        assert (analyzer.generate_options_score(50.0, 1.0, None, [])
                == analyzer.generate_options_score(50.0, 1.0, 0.0, []))

    def test_analyze_survives_none_gex(self, monkeypatch):
        """整条链 gamma×OI 全为 0 → GEX 为 None → analyze() 不得抛异常。

        这是第一轮漏掉的那个测试：它走的是**调用方**，不是被改的函数。
        """
        agent = OptionsAgent()
        rows_c = [{"strike": 150, "openInterest": 0, "impliedVolatility": 0.30, "gamma": 0.0},
                  {"strike": 160, "openInterest": 0, "impliedVolatility": 0.28, "gamma": 0.0}]
        rows_p = [{"strike": 130, "openInterest": 0, "impliedVolatility": 0.40, "gamma": 0.0},
                  {"strike": 140, "openInterest": 0, "impliedVolatility": 0.38, "gamma": 0.0}]
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain",
                            lambda t: {"calls": rows_c, "puts": rows_p,
                                       "expirations": ["2026-09-18"], "source": "real"})
        monkeypatch.setattr(agent.fetcher, "fetch_historical_hv",
                            lambda t: [0.25 + i * 0.02 for i in range(20)])
        monkeypatch.setattr(agent.fetcher, "_save_last_valid_iv", lambda t, iv: None)
        monkeypatch.setattr(agent.fetcher, "_read_last_valid_iv", lambda t: None)

        result = agent.analyze("ZZZ", stock_price=145.0)
        assert result["gamma_exposure"] is None
        # 不得伪造成「中性」——那正是 0.0 的老毛病换了个字段重演
        assert result["gamma_squeeze_risk"] == "unknown"


class TestNearestStrikeAveraging:
    """期权链是**跨到期日拍平**的：同一个行权价有多行，IV 各不相同。

    初版退化路径用 `min(otm, key=dist)` 只取其中一行 —— 取哪行取决于列表顺序，
    且与窗口路径「取平均」的语义不一致。实测 AMC 8/27：32 行 = 8 档 × 4 个到期日，
    $3.0 那档的 IV 是 [0.8841, 0.8406, 0.8604, 0.8577]，初版取了第一个。
    """

    def _chain(self, spot=2.70):
        # 每档 3 个到期日，IV 明显不同 —— 取一行还是取平均，结果必然可分辨
        calls, puts = [], []
        for k in (2.5, 3.0, 3.5):
            for iv in (0.90, 0.80, 0.70):
                calls.append({"strike": k, "impliedVolatility": iv,
                              "openInterest": 100, "gamma": 0.05})
        for k in (1.5, 2.0, 2.5):
            for iv in (0.60, 0.50, 0.40):
                puts.append({"strike": k, "impliedVolatility": iv,
                             "openInterest": 100, "gamma": 0.05})
        return calls, puts, spot

    def test_averages_all_expiries_at_the_chosen_strike(self):
        analyzer = OptionsAnalyzer()
        calls, puts, spot = self._chain()
        r = analyzer.calculate_iv_skew(calls, puts, spot)
        assert r["skew_basis"] == "nearest_strike"
        # 均值 (0.90+0.80+0.70)/3 = 0.80 → 80.0%；只取第一行会得到 90.0
        assert r["otm_call_iv"] == pytest.approx(80.0), "退化路径只取了一行，没按档聚合"
        assert r["otm_put_iv"] == pytest.approx(50.0)

    def test_result_is_independent_of_row_order(self):
        """取哪一行不该由列表顺序决定 —— 那是不可复现的。"""
        import random
        analyzer = OptionsAnalyzer()
        calls, puts, spot = self._chain()
        base = analyzer.calculate_iv_skew(calls, puts, spot)
        for seed in (1, 2, 3):
            c, p = list(calls), list(puts)
            random.Random(seed).shuffle(c)
            random.Random(seed + 100).shuffle(p)
            assert analyzer.calculate_iv_skew(c, p, spot) == base

    def test_equidistant_strikes_break_ties_deterministically(self):
        """目标恰在两档正中时，取舍不得依赖 dict/list 顺序。"""
        import random
        analyzer = OptionsAnalyzer()
        # 变异检查抓到的第二个假护栏（本轮）：初版用 1.6/2.6 声称「等距」，
        # 但 1.6 < 现价 2.00，先被 OTM 侧约束滤掉了 —— 根本没有并列，
        # 去掉次序键照样绿。必须真的构造两档同距的 **OTM** 行权价。
        #
        # spot=2.00 → call_target=2.10，容差 ±0.06（窗口装不下，必走退化路径）
        # 档位 2.0 与 2.2 到 2.10 都是 0.10，且都 ≥ 现价 → 真并列
        calls = [{"strike": k, "impliedVolatility": iv, "openInterest": 100, "gamma": 0.05}
                 for k, iv in ((2.0, 0.90), (2.2, 0.40))]
        puts = [{"strike": 1.9, "impliedVolatility": 0.50, "openInterest": 100, "gamma": 0.05}]
        base = analyzer.calculate_iv_skew(calls, puts, 2.00)
        assert base["skew_basis"] == "nearest_strike", "没走到退化路径，这条测不到并列"
        for seed in range(12):
            c = list(calls)
            random.Random(seed).shuffle(c)
            assert analyzer.calculate_iv_skew(c, puts, 2.00) == base
