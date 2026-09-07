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
#
# 夹具（v0.45.157）：这一节原先靠「生产 pheromone.db 存在」才跑得起来，
# 病灶见 `TestSPYBenchmarkUnavailable` 的 docstring。现在两个外部依赖
# —— 预测库与 SPY 报价 —— 都由测试自己构造。

#: 四条已验证 T+7 预测。`bullish` 且 score >= min_score_bull(5.5) ⇒ 真的会建仓，
#: 不是「跑通了但一笔没进」。日期全部取自 `is_trading_day` 认可的交易日，
#: 否则 `exclude_nontrading_days=True` 会把它们全滤掉、又退回「无已验证预测数据」。
_FIXTURE_PREDICTIONS = (
    # date          ticker  direction  score  price  ret_t7  net_t7  exit_date     exit_px  days
    ("2026-06-01", "AAA", "bullish", 6.5, 100.0, 2.0, 1.6, "2026-06-08", 102.0, 7),
    ("2026-06-02", "BBB", "bullish", 7.0, 200.0, -1.5, -1.9, "2026-06-09", 197.0, 7),
    ("2026-06-03", "CCC", "bearish", 4.0, 50.0, -3.0, 2.4, "2026-06-10", 48.5, 7),
    ("2026-06-04", "DDD", "bullish", 6.0, 80.0, 0.5, 0.1, "2026-06-11", 80.4, 7),
)


def _seed_predictions_db(path):
    """建一个夹具 `pheromone.db`，**建表 DDL 取自生产 `PredictionStore`**，不手抄。

    `predictions` 有 40+ 列、经过多轮 `_migrate_options_columns` 演进；在测试里
    手抄一份 schema 等于给它冻一个快照，生产加列后两边静默漂移。用生产的建表器
    就永远不会。
    """
    import json
    import sqlite3

    from backtester import PredictionStore

    PredictionStore(db_path=str(path))          # ← 生产 DDL 建表

    dims = json.dumps({"signal": 6.0, "catalyst": 6.5, "sentiment": 6.2,
                       "odds": 5.8, "risk_adj": 6.1})
    with sqlite3.connect(str(path)) as conn:
        conn.executemany(
            "INSERT INTO predictions ("
            "  date, ticker, direction, final_score, price_at_predict,"
            "  return_t7, net_return_t7, exit_date, exit_price, holding_days,"
            "  exit_reason, cost_breakdown, spy_return_t7, dimension_scores, checked_t7"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,'T7_CLOSE','{}',0.0,?,1)",
            [(d, t, dr, sc, px, rt, nrt, ed, ep, hd, dims)
             for d, t, dr, sc, px, rt, nrt, ed, ep, hd in _FIXTURE_PREDICTIONS],
        )
        conn.commit()
    return path


#: 夹具 SPY 序列：收盘价 = `_SPY_BASE + i * _SPY_STEP`（i = 第几个工作日）。
#: 下面几条测试里的价格常数都由这两个数逐位算出，不是随手挑的。
_SPY_BASE = 400.0
_SPY_STEP = 0.5


def _synthetic_spy(start_date: str, end_date: str) -> dict:
    """`_fetch_spy_prices` 的确定性替身。

    ⚠️ 这**不是** conftest 那批「取不到」源桩：本节要验的正例恰恰是「报价拿得到
    时基准可用」，桩必须给出一个**真实形态**的序列，不是空 dict。所以它照抄真源
    的三条形态约束：

      · 区间 `[start-60d, end+10d]` —— 真源多拉 60 天供 MA 用
      · **只含工作日**              —— 真源只返回交易日；`_nearest_close` 的
                                       「找最近交易日」两个分支靠这个才有意义
      · 单调微涨                    —— 让 `_is_risk_off` 恒 False，宏观门控
                                       （`macro_gate` 默认 True）不至于把看多样本
                                       全拦掉，回测才真的建了仓

    单调 ⇒ 起止价确定 ⇒ `spy_return_pct` 可以被**精确**断言，而不只是「非 None」。
    """
    from datetime import datetime, timedelta

    day = datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=60)
    stop = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=10)
    out, i = {}, 0
    while day <= stop:
        if day.weekday() < 5:
            out[day.strftime("%Y-%m-%d")] = _SPY_BASE + i * _SPY_STEP
            i += 1
        day += timedelta(days=1)
    return out


