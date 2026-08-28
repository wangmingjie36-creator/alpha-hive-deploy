"""v0.45.42 —— 缺失值不许冒充 0

2026-08-26 的 yfinance 全线故障暴露了四处「安全默认值」，它们的共同特征是：
**默认值本身是一个合法、可解读、且完全虚假的读数**。

  · IV Rank None → 0.0%      读作「IV 处于历史区间最低点」（强做多波动率信号）
  · IV Skew 取错蜂 → 0.00     读作「无偏斜」，且从上线起就没对过
  · SPY 基准取数失败 → 0%     读作「大盘半年没动」，Alpha 因此 = 组合收益本身
                              （当天网站 +4.29%，真值 −5.62%，符号反了）
  · spy_return_t7 None → 0.0  读作「那一周大盘没动」

判据（CLAUDE.md 安全默认值）：这个默认值会不会让下游误以为掌握了信息。

本文件的每条测试都是「喂退化数据看它红」——把上游置空，断言渲染出的是「—」
而不是一个能被当真的数字。
"""

import re

import pytest


# ─────────────── ① ML 报告：IV Rank / P/C / OI 缺失 ───────────────

def _oracle_html(**overrides):
    from generate_ml_report import MLEnhancedReportGenerator

    gen = MLEnhancedReportGenerator.__new__(MLEnhancedReportGenerator)
    opts = {"iv_rank": 45.0, "iv_current": 50.0, "put_call_ratio": 1.0,
            "total_oi": 12345, "iv_skew_ratio": 1.05}
    opts.update(overrides)
    return MLEnhancedReportGenerator._ch3_oracle(gen, {}, opts, current_price=100.0)


def _card_value(html: str, label: str):
    """取出 <div class="lbl">{label}</div> 前面那个 num 单元格的文本"""
    # 非贪婪不够——num 内部可能含 <span>，但绝不能跨过 </div><div class="lbl">
    m = re.search(r'<div class="num"[^>]*>((?:(?!<div class="num").)*?)'
                  r'</div><div class="lbl">' + re.escape(label) + "</div>",
                  html, re.S)
    assert m, f"未找到卡片「{label}」"
    return re.sub(r"<[^>]+>", "", m.group(1)).strip()


class TestIVRankMissingRendersDash:
    def test_present_renders_number(self):
        assert _card_value(_oracle_html(iv_rank=47.4), "IV Rank") == "47.4%"

    def test_missing_renders_dash_not_zero(self):
        v = _card_value(_oracle_html(iv_rank=None), "IV Rank")
        assert v == "—", f"IV Rank 缺失渲染成 {v!r}"
        assert "0.0" not in v, "0.0% 会被读作『IV 在历史最低点』"

    def test_key_absent_also_dash(self):
        opts = {"iv_current": 50.0, "put_call_ratio": 1.0}
        from generate_ml_report import MLEnhancedReportGenerator
        gen = MLEnhancedReportGenerator.__new__(MLEnhancedReportGenerator)
        html = MLEnhancedReportGenerator._ch3_oracle(gen, {}, opts, current_price=100.0)
        assert _card_value(html, "IV Rank") == "—"

    @pytest.mark.parametrize("field,label", [
        ("put_call_ratio", "近端 P/C Ratio"),
        ("iv_current", "当前 IV"),
    ])
    def test_other_option_metrics_also_dash(self, field, label):
        assert _card_value(_oracle_html(**{field: None}), label) == "—"

    def test_total_oi_missing_dash(self):
        html = _oracle_html(total_oi=None)
        m = re.search(r"<td>近端总持仓量</td><td>(.*?)</td>", html, re.S)
        assert m and "—" in m.group(1), "总持仓量缺失应显示 —"


# ─────────────── ② BearBee 的 IV Skew 必须来自 OracleBee ───────────────

class TestBearSkewReadsOracleBee:
    @staticmethod
    def _bear_html(agent_details):
        from generate_ml_report import MLEnhancedReportGenerator
        gen = MLEnhancedReportGenerator.__new__(MLEnhancedReportGenerator)
        return MLEnhancedReportGenerator._ch3_bear(gen, agent_details)

    BEAR = {"BearBeeContrarian": {"score": 5.0,
                                  "details": {"bearish_signals": [], "bear_score": 6.0}}}

    def test_reads_skew_from_oracle_not_bear(self):
        ad = dict(self.BEAR)
        ad["OracleBeeEcho"] = {"details": {"iv_skew_ratio": 1.37}}
        assert _card_value(self._bear_html(ad), "IV Skew 比") == "1.37"

    def test_regression_bear_details_alone_no_longer_yields_zero(self):
        """旧 bug：从 BearBee 的 details 取 → 永远取不到 → 恒定 0.00"""
        v = _card_value(self._bear_html(dict(self.BEAR)), "IV Skew 比")
        assert v == "—", f"OracleBee 缺席时应显示 —，实得 {v!r}"
        assert v != "0.00", "0.00 是旧 bug 的指纹（从上线起就没对过）"


