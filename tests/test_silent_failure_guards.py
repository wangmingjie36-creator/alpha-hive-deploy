"""
三条静默失败路径的回归闸（v0.45.2）

共同形状：**没有报错、退出码为 0、日志正常，但产出早已是假的**。
三者都是在 2026-08-24 跑后核对里被翻出来的，且都存活了很久：

  · ticker 正则 `^[A-Z]{1,5}$` 拒绝 BRK-B → 8 只蜂里 7 只提前返回
    score=5.0/confidence=0.0，日报照常印 "BRK-B NEUTRAL 5.0"，看不出它
    从未被分析过（至少可追溯到 2026-08-11 的日报）。
  · cache.py fallback 把 momentum_5d 初始化为 0.0 → 取数残缺时伪装成
    "持平"，且不置 _data_unavailable，与 v0.43.25 在 ScoutBee 侧拆掉的
    是同一形状，只是搬到了上游。
  · `git commit-tree` 是管道命令，不做 `git commit` 的"无变更则拒绝"
    检查 → 2026-08-15 连续三条 `Deploy: ML reports (12 tickers)` 全是
    0 文件空提交，message 里的数字是声称值而非实测值。

每条测试都必须能在"把修复回退掉"时变红——这是本文件存在的唯一理由。
"""

import subprocess
import sys

import pytest


# ─────────────────────────── ① ticker 正则 ───────────────────────────

class TestTickerFormatAcceptsClassShares:
    """类份额后缀（BRK-B / BRK.B / BF-B）必须被当作合法 ticker。"""

    @pytest.mark.parametrize("ticker", ["BRK-B", "BRK.B", "BF-B", "NVDA", "T", "GOOGL", "A"])
    def test_valid_tickers_accepted(self, ticker):
        from swarm_agents._config import _RE_TICKER
        assert _RE_TICKER.match(ticker), f"{ticker} 应被接受"

    @pytest.mark.parametrize("ticker", [
        "", "brk-b", "TOOLONGX", "BRK-BB", "BRK--B", "../etc", "BRK-", "-B", "BRK B", "NV*DA",
    ])
    def test_invalid_tickers_still_rejected(self, ticker):
        """放宽后仍须挡住注入/路径穿越形状的输入。"""
        from swarm_agents._config import _RE_TICKER
        assert not _RE_TICKER.match(ticker), f"{ticker} 不应被接受"

    def test_agent_does_not_short_circuit_on_brk_b(self):
        """_validate_ticker 返回 None 才表示放行；返回 dict 就是那条静默中性化路径。"""
        from swarm_agents.base import BeeAgent

        class _Probe(BeeAgent):              # BeeAgent 是 ABC，需一个具体子类
            def analyze(self, ticker):
                return {}

        agent = _Probe.__new__(_Probe)       # 不跑 __init__，只测校验逻辑
        assert agent._validate_ticker("BRK-B") is None
        rejected = agent._validate_ticker("../etc")
        assert rejected is not None and rejected["error"] == "invalid_ticker"

    def test_every_configured_ticker_passes_validation(self):
        """配置里的标的必须全部能过校验，否则又是一只静默 5.0。"""
        from swarm_agents._config import _RE_TICKER
        from config import WATCHLIST, WATCHLIST_EXTENDED
        bad = [t for t in {**WATCHLIST, **WATCHLIST_EXTENDED} if not _RE_TICKER.match(t)]
        assert not bad, f"这些标的会被静默中性化: {bad}"


# ─────────────────────── ② momentum_5d 诚实缺数据 ───────────────────────