class TestSPYBenchmarkUnavailable:
    """v0.45.157：从「有生产库才跑」改成「夹具库 + 确定性 SPY 桩」。

    这三条此前挂着**两层** skip —— `PROD_DB.exists()`（v0.45.117 加）与
    `if "error" in r: pytest.skip(...)`。而 `pheromone.db` 命中 `.gitignore` 的
    `*.db`，**任何干净检出与 CI runner 上都不存在它**。三种环境各卡在不同一层：

      · 干净检出 / CI   → 第一层 skip（库不存在），永不执行
      · 多数 worktree   → 库存在，但那是别的测试写出来的空壳（0 行 predictions），
                          第二层 skip，同样不执行
      · 只有主 checkout → 两层都过，真跑 —— 然后 `_fetch_spy_prices` 打 yahoo 外网，
                          被 conftest 的离线闸判红

    也就是说：**这条测试唯一会执行的地方，正是它唯一会失败的地方。**

    判据（已写进 CLAUDE.md）：写 `if not X.exists(): pytest.skip()` 之前先问一句
    **「X 在哪些环境里存在？」** 若答案是「只有一台机器上的一个目录」，那这条测试
    等于没有 —— 加一个 skip 守卫与让测试彻底跑不到之间，只隔着一个未被 git
    跟踪的文件。同族见 v0.45.114「跳过缺失项＝把缺失渲染成不存在」。

    修法：两个外部依赖都由测试构造 —— 预测库用生产 DDL 现建（`_seed_predictions_db`），
    SPY 报价用确定性桩（`_synthetic_spy`）。于是两层 skip **全部升级为断言**：
    依赖既然由本测试构造，构造坏了就必须变红，不能再退回「跳过」。
    真 `_fetch_spy_prices` 那条网络路径另见 `TestRealSPYFetchContract`
    （`@pytest.mark.integration`，显式 opt-in）。
    """

    @pytest.fixture(autouse=True)
    def _fixture_backtest(self, tmp_path, monkeypatch):
        import portfolio_backtest as pb
        db = _seed_predictions_db(tmp_path / "pheromone.db")
        monkeypatch.setattr(pb, "_find_db", lambda: db)
        monkeypatch.setattr(pb, "_fetch_spy_prices", _synthetic_spy)
        # 正面核对桩真的接上了（CLAUDE.md：断言要成对 —— 「没读生产库」必须配
        # 「确实读到了夹具库」）。少了这句，将来 `_find_db` 改名或被内联，夹具会
        # 静默失效、测试悄悄退回读生产库 —— 正是本次要修的那个形状本身。
        loaded = pb.load_verified_predictions(horizon=7)
        assert len(loaded) == len(_FIXTURE_PREDICTIONS), \
            f"夹具预测库没接上（读到 {len(loaded)} 条）"
        assert {p["ticker"] for p in loaded} == {r[1] for r in _FIXTURE_PREDICTIONS}, \
            "读到的不是夹具库 —— `_find_db` 的桩没打中"

    def test_empty_prices_yields_none_not_zero(self, monkeypatch):
        import portfolio_backtest as pb
        monkeypatch.setattr(pb, "_fetch_spy_prices", lambda *a, **k: {})
        r = pb.run_backtest(pb.BacktestConfig(exclude_nontrading_days=True))
        assert "error" not in r, f"夹具应当让回测跑得起来，实得 {r.get('error')!r}"
        b = r["benchmark"]
        assert b["spy_return_pct"] is None, "取数失败必须给 None，不能给 0"
        assert b["available"] is False
        assert r["alpha"] is None, "没有基准就没有 Alpha —— 不能拿组合收益冒充"

    def test_available_when_prices_present(self):
        import portfolio_backtest as pb
        r = pb.run_backtest(pb.BacktestConfig(exclude_nontrading_days=True))
        assert "error" not in r, f"夹具应当让回测跑得起来，实得 {r.get('error')!r}"
        b = r["benchmark"]
        assert b["available"] is True, "夹具给了完整报价，基准必须可用"
        assert b["spy_return_pct"] is not None
        assert r["alpha"] is not None

        # 桩是确定性的 ⇒ 这里断言**数值**，不再只是「非 None」。
        # 查询两端 first=2026-06-01 / last=2026-06-11（= 最晚 exit_date），
        # 二者都在序列内，按 `_SPY_BASE + i*_SPY_STEP` 逐位可算。
        assert b["spy_start_price"] == 421.0
        assert b["spy_end_price"] == 425.0
        assert b["spy_return_pct"] == pytest.approx(0.95, abs=0.005)

        # 本文件的主题：alpha 必须**由基准导出**，不能是组合收益本身。
        # 旧 bug 的指纹正是 alpha == total_return_pct（SPY 被当成 0%）。
        total = r["portfolio"]["total_return_pct"]
        assert r["alpha"] == pytest.approx(total - b["spy_return_pct"], abs=0.011)
        assert r["alpha"] != total, "SPY 涨了 0.95%，alpha 不该等于组合收益本身"

    def test_nontrading_boundary_still_resolves(self, monkeypatch):
        """起止日恰逢非交易日时取最近交易日，而不是退化成 0。

        ⚠️ 这条此前是**空转**的：它抹掉的是报价序列首尾各 3 天，而序列首端在
        `first_date - 60d`（真源就是这么拉的），离被查的 `first_date` 有 60 天，
        `_nearest_close` 根本走不到「找最近交易日」那两个分支。改为抹掉
        **被查的那两个日期本身及其邻近**，才真的把查询推到非交易日上；
        并在挖洞后**前置核对洞确实落在查询点上**，免得再空转一次。
        """
        import portfolio_backtest as pb

        first, last = "2026-06-01", "2026-06-11"        # run_backtest 会查的两端

        def _holes_at_query_dates(start_date, end_date):
            px = _synthetic_spy(start_date, end_date)
            for d in ("2026-05-28", "2026-05-29", "2026-06-01",
                      "2026-06-11", "2026-06-12", "2026-06-15"):
                px.pop(d, None)                          # 模拟长周末 + 假日连休
            return px

        full = _synthetic_spy(first, last)
        assert first in full and last in full, "夹具序列本应含这两个查询日"
        holed = _holes_at_query_dates(first, last)
        assert first not in holed and last not in holed, "洞没挖在查询日上 —— 本条会空转"

        monkeypatch.setattr(pb, "_fetch_spy_prices", _holes_at_query_dates)
        r = pb.run_backtest(pb.BacktestConfig(exclude_nontrading_days=True))
        assert "error" not in r, f"夹具应当让回测跑得起来，实得 {r.get('error')!r}"
        b = r["benchmark"]
        assert b["available"] is True, "起止日落在非交易日不该让基准整体不可用"
        # 落到邻近交易日：first 往后找到 06-02（421.5），last 往前找到 06-10（424.5）
        assert b["spy_start_price"] == 421.5, "first_date 缺失应取其后最近的交易日"
        assert b["spy_end_price"] == 424.5, "last_date 缺失应取其前最近的交易日"
        assert b["spy_return_pct"] == pytest.approx(0.71, abs=0.005)


