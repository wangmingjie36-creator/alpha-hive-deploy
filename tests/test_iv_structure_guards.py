"""
IV 期限结构 / IV-RV 价差的"零值伪装"回归闸（v0.45.4）

形状与 test_silent_failure_guards.py 同源：**没报错、退出码 0、页面照常渲染，
但显示的数字是编的**。2026-08-24 那批 12 只标的里，深度报告的
「IV 期限结构 + IV-RV 价差」四格有 6~7 只显示 `0.0% / 0.0%`、4 只显示
`+0.0pp / 0.0%`——不是测得为零，是三处独立缺陷叠加：

  ① `calculate_iv_rv_spread` 失败时返回的字典里 `rv_30d` 键**存在且为 None**，
     于是下游 `.get("rv_30d", 0.0)` 的默认值根本不生效，None 一路流到渲染层。
  ② `closes[closes > 5]` 用绝对量纲滤 yfinance 哨兵值，把 AMC(≈$3) 这类
     低价股的全部真实收盘价一并滤光 → RV 结构性永远不可用。
  ③ `calculate_iv_term_structure` 的 `except Exception: continue` 吞掉 SSLError，
     term_structure 变空列表 → shape="unknown" → 渲染成 0.0%。

每条测试都必须能在"把修复回退掉"时变红。
"""

import re

import pytest


# ───────────────── ① None 不得被 .get 默认值伪装成 0 ─────────────────

class TestNoneNotCoercedToZero:
    """上游诚实返回的 None 必须原样传到渲染层，不能在中途变成 0.0。"""

    def test_iv_rv_empty_payload_keeps_none(self):
        """失败载荷里这些键存在且为 None——这正是 .get(k, 0.0) 失效的原因。"""
        from market_intelligence import calculate_iv_rv_spread

        # 用一个不可能存在的 ticker 触发失败路径
        out = calculate_iv_rv_spread("__NOT_A_TICKER__", 30.0)
        assert out["rv_30d"] is None
        assert out["iv_rv_spread"] is None
        assert out.get("data_available") is False
        # 键必须"存在"，否则这个 bug 的形状就不成立了
        assert "rv_30d" in out and "iv_rv_spread" in out

    def test_options_unavailable_fallback_is_none_not_zero(self):
        """期权链完全不可用时的兜底 dict 不得把缺数写成 0.0（静默中性化）。"""
        import inspect

        import options_analyzer

        src = inspect.getsource(options_analyzer)
        # 兜底 dict 里这两行曾是 "rv_30d": 0.0 / "iv_rv_spread": 0.0
        assert '"rv_30d": 0.0' not in src, "缺数不得写成 0.0"
        assert '"iv_rv_spread": 0.0' not in src, "缺数不得写成 0.0"

    def test_result_assembly_has_no_zero_default(self):
        """结果组装处不得写 .get(k, 0.0)——默认值对 None 无效，只会制造错觉。"""
        import inspect

        import options_analyzer

        src = inspect.getsource(options_analyzer)
        assert 'iv_rv_data.get("rv_30d", 0.0)' not in src
        assert 'iv_rv_data.get("iv_rv_spread", 0.0)' not in src


# ───────────────── ② 低价股的 RV 不得被绝对阈值滤光 ─────────────────