class TestMomentumMissingIsNoneNotZero:
    """取不到 5 日动量时必须是 None（诚实缺数据），不能是 0.0（伪装持平）。"""

    def test_cache_fallback_returns_none_momentum(self, monkeypatch):
        import swarm_agents.cache as cache
        # 强制走 data_pipeline 不可用的保守 fallback 分支
        monkeypatch.setitem(sys.modules, "data_pipeline", None)
        monkeypatch.setattr(cache.yfinance_breaker, "allow_request", lambda: False)
        with cache._yf_lock:
            cache._yf_cache.pop("ZZTEST", None)
            cache._yf_cache_ts.pop("ZZTEST", None)

        data = cache._fetch_stock_data("ZZTEST")

        assert data["_data_unavailable"] is True
        assert data["momentum_5d"] is None, (
            "取数失败却给出 0.0，下游无法区分『真持平』和『没数据』"
        )

    def test_divergence_reports_unavailable_when_momentum_missing(self):
        """momentum 缺失时是 'unavailable'（查不了），不是 'none'（查过没背离）。"""
        from swarm_agents.sentiment import _detect_sentiment_price_divergence as detect
        r = detect(70, None, "TEST")
        assert r["divergence_type"] == "unavailable"
        assert r["score_adj"] == 0.0, "缺数据不得影响评分"

    def test_divergence_still_works_with_real_momentum(self):
        from swarm_agents.sentiment import _detect_sentiment_price_divergence as detect
        assert detect(70, -5.0, "TEST")["divergence_type"] == "bull_trap"
        assert detect(30, +5.0, "TEST")["divergence_type"] == "hidden_opportunity"
        assert detect(70, 0.0, "TEST")["divergence_type"] == "none"

    def test_buzz_bee_passes_none_through(self):
        """回归闸：调用方若再把 None 换成 0.0，'unavailable' 分支会重新变成死代码。"""
        import inspect
        from swarm_agents import buzz_bee
        src = inspect.getsource(buzz_bee)
        assert "_mom_raw if _mom_raw is not None else 0.0" not in src, (
            "BuzzBee 又把 None 伪造成 0.0 了"
        )


# ────────────────────── ③ gh-pages 空提交闸 ──────────────────────

def _git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo).decode().strip()