@pytest.mark.integration
class TestRealSPYFetchContract:
    """真 `_fetch_spy_prices`（打真外网）—— 显式 opt-in：`-m integration`。

    上面那组用桩把 SPY 报价换掉了，所以**没有任何离线测试再执行真取数函数**；
    `TestSPYFetchGuarded` 只做静态源码核对（有没有走闸门、有没有重试），
    证不了它真跑起来是什么形状。这条补上那一段，验的是下游依赖的**契约本身**：
    要么给 `{date: float}`，要么给 `{}`，中间没有第三种形态，且**永不返回 0 价**。

    与上一版被删掉的那两层 skip 的区别：这里的条件性写在 marker 上、由命令行
    显式选中（CI 跑的是 `-m "not integration and not network"`），
    而不是藏在一条默认执行时静默跳过的 skip 里。前者可见，后者不可见 ——
    这正是本次要治的分别。
    """

    def test_real_fetch_returns_usable_prices(self):
        import portfolio_backtest as pb
        px = pb._fetch_spy_prices("2026-06-01", "2026-06-11")
        assert isinstance(px, dict)

        # ⚠️ 这里**刻意不写** `if not px: pytest.skip(...)`。空 dict 确实是契约内的
        # 失败形态，但本条测试的意图是「对接外部系统这件事本身还成立吗」——
        # 取数长期挂掉时若跳过，就没有任何东西会红，那正是本版要治的形状。
        # 它被 addopts 默认排除，只有人显式 `-m integration` 才跑；跑的人想知道的
        # 就是「现在还通不通」，所以取不到就该判红，不是跳过。
        assert px, "真 SPY 取数返回空 —— 对接断了（这条测试的意图就是判它）"

        for d, v in px.items():
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", d), f"键不是日期：{d!r}"
            assert isinstance(v, float) and v > 0, f"{d} 的收盘价是 {v!r} —— 0/负价必须不存在"


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