# ─────────────── ③ SPY 基准：取数失败 ≠ 大盘没动 ───────────────

class TestSPYBenchmarkUnavailable:
    def test_empty_prices_yields_none_not_zero(self, monkeypatch):
        import portfolio_backtest as pb
        monkeypatch.setattr(pb, "_fetch_spy_prices", lambda *a, **k: {})
        r = pb.run_backtest(pb.BacktestConfig(exclude_nontrading_days=True))
        if "error" in r:
            pytest.skip(f"回测不可用：{r['error']}")
        b = r["benchmark"]
        assert b["spy_return_pct"] is None, "取数失败必须给 None，不能给 0"
        assert b["available"] is False
        assert r["alpha"] is None, "没有基准就没有 Alpha —— 不能拿组合收益冒充"

    def test_available_when_prices_present(self):
        import portfolio_backtest as pb
        r = pb.run_backtest(pb.BacktestConfig(exclude_nontrading_days=True))
        if "error" in r:
            pytest.skip(f"回测不可用：{r['error']}")
        if not r["benchmark"]["available"]:
            pytest.skip("本次 SPY 取数失败（网络），正例无法验证")
        assert r["benchmark"]["spy_return_pct"] is not None
        assert r["alpha"] is not None

    def test_nontrading_boundary_still_resolves(self, monkeypatch):
        """起止日落在非交易日时，取最近交易日，而不是退化成 0"""
        import portfolio_backtest as pb
        real = pb._fetch_spy_prices

        def _weekend_holes(*a, **k):
            px = real(*a, **k)
            # 抹掉首尾各 3 天，模拟起止日恰逢周末/假日
            if not px:
                return px
            ks = sorted(px)
            for d in ks[:3] + ks[-3:]:
                px.pop(d, None)
            return px

        monkeypatch.setattr(pb, "_fetch_spy_prices", _weekend_holes)
        r = pb.run_backtest(pb.BacktestConfig(exclude_nontrading_days=True))
        if "error" in r:
            pytest.skip(f"回测不可用：{r['error']}")
        if not r["benchmark"]["available"]:
            pytest.skip("本次 SPY 取数失败（网络）")
        assert r["benchmark"]["spy_return_pct"] is not None


# ─────────────── ④ 静态守卫：出站请求进闸门 ───────────────

class TestSPYFetchGuarded:
    def test_fetch_spy_prices_uses_http_gate_and_retries(self):
        import inspect

        import portfolio_backtest as pb
        src = inspect.getsource(pb._fetch_spy_prices)
        assert "https_gate" in src, "出站请求必须进 http_gate 闸门"
        assert "range(3)" in src, "必须有退避重试"
        assert "_log.warning" in src, "失败必须留痕，不能静默 return {}"

    def test_dashboard_js_does_not_coerce_null_spy_to_zero(self):
        from pathlib import Path
        js = Path("templates/dashboard.js").read_text()
        assert "Number(real.spy_return_pct)||0" not in js, \
            "`Number(null)||0` 会把『取数失败』渲染成『大盘 0%』"
        assert "Number(real.alpha_vs_spy)||0" not in js, \
            "Alpha 同理——没有基准时它必须是 —，不是组合收益本身"


# ─────────────── ⑤ 核心指标区块（第二处同型缺陷，v0.45.43）───────────────