@pytest.fixture
def tiny_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("v1\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "c1")
    return repo


class TestGhpagesTreeDelta:
    def test_identical_tree_reports_no_change(self, tiny_repo):
        """这正是 2026-08-15 那三条空提交的形状。"""
        from report_deployer import ghpages_tree_delta
        head = _git(tiny_repo, "rev-parse", "HEAD")
        tree = _git(tiny_repo, "rev-parse", "HEAD^{tree}")
        changed, n = ghpages_tree_delta(str(tiny_repo), tree, head)
        assert changed is False and n == 0

    def test_real_change_is_counted(self, tiny_repo):
        from report_deployer import ghpages_tree_delta
        head = _git(tiny_repo, "rev-parse", "HEAD")
        (tiny_repo / "a.txt").write_text("v2\n")
        (tiny_repo / "b.txt").write_text("new\n")
        _git(tiny_repo, "add", "-A")
        _git(tiny_repo, "commit", "-qm", "c2")
        tree2 = _git(tiny_repo, "rev-parse", "HEAD^{tree}")
        changed, n = ghpages_tree_delta(str(tiny_repo), tree2, head)
        assert changed is True and n == 2, "变更文件数必须是实测值"

    def test_missing_parent_fails_open(self, tiny_repo):
        """无法判定时返回 (True, -1)：宁可多提交一次，不可阻断部署。"""
        from report_deployer import ghpages_tree_delta
        tree = _git(tiny_repo, "rev-parse", "HEAD^{tree}")
        assert ghpages_tree_delta(str(tiny_repo), tree, None) == (True, -1)
        assert ghpages_tree_delta(str(tiny_repo), tree, "deadbeef") == (True, -1)


# ══════════════════════════════════════════════════════════════════
# v0.45.3：把「缺数据渲染成安全值」的剩余六处一并堵上
#
# 这一批与上面三条同源，但危险程度不同：`volatility_20d = 0.0` 喂进风险引擎
# 时 σ=0 ⇒ VaR 恒为 0 ⇒ 风险面板显示「🟢 低」。**把「没查到」渲染成
# 「没问题」比崩溃危险得多**——崩溃至少会报错。
# ══════════════════════════════════════════════════════════════════


class TestDataclassDefaultsAreHonest:
    """StockData 的默认值必须是"未知"，不是"持平 / 正常量 / 零波动"。"""

    def test_bare_stockdata_has_no_fabricated_numbers(self):
        from data_pipeline import StockData
        d = StockData(price=10.0).to_dict()
        assert d["momentum_5d"] is None
        assert d["volume_ratio"] is None
        assert d["volatility_20d"] is None, "σ=0.0 在风险引擎里等于『零风险』"

    def test_cache_fallback_has_no_fabricated_numbers(self, monkeypatch):
        import swarm_agents.cache as cache
        monkeypatch.setitem(sys.modules, "data_pipeline", None)
        monkeypatch.setattr(cache.yfinance_breaker, "allow_request", lambda: False)
        with cache._yf_lock:
            cache._yf_cache.pop("ZZTEST2", None)
            cache._yf_cache_ts.pop("ZZTEST2", None)
        data = cache._fetch_stock_data("ZZTEST2")
        assert data["momentum_5d"] is None
        assert data["volume_ratio"] is None
        assert data["volatility_20d"] is None


class TestRiskEngineRefusesWithoutSigma:
    """σ 未知时 VaR 在数学上没有意义——既不崩也不编，拒绝出数字。"""

    @staticmethod
    def _sd(**over):
        base = {"price": 100.0, "volatility_20d": 30.0,
                "momentum_5d": 2.0, "volume_ratio": 1.0}
        base.update(over)
        return base

    def test_parametric_var_refuses_on_none_sigma(self):
        from risk_engine import parametric_var
        r = parametric_var(self._sd(volatility_20d=None), horizon_days=7)
        assert "error" in r and "volatility_20d" in r["error"]

    def test_parametric_var_refuses_on_non_numeric_sigma(self):
        from risk_engine import parametric_var
        r = parametric_var(self._sd(volatility_20d="n/a"), horizon_days=7)
        assert "error" in r

    def test_parametric_var_still_computes_normally(self):
        from risk_engine import parametric_var
        r = parametric_var(self._sd(), horizon_days=7)
        assert "error" not in r

    def test_none_momentum_does_not_crash(self):
        """动量缺失不该阻断 VaR——它只影响漂移项，不像 σ 那样使结果无意义。"""
        from risk_engine import parametric_var
        r = parametric_var(self._sd(momentum_5d=None), horizon_days=7)
        assert "error" not in r

    def test_style_returns_unknown_and_does_not_keyerror(self):
        """回归闸：`sens_map[style]` 曾是裸下标，'unknown' 会 KeyError。"""
        from risk_engine import _classify_growth_value
        assert _classify_growth_value(self._sd(volatility_20d=None)) == "unknown"
        assert _classify_growth_value(self._sd()) in ("growth", "value", "blend")


class TestMLFeatureImputationIsDeclared:
    """插补本身没问题（0.5 是本模型精确的无观点点），**沉默**才有问题。"""

    @staticmethod
    def _td(**over):
        import dataclasses
        from ml_predictor import TrainingData
        base = dict(ticker="TEST", crowding_score=40.0, catalyst_quality="B",
                    momentum_5d=2.0, volatility=8.0, market_sentiment=50,
                    iv_rank=40.0, put_call_ratio=1.0, final_score=6.0,
                    odds_score=5.0, risk_adj_score=5.0, agent_agreement=0.6,
                    direction_encoded=1)
        names = {f.name for f in dataclasses.fields(TrainingData)}
        kw = {k: v for k, v in base.items() if k in names}
        for f in dataclasses.fields(TrainingData):
            if (f.name not in kw and f.default is dataclasses.MISSING
                    and f.default_factory is dataclasses.MISSING):
                kw[f.name] = 0.0
        kw.update(over)
        return TrainingData(**kw)

    def test_normalize_feature_survives_none_and_nan(self):
        from ml_predictor import SimpleMLModel, _FEATURE_NEUTRAL
        m = SimpleMLModel()
        assert m.normalize_feature(None, 0, 10) == _FEATURE_NEUTRAL
        assert m.normalize_feature(float("nan"), 0, 10) == _FEATURE_NEUTRAL

    def test_normalize_feature_math_unchanged(self):
        """插补不能顺手改坏正常路径。"""
        from ml_predictor import SimpleMLModel
        m = SimpleMLModel()
        assert m.normalize_feature(3, 0, 10) == pytest.approx(0.3)
        assert m.normalize_feature(8, 0, 10) == pytest.approx(0.8)

    def test_neutral_value_is_actually_neutral(self):
        """0.5 之所以能当无观点点，是因为 centered_feature 对它恒等。"""
        from ml_predictor import centered_feature, _FEATURE_NEUTRAL
        for influence in (0.3, 0.5, 0.7, 1.0):
            for inverse in (False, True):
                assert centered_feature(_FEATURE_NEUTRAL, influence, inverse) == 0.5

    def test_missing_features_are_named(self):
        from ml_predictor import _missing_features
        assert _missing_features(self._td()) == []
        assert _missing_features(self._td(momentum_5d=None)) == ["momentum"]
        assert set(_missing_features(self._td(momentum_5d=None, iv_rank=None))) \
            == {"momentum", "iv_rank"}

    def test_quality_fields_ride_along_with_the_prediction(self):
        from ml_predictor import _feature_quality
        q = _feature_quality(self._td(momentum_5d=None))
        assert q["feature_completeness"] == "11/12"
        assert q["imputed_features"] == ["momentum"]
        assert q["unreliable"] is False

    def test_too_many_missing_marks_unreliable(self):
        from ml_predictor import _feature_quality
        q = _feature_quality(self._td(momentum_5d=None, volatility=None, iv_rank=None,
                                      put_call_ratio=None, odds_score=None))
        assert q["unreliable"] is True

    def test_recommendation_refuses_when_unreliable(self):
        """插补值全中性 → 概率被推向 0.5 → 稳定落进 HOLD，读起来像真实判断。"""
        from ml_predictor import MLPredictionService as MLPredictor
        rec = MLPredictor._generate_recommendation(
            None, {"probability": 0.55, "unreliable": True,
                   "imputed_features": ["a", "b", "c", "d", "e"]})
        assert "NO CALL" in rec
        ok = MLPredictor._generate_recommendation(
            None, {"probability": 0.80, "unreliable": False, "imputed_features": []})
        assert "NO CALL" not in ok


class TestPromptRenderingDoesNotLieToTheModel:
    """喂给 LLM 的假事实会被它当前提推理——缺失必须写"不可得"，不是 0。"""

    def test_none_renders_as_unavailable_not_zero(self):
        from llm_service import _fmt_num
        assert _fmt_num(None) == "不可得"
        assert _fmt_num(None, ".1f") == "不可得"
        assert _fmt_num("n/a") == "不可得"

    def test_real_numbers_still_render(self):
        from llm_service import _fmt_num
        assert _fmt_num(2.345) == "+2.3%"
        assert _fmt_num(28.7, ".1f") == "28.7%"

    def test_prompt_templates_no_longer_format_raw_get(self):
        """回归闸：`.get(k, 0)` 接不住 None，format(None, spec) 直接 TypeError。"""
        import inspect
        import llm_service
        src = inspect.getsource(llm_service)
        assert "{stock_data.get('momentum_5d', 0):+.1f}%" not in src
        assert "{stock_data.get('volatility_20d', 0):.1f}%" not in src


class TestCollectDataEmitsNullNotZero:
    def test_none_becomes_json_null(self):
        """回归闸：`round(float(sdet.get(k, 0)), n)` 在值为 None 时 TypeError。"""
        import inspect
        import collect_data
        src = inspect.getsource(collect_data.extract_raw)
        assert 'round(float(sdet.get("momentum_5d", 0)), 4)' not in src
        assert 'round(float(sdet.get("crowding_score", 0)), 1)' not in src


class TestAgentsSurviveFullyDegradedData:
    """上游诚实化必须**同一批**改下游——否则 None 只是把崩溃点搬了个家。

    实测教训（2026-08-25 自动扫描）：v0.45.3 把 `volatility_20d` 的默认值从
    0.0 改成 None 后，`buzz_bee.py` 的 `vol20 > _vlt.get("extreme", 60)`
    当场 TypeError，**30/30 只标的的 BuzzBee 全崩**、BearBee 28 只。
    单元测试当时全绿——因为没有一条测试喂过"全部字段皆缺"的数据。
    """

    FULLY_DEGRADED = {
        "price": 100.0, "momentum_5d": None, "volume_ratio": None,
        "volatility_20d": None, "avg_volume": 0, "data_source": "fallback",
    }

    @pytest.mark.parametrize("agent_path,cls_name", [
        ("swarm_agents.buzz_bee", "BuzzBeeWhisper"),
        ("swarm_agents.bear_bee", "BearBeeContrarian"),
        ("swarm_agents.rival_bee", "RivalBeeVanguard"),
    ])
    def test_agent_does_not_crash_on_all_none_metrics(self, agent_path, cls_name):
        import importlib
        from pheromone_board import PheromoneBoard
        cls = getattr(importlib.import_module(agent_path), cls_name)
        agent = cls(PheromoneBoard())
        agent._get_stock_data = lambda _t: dict(self.FULLY_DEGRADED)
        result = agent.analyze("NVDA")
        assert result.get("error") != "agent_failure", f"{cls_name} 崩了"
        assert "failed" not in str(result.get("discovery", ""))
        assert result.get("score") is not None


class TestNoTickerMayBeDropped:
    """「一只都不能丢」（v0.45.4）。

    实测丢失史：2026-08-12 丢 7 只、08-13 丢 4 只、08-25 丢 2 只（COST/DE），
    每次都只有一条 WARNING，编排器照常打印「✅ 所有步骤成功」——因为唯一的
    数量提示 `扫描 ${#TICKERS[@]} 只` 取自**配置数组长度**，从不与实际产出比对。

    这些是结构性断言而非行为测试：触发路径需要一次完整蜂群扫描（约 20 分钟 +
    大量外网请求），没法在单测里跑。它们的作用是**当有人把重试或闸门删掉时变红**。
    """

    @staticmethod
    def _src():
        import inspect
        import alpha_hive_daily_report
        return inspect.getsource(alpha_hive_daily_report)

    def test_failed_tickers_are_retried(self):
        src = self._src()
        assert "_MAX_TICKER_ATTEMPTS" in src, "失败标的必须重试——AST SystemError 是竞态偶发"
        assert "♻️" in src or "重试" in src

    def test_failure_log_keeps_the_stack(self):
        """回归闸：此前只记 str(e)，拿不到栈就无法定位 AST SystemError。"""
        src = self._src()
        idx = src.find("并行分析失败")
        assert idx > 0
        assert "exc_info=True" in src[idx - 200:idx + 200], "这条 warning 必须带 exc_info"

    def test_completeness_gate_exists(self):
        """打算扫的每一只都必须有结果，缺一只就要刺眼。"""
        src = self._src()
        assert "标的丢失" in src, "缺标的必须打 ERROR"
        assert "标的完整性" in src, "齐了也要有正面记录，否则无从区分『没跑』和『跑了没丢』"

    def test_retry_set_comes_from_actual_output(self):
        """重试集合必须查实际产出，不能靠异常记账。

        二次检查抓到的：按异常记账有两个缺口——
        ① `future.result(timeout=)` 抛超时时任务仍在池里跑，会重复分析；
        ② `_analyze_and_save` 返回空但不抛异常时，会被漏掉。
        """
        src = self._src()
        assert "_pending_again" in src, "重试集合应由查 swarm_results 的函数决定"
        # 硬闸也必须走同一个真相源，否则两处口径会漂移
        assert "missing = [t for _, t in _pending_again()]" in src

    def test_orchestrator_compares_intended_vs_actual(self):
        """编排器不能再拿配置数组长度冒充实际产出。"""
        import pathlib
        sh = pathlib.Path.home() / ".claude/scripts/alpha-hive-orchestrator.sh"
        if not sh.exists():
            pytest.skip("编排器脚本不在本机")
        text = sh.read_text(encoding="utf-8")
        assert "ticker_completeness" in text
        assert "ACTUAL_COUNT" in text and "INTENDED_COUNT" in text


class TestDeployWhitelistAcceptsClassShares:
    """部署白名单也必须认类份额后缀（v0.45.5）。

    v0.45.2 修好 ticker 正则后 BRK-B 终于产出 ML 报告，却**从不被部署**：
    白名单 `^alpha-hive-\\w+-ml-enhanced-...` 里的 `\\w` 不含连字符。
    而 index.html 照常链接它 —— 线上直接 404。
    同一个坑在一天之内出现在两层：Agent 校验层（v0.45.2）与部署层（v0.45.5）。
    """

    ML_FILES_OK = [
        "alpha-hive-NVDA-ml-enhanced-2026-08-25.html",
        "alpha-hive-BRK-B-ml-enhanced-2026-08-25.html",
        "alpha-hive-BRK.B-ml-enhanced-2026-08-25.html",
        "alpha-hive-T-ml-enhanced-2026-08-25.html",
    ]
    ML_FILES_BAD = [
        "alpha-hive-NVDA-ml-enhanced-2026-08-25.html.bak",
        "evil-alpha-hive-X-ml-enhanced-2026-08-25.html",
        "alpha-hive-NVDA-ml-enhanced-2026-8-25.html",
        "alpha-hive-NVDA-deep-2026-08-25.html",
    ]

    @staticmethod
    def _patterns():
        """从生产代码里抓正则，不在测试里重抄一份——否则测试测的是自己。"""
        import re
        import pathlib
        pats = []
        for name in ("report_deployer.py", "generate_ml_report.py"):
            src = pathlib.Path(name).read_text(encoding="utf-8")
            m = re.search(r'r"(\^alpha-hive-.+?-ml-enhanced-.+?\$)"', src)
            assert m, f"{name}: 找不到 ML 白名单正则"
            pats.append((name, re.compile(m.group(1))))
        return pats

    def test_class_share_reports_are_deployable(self):
        for name, pat in self._patterns():
            for f in self.ML_FILES_OK:
                assert pat.match(f), f"{name} 会漏掉 {f}"

    def test_non_report_files_still_excluded(self):
        """放宽字符集不能顺手把备份/伪造文件放进部署。"""
        for name, pat in self._patterns():
            for f in self.ML_FILES_BAD:
                assert not pat.match(f), f"{name} 误收 {f}"