class TestLowPricedStockRealizedVol:
    """哨兵值过滤必须用相对量纲，否则低价股整条序列被清零。"""

    @pytest.mark.parametrize("price_level", [2.5, 3.0, 4.9])
    def test_penny_range_prices_survive_sentinel_filter(self, price_level, monkeypatch):
        """AMC 形状：全序列在 $5 以下，旧的 `closes > 5` 会把它滤光。"""
        import numpy as np
        import pandas as pd

        import market_intelligence as mi

        rng = np.random.default_rng(7)
        closes = price_level * np.exp(np.cumsum(rng.normal(0, 0.02, 45)))
        frame = pd.DataFrame({"Close": closes})

        import yfinance as yf

        monkeypatch.setattr(yf, "download", lambda *a, **k: frame)
        out = mi.calculate_iv_rv_spread("AMC", 90.0)

        assert out["rv_30d"] is not None, f"${price_level} 的股票 RV 不应不可用"
        assert out["rv_30d"] > 0
        assert out.get("data_available") is True

    def test_sentinel_values_still_filtered(self, monkeypatch):
        """归一化哨兵值（~1.0）混进真实价格时仍须被剔除。

        ⚠️ 断言的是**压低**而非推高。第一版这条测试断言 `rv < 300`，结果把
        哨兵值过滤整段删掉它照样绿——因为下游 `|log_ret| < 0.5` 的跳变过滤
        已经吃掉了进出哨兵区的那两个 ±5.19 极端收益。真正残留的污染是区块
        **内部**那几个零收益，它们把波动率往下拽。不会红的测试比没有更糟，
        它让人以为这条路径有回归保护。
        """
        import numpy as np
        import pandas as pd

        import market_intelligence as mi
        import yfinance as yf

        rng = np.random.default_rng(11)
        real = 180 * np.exp(np.cumsum(rng.normal(0, 0.02, 40)))

        def _rv(series):
            monkeypatch.setattr(yf, "download",
                                lambda *a, **k: pd.DataFrame({"Close": series}))
            r = mi.calculate_iv_rv_spread("NVDA", 50.0)
            assert r["rv_30d"] is not None, "RV 不应不可用"
            return r["rv_30d"]

        clean = _rv(real)
        # 尾部插入 8 个 1.0 哨兵值：过滤生效则它们被整体剔除，RV 与干净序列一致
        polluted = _rv(np.concatenate([real, np.ones(8)]))

        assert polluted == pytest.approx(clean, rel=0.02), (
            f"哨兵值污染改变了 RV（干净 {clean:.2f}% → 污染 {polluted:.2f}%），"
            "说明过滤未生效"
        )


# ───────────────── ③ 期限结构失败必须留痕，不得静默 ─────────────────

class TestTermStructureFailsLoudly:
    def test_missing_price_reports_reason_not_zero(self):
        """现价不可用时 front/back 必须是 None 并给出原因，绝不是 0。"""
        from options_analyzer import OptionsAnalyzer

        out = OptionsAnalyzer().calculate_iv_term_structure("NVDA", 0)
        assert out["data_available"] is False
        assert out["front_iv"] is None and out["back_iv"] is None
        assert out["error"], "失败必须带原因，不能静默"

    def test_result_dict_always_carries_availability_flag(self):
        from options_analyzer import OptionsAnalyzer

        out = OptionsAnalyzer().calculate_iv_term_structure("NVDA", 0)
        for key in ("data_available", "source", "error", "front_iv", "back_iv", "iv_spread"):
            assert key in out, f"缺少 {key}，下游无法区分'没取到'与'算出来是 0'"

    def test_yfinance_path_does_not_swallow_errors(self):
        """降级路径必须逐条回传失败原因——旧实现是 `except Exception: continue`。"""
        import inspect

        from options_analyzer import OptionsAnalyzer

        src = inspect.getsource(OptionsAnalyzer._iv_term_points_yfinance)
        assert "errors.append" in src
        assert "https_gate" in src, "出站请求必须进 http_gate 闸门"


# ───────────────── ④ 渲染层：缺数显示「—」而非 0 ─────────────────

def _render_iv_block(term_structure, rv_30d, iv_rv_spread, iv_rv_signal, iv_rv_detail):
    """用最小载荷驱动 _ch3_oracle，取出 IV 小节的 HTML。"""
    from generate_ml_report import MLEnhancedReportGenerator

    gen = MLEnhancedReportGenerator.__new__(MLEnhancedReportGenerator)
    opts = {
        "iv_rank": 45.0, "iv_current": 50.0, "put_call_ratio": 1.0,
        "iv_term_structure": term_structure,
        "rv_30d": rv_30d, "iv_rv_spread": iv_rv_spread,
        "iv_rv_signal": iv_rv_signal, "iv_rv_detail": iv_rv_detail,
    }
    html = MLEnhancedReportGenerator._ch3_oracle(gen, {}, opts, current_price=100.0)
    m = re.search(r"IV 期限结构 · IV-RV 价差.*?</p>", html, re.S)
    assert m, "IV 小节未渲染"
    return m.group(0)