class TestOptionsSectionCoreMetrics:
    """`_generate_options_section_html` 与 `_ch3_oracle` 是两套独立渲染，
    v0.45.42 只修了后者。8/26 是**部分**降级（CBOE 好、yfinance 挂），
    data_quality 仍是 "real" ⇒ 全盘不可用闸挡不住，于是 iv_rank=50
    渲染成「50.0（中等 IV）」——一个确凿的假读数。"""

    @staticmethod
    def _html(**over):
        from generate_ml_report import MLEnhancedReportGenerator
        gen = MLEnhancedReportGenerator.__new__(MLEnhancedReportGenerator)
        opts = {"data_quality": "real", "iv_rank": 37.8, "iv_current": 48.61,
                "iv_percentile": 26.0, "put_call_ratio": 0.91}
        opts.update(over)
        return MLEnhancedReportGenerator._generate_options_section_html(gen, opts)

    @staticmethod
    def _metric(html, label):
        import re
        m = re.search(r'<span class="metric-label">' + re.escape(label) +
                      r'</span>\s*<span class="metric-value"[^>]*>\s*(.*?)\s*</span>',
                      html, re.S)
        assert m, f"未找到指标「{label}」"
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()

    def test_present_values_render(self):
        h = self._html()
        assert self._metric(h, "当前 IV") == "48.61%"
        assert "37.8" in self._metric(h, "IV Rank")

    def test_iv_rank_none_is_dash_not_fifty(self):
        v = self._metric(self._html(iv_rank=None), "IV Rank")
        assert "—" in v, f"IV Rank 缺失渲染成 {v!r}"
        assert "50.0" not in v, "50.0（中等 IV）是旧 bug 的指纹"
        assert "中等 IV" not in v, "缺失不该被标成「中等 IV」"

    def test_partial_degradation_passes_quality_gate(self):
        """复刻 8/26：CBOE 正常所以 data_quality='real'，但 yfinance 派生字段为 None。
        全盘不可用闸挡不住这种情况 —— 正是本测试存在的理由。"""
        h = self._html(iv_rank=None, iv_percentile=None)
        assert "期权数据不可用" not in h, "部分降级不该触发全盘跳过"
        assert "—" in self._metric(h, "IV Rank")

    def test_zero_is_not_replaced(self):
        """`or 25` / `or 1.0` 比 `if is None` 更糟：真实的 0 也会被顶掉"""
        assert self._metric(self._html(put_call_ratio=0.0), "Put/Call Ratio") == "0.00"
        assert self._metric(self._html(iv_current=0.0), "当前 IV") == "0.00%"

    def test_all_none_each_dashed_independently(self):
        h = self._html(iv_rank=None, iv_current=None,
                       iv_percentile=None, put_call_ratio=None)
        for lbl in ("IV Rank", "当前 IV", "IV 百分位数", "Put/Call Ratio"):
            assert "—" in self._metric(h, lbl), f"{lbl} 未降级为 —"


# ─────── ⑥ 我自己在 v0.45.42 引入的回归（v0.45.43 修）───────

class TestEquityCurveSurvivesNullSpy:
    """v0.45.42 把 `_spy` 改成可为 None，却漏了 `round(_spy, 2)` 这个消费点。
    结果：TypeError → 被 `except ... _log.debug` 整块吞掉 → equity_curve 与
    trading_stats["realistic"] 全部不生成。而 _trading_stats 预置了
    total_spy_ret=0.0 / alpha_vs_spy=0.0，页面照常渲染出「大盘 0%、无超额」。

    教训（也是本文件的主题）：把一个值改成可 None，必须把它的**每个**
    消费点都找出来 —— 漏掉一个，就是一次新的静默降级。
    """

    def test_round_guarded_at_spy_ret(self):
        import inspect
        import dashboard_renderer
        src = inspect.getsource(dashboard_renderer)
        assert '"spy_ret": round(_spy, 2),' not in src, \
            "_spy 可为 None，裸 round() 会抛 TypeError 并被整块吞掉"
        assert '"spy_ret": (round(_spy, 2) if _spy is not None else None)' in src

    def test_equity_failure_is_not_debug_level(self):
        """吞掉这条异常的 except 必须是 warning —— 它让 bug 隐身了三次重跑"""
        import inspect
        import dashboard_renderer
        src = inspect.getsource(dashboard_renderer)
        assert '_log.debug("Equity curve 数据加载失败' not in src, \
            "equity 计算失败必须 warning，不能 debug"
        assert "Equity curve / trading_stats 计算失败" in src

    def test_defaults_are_none_not_zero(self):
        """预置默认值 0.0 会冒充真实结果 —— 必须是 None"""
        import inspect
        import dashboard_renderer
        src = inspect.getsource(dashboard_renderer)
        assert '"total_spy_ret": 0.0, "alpha_vs_spy": 0.0,' not in src
        assert '"total_spy_ret": None, "alpha_vs_spy": None,' in src

    def test_js_upper_bound_branch_no_coercion(self):
        """realistic 缺失时实际渲染的是「理论上限口径」分支 —— 它也不许把 null 变 0"""
        from pathlib import Path
        js = Path("templates/dashboard.js").read_text()
        assert "Math.round(ts.final_cap_spy||initCap)" not in js
        assert "(ts.alpha_vs_spy||0).toFixed(2)" not in js
        assert "spyAvailT" in js and "alphaAvailT" in js