class TestDegradedRenderingShowsDash:
    UNAVAILABLE_TERM = {
        "shape": "unknown", "term_structure": [], "front_iv": None, "back_iv": None,
        "iv_spread": None, "data_available": False, "source": "none",
        "error": "SSLError: TLS connect error",
    }

    def test_all_unavailable_renders_no_zeros(self):
        block = _render_iv_block(self.UNAVAILABLE_TERM, None, None, "unknown",
                                 {"data_available": False, "error": "yfinance 不可用"})
        assert "0.0%" not in block, "缺数被伪装成 0.0%"
        assert "+0.0pp" not in block, "缺数被伪装成 +0.0pp"
        assert "—" in block
        assert "数据不可用" in block

    def test_failure_reason_is_surfaced_and_escaped(self):
        block = _render_iv_block(
            {**self.UNAVAILABLE_TERM, "error": "boom <script>x</script>"},
            None, None, "unknown", {"data_available": False, "error": "rv <b>err</b>"})
        assert "<script>" not in block, "失败原因未转义"
        assert "&lt;script&gt;" in block

    def test_real_values_still_render(self):
        term = {"shape": "backwardation", "term_structure": [{"expiry": "2026-09-18", "dte": 24,
                                                              "atm_iv": 47.3}],
                "front_iv": 47.3, "back_iv": 40.1, "iv_spread": -7.2,
                "data_available": True, "source": "cboe", "error": ""}
        block = _render_iv_block(term, 35.5, 17.9, "expensive", {"data_available": True})
        assert "47.3% / 40.1%" in block
        assert "+17.9pp" in block
        assert "35.5%" in block
        assert "BACKWARDATION" in block
        assert "cboe" in block, "取数来源须可见（口径可追溯）"

    def test_genuine_zero_is_distinguishable_from_missing(self):
        """真实测得 0.0 必须显示为 0.0，与「—」区分——否则修复就修反了。"""
        term = {"shape": "flat", "term_structure": [], "front_iv": 0.0, "back_iv": 0.0,
                "iv_spread": 0.0, "data_available": True, "source": "cboe", "error": ""}
        block = _render_iv_block(term, 0.0, 0.0, "fair", {"data_available": True})
        assert "0.0% / 0.0%" in block
        assert "+0.0pp" in block


# ────────── ⑤ 期限结构缺数必须弃权，不得投"中性"票 ──────────

class TestCallFlowVoteAbstains:
    """`classify_call_flow` 的 C 票：没数据 ≠ 观察到中性。

    旧实现 `if term_data.get("shape")` —— "unknown" 是 **truthy 字符串**，
    落进 else 投出一张真票 "mixed"，而函数下方
    `labels = [v for v in votes.values() if v != "unknown"]` 本就是为弃权
    设计的，这张票永远到不了那里。2026-08-24 有 14/30 只标的期限结构取数
    失败，却各自贡献了一张"中性"，既参与多数投票又抬高 confidence 分母。
    """

    CALLS = [
        {"strike": 100, "openInterest": 500, "expiration": "2026-09-11"},
        {"strike": 105, "openInterest": 900, "expiration": "2027-01-15"},
    ]

    def _classify(self, term_data):
        from options_analyzer import OptionsAnalyzer

        return OptionsAnalyzer().classify_call_flow(
            self.CALLS, [], 102.0, skew_data={"skew_ratio": 1.0}, term_data=term_data)

    @pytest.mark.parametrize("term_data", [
        {"shape": "unknown", "data_available": False},
        {"shape": "unknown"},          # 旧快照没有 data_available 键
        {},
        None,
    ])
    def test_missing_term_structure_abstains(self, term_data):
        assert self._classify(term_data)["votes"]["C"] == "unknown", \
            "缺数必须弃权，不能投中性票"

    @pytest.mark.parametrize("shape,expected", [
        ("backwardation", "directional"),
        ("contango", "mixed"),
        ("flat", "mixed"),
    ])
    def test_real_shapes_still_vote(self, shape, expected):
        """别把守卫写成永远弃权——真实形态照常投票。"""
        assert self._classify({"shape": shape, "data_available": True})["votes"]["C"] == expected

    def test_failure_and_genuine_contango_differ(self):
        """核心断言：取数失败与真实 contango 曾输出**完全一致**的分类结果。"""
        failed = self._classify({"shape": "unknown", "data_available": False})
        real = self._classify({"shape": "contango", "data_available": True})
        assert (failed["label"], failed["confidence"]) != (real["label"], real["confidence"]), \
            "取数失败被伪装成真实观测"

    def test_confidence_denominator_excludes_abstention(self):
        """弃权票不得进入 confidence 的分母。"""
        failed = self._classify({"shape": "unknown", "data_available": False})
        # 只剩 A/B 两票，分母为 2 → confidence ∈ {0.5, 1.0}
        assert failed["confidence"] in (0.5, 1.0)
        assert "term(" not in failed["reasoning"], "弃权维度不应出现在理由里"


# ────────── ⑥ CBOE 类份额符号规范化 ──────────

class TestCboeClassShareSymbol:
    """CBOE 用点 `BRK.B`，项目内部用连字符 `BRK-B`。

    2026-08-25 实测：`BRK.B` 返回 2054 个合约，`BRK-B` / `BRKB` 均 403。
    症状不是报错而是**全链降级**——主链 / 全链 OI / IV 期限结构三个 CBOE 主源
    一起失败，BRK-B 每次扫描白烧 3 次重试再整体退回 yfinance（5 次额外往返，
    恰好是 SSL 风暴高发路径）。BRK-B 是 30 只每日扫描标的之一。
    """

    @pytest.mark.parametrize("ticker,expected", [
        ("BRK-B", "BRK.B"),
        ("brk-b", "BRK.B"),
        ("BF-B", "BF.B"),
        ("NVDA", "NVDA"),
        ("nvda", "NVDA"),
    ])
    def test_symbol_mapping(self, ticker, expected):
        from cboe_options import _cboe_symbol

        assert _cboe_symbol(ticker) == expected

    def test_url_uses_dotted_form(self, monkeypatch):
        """真正要守的是 **URL** 用点号——只测 helper 不够，得看实际请求。"""
        import cboe_options as c

        seen = []

        class _Resp:
            def read(self):
                return b'{"data": {"options": []}}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=None):
            seen.append(req.full_url)
            return _Resp()

        monkeypatch.setattr(c.urllib.request, "urlopen", _fake_urlopen)
        with c._cache_lock:
            c._payload_cache.pop("BRK-B", None)
        c._fetch_cboe_payload("BRK-B", 5)

        assert seen, "未发出请求"
        assert "BRK.B.json" in seen[0], f"URL 未用点号: {seen[0]}"
        assert "BRK-B" not in seen[0]

    def test_cache_key_keeps_original_ticker(self, monkeypatch):
        """缓存键/日志/返回结构必须沿用原始 ticker，避免上下游出现第二种写法。"""
        import cboe_options as c

        class _Resp:
            def read(self):
                return b'{"data": {"options": [{"option": "BRKB260828C00270000"}]}}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(c.urllib.request, "urlopen", lambda req, timeout=None: _Resp())
        with c._cache_lock:
            c._payload_cache.pop("BRK-B", None)
            c._payload_cache.pop("BRK.B", None)
        c._fetch_cboe_payload("BRK-B", 5)

        assert "BRK-B" in c._payload_cache, "缓存键应是原始 ticker"
        assert "BRK.B" not in c._payload_cache, "缓存键不应出现 CBOE 写法"
        with c._cache_lock:
            c._payload_cache.pop("BRK-B", None)

    def test_occ_regex_still_parses_class_share_contracts(self):
        """CBOE 返回的 OCC 符号是 `BRKB...`（无分隔符），现有正则必须照常匹配。"""
        from cboe_options import _parse_occ

        parsed = _parse_occ("BRKB260828C00270000")
        assert parsed is not None
        expiry, cp, strike = parsed
        assert expiry == "2026-08-28" and cp == "C" and strike == 270.0
