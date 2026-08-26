"""
ic_diagnostics 测试（v0.42.6）

重点锁死两件事：
  1. 统计函数正确（spearman / Newey-West / 不重叠取样）
  2. **T+30 必须按月取样**——按周取样仍重叠 ~4 倍，实测会把 risk_adj 的
     t 从真实的 −1.09 抬到 −3.28（看起来比 T+7 还显著），是最危险的误读
"""

import datetime
import json
import math
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ic_diagnostics as icd


class TestSpearman:
    def test_perfect_positive(self):
        assert icd.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)

    def test_perfect_negative(self):
        assert icd.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_monotone_not_linear(self):
        """秩相关只看序，非线性单调变换应仍为 1"""
        assert icd.spearman([1, 2, 3, 4], [1, 4, 9, 16]) == pytest.approx(1.0)

    def test_ties_use_average_rank(self):
        c = icd.spearman([1, 1, 2, 2], [1, 2, 3, 4])
        assert c is not None and 0 < c < 1

    def test_constant_input_returns_none(self):
        assert icd.spearman([5, 5, 5, 5], [1, 2, 3, 4]) is None

    def test_too_few_points(self):
        assert icd.spearman([1], [2]) is None

    @pytest.mark.parametrize("n", [8, 20])
    def test_matches_scipy_when_available(self, n):
        scipy_stats = pytest.importorskip("scipy.stats")
        import random
        rng = random.Random(42)
        x = [rng.random() for _ in range(n)]
        y = [rng.random() for _ in range(n)]
        assert icd.spearman(x, y) == pytest.approx(
            scipy_stats.spearmanr(x, y).correlation, abs=1e-9)


class TestNeweyWest:
    def test_no_autocorrelation_close_to_plain_t(self):
        """无自相关时 NW t 应接近朴素 t"""
        import random
        rng = random.Random(7)
        vals = [rng.gauss(0.1, 1.0) for _ in range(200)]
        _, _, t, _ = icd.basic_stats(vals)
        nw = icd.newey_west_t(vals, lag=7)
        assert abs(nw - t) < 0.5 * abs(t) + 0.5

    def test_positive_autocorrelation_shrinks_t(self):
        """正自相关应把 |t| 拉低（这正是 IC 序列的情形）"""
        vals = []
        prev = 0.0
        import random
        rng = random.Random(11)
        for _ in range(200):
            prev = 0.8 * prev + rng.gauss(0, 1)
            vals.append(prev + 0.5)
        _, _, t, _ = icd.basic_stats(vals)
        nw = icd.newey_west_t(vals, lag=7)
        assert abs(nw) < abs(t), f"NW t={nw:.2f} 未低于朴素 t={t:.2f}"

    def test_too_short_returns_nan(self):
        assert math.isnan(icd.newey_west_t([1.0, 2.0], lag=7))


class TestNonOverlappingSubsample:
    def _series(self, start="2026-03-02", days=60):
        d0 = datetime.date.fromisoformat(start)
        out = {}
        for i in range(days):
            d = d0 + datetime.timedelta(days=i)
            if d.weekday() < 5:
                out[d.isoformat()] = 0.1
        return out

    def test_weekly_picks_one_per_iso_week(self):
        s = self._series()
        sub = icd.subsample_non_overlapping(s, "周")
        weeks = {datetime.date.fromisoformat(d).isocalendar()[:2] for d in s}
        assert len(sub) == len(weeks)

    def test_monthly_picks_one_per_month(self):
        s = self._series(days=120)
        sub = icd.subsample_non_overlapping(s, "月")
        months = {d[:7] for d in s}
        assert len(sub) == len(months)

    def test_monthly_is_strictly_coarser_than_weekly(self):
        """核心：T+30 用月度必须比周度取样更少 —— 周度对 30 天前瞻仍重叠 ~4 倍"""
        s = self._series(days=150)
        assert len(icd.subsample_non_overlapping(s, "月")) < \
               len(icd.subsample_non_overlapping(s, "周"))

    def test_horizon_config_pairs_t30_with_month(self):
        """回归：T+30 的取样周期必须是"月"。改成"周"会把 t 值虚高到误导水平
        （实测 risk_adj 周度 −3.28 vs 真实月度 −1.09）。"""
        assert icd.HORIZONS["t30"][3] == "月"
        assert icd.HORIZONS["t7"][3] == "周"

    def test_t7_uses_gross_return_not_net(self):
        """目标变量必须是 return_t7（毛）。net_return_t7 是方向调整后的损益，
        与"看多度"分数配对在语义上是错的。"""
        assert icd.HORIZONS["t7"][0] == "return_t7"
        assert icd.HORIZONS["t30"][0] == "return_t30"


class TestLoadAndDiagnose:
    @pytest.fixture
    def fake_db(self, tmp_path):
        """构造一个 signal 完美正相关、risk_adj 完美负相关的合成库"""
        db = tmp_path / "p.db"
        con = sqlite3.connect(db)
        # close_t7 是 v0.45.19 起的默认口径（唯一未被路径截断的收益源），
        # fixture 必须带上它，否则默认口径查不到列 → 静默 0 行。
        # 此处刻意让 close_t7 == price_t7，使既有断言（IC=±1）在两种口径下等价，
        # 这组测试因此仍只检验 IC 计算本身，不掺入口径差异。
        con.execute("""CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, ticker TEXT, dimension_scores TEXT,
            price_at_predict REAL, price_t7 REAL, price_t30 REAL, close_t7 REAL,
            return_t7 REAL, checked_t7 INTEGER DEFAULT 0,
            return_t30 REAL, checked_t30 INTEGER DEFAULT 0)""")
        d0 = datetime.date.fromisoformat("2026-03-02")
        n_days = 0
        i = 0
        while n_days < 40:
            d = d0 + datetime.timedelta(days=i)
            i += 1
            if d.weekday() >= 5:
                continue
            n_days += 1
            for j in range(8):
                dims = {"signal": float(j), "catalyst": 5.0,
                        "sentiment": 5.0, "odds": 5.0,
                        "risk_adj": float(8 - j)}
                con.execute(
                    "INSERT INTO predictions (date,ticker,dimension_scores,"
                    "price_at_predict,price_t7,price_t30,close_t7,"
                    "return_t7,checked_t7,return_t30,checked_t30) "
                    "VALUES (?,?,?,?,?,?,?,?,1,?,1)",
                    (d.isoformat(), f"T{j}", json.dumps(dims),
                     100.0, 100.0 + j, 100.0 + j, 100.0 + j,
                     float(j), float(j)))
        con.commit()
        con.close()
        return db

    def test_load_daily_ic_recovers_known_signs(self, fake_db):
        ic, rows, widths = icd.load_daily_ic(fake_db, "return_t7", "checked_t7")
        assert rows == 40 * 8
        assert len(widths) == 40 and set(widths) == {8}
        assert mean_of(ic["signal"]) == pytest.approx(1.0, abs=1e-9)
        assert mean_of(ic["risk_adj"]) == pytest.approx(-1.0, abs=1e-9)

    def test_constant_dimension_is_skipped(self, fake_db):
        """全为常数的维度无法算秩相关，应被跳过而非产生假值"""
        ic, _, _ = icd.load_daily_ic(fake_db, "return_t7", "checked_t7")
        assert ic["catalyst"] == {}

    def test_min_width_filters_days(self, fake_db):
        ic, _, widths = icd.load_daily_ic(fake_db, "return_t7", "checked_t7",
                                          min_width=9)
        assert widths == [] and ic["signal"] == {}

    def test_perfect_correlation_yields_undefined_t(self, fake_db):
        """IC 恒为 1.0 → 方差为 0 → t 未定义。此时不得判定"显著"。

        零方差序列的 t = mean/0，数学上无意义。工具必须返回 nan 并计 0 个
        口径通过，而不是报告一个天文数字的 t。
        """
        ic, _, _ = icd.load_daily_ic(fake_db, "return_t7", "checked_t7")
        r = icd.diagnose(ic["signal"], lag=7, period="周")
        assert r["daily_ic"] == pytest.approx(1.0)
        assert math.isnan(r["daily_t"])
        assert r["passed_methods"] == 0
        assert r["sub_n"] < r["n_days"]       # 不重叠取样必然更少

    def test_noisy_strong_signal_passes_multiple_methods(self, tmp_path):
        """带噪声的强正相关应通过多个口径（这才是真实数据的形态）"""
        import random
        rng = random.Random(3)
        db = tmp_path / "noisy.db"
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, ticker TEXT, dimension_scores TEXT,
            price_at_predict REAL, price_t7 REAL, price_t30 REAL, close_t7 REAL,
            return_t7 REAL, checked_t7 INTEGER DEFAULT 0,
            return_t30 REAL, checked_t30 INTEGER DEFAULT 0)""")
        d0 = datetime.date.fromisoformat("2026-03-02")
        n = 0
        i = 0
        while n < 60:
            d = d0 + datetime.timedelta(days=i)
            i += 1
            if d.weekday() >= 5:
                continue
            n += 1
            for j in range(10):
                dims = {"signal": float(j) + rng.gauss(0, 1.2),
                        "catalyst": rng.random() * 10,
                        "sentiment": 5.0, "odds": 5.0, "risk_adj": 5.0}
                con.execute(
                    "INSERT INTO predictions (date,ticker,dimension_scores,"
                    "price_at_predict,price_t7,close_t7,return_t7,checked_t7) "
                    "VALUES (?,?,?,?,?,?,?,1)",
                    (d.isoformat(), f"T{j}", json.dumps(dims),
                     100.0, 100.0 + j, 100.0 + j, float(j)))
        con.commit()
        con.close()

        ic, _, _ = icd.load_daily_ic(db, "return_t7", "checked_t7")
        r = icd.diagnose(ic["signal"], lag=7, period="周")
        assert r["daily_ic"] > 0.5
        assert r["passed_methods"] >= 3, f"强信号只过了 {r['passed_methods']} 个口径"

        # 纯随机维度不得过半数口径。
        # 注意不能断言 ==0：四个口径各按 α=0.05 判定，纯噪音"至少过一个"的
        # 概率约 1−0.95⁴ ≈ 19%。所以「仅一口径过」正是噪音的典型形态——
        # 真实数据里 signal 与 catalyst 恰好就是这个档位，不足以支撑任何动作。
        rc = icd.diagnose(ic["catalyst"], lag=7, period="周")
        assert rc["passed_methods"] <= 1, \
            f"随机维度过了 {rc['passed_methods']} 个口径，超出噪音应有水平"

    def test_diagnose_short_series_returns_empty(self):
        assert icd.diagnose({"2026-03-02": 0.1}, lag=7, period="周") == {}

    def test_price_target_ignores_truncated_return_column(self, tmp_path):
        """target='price' 必须绕开 return_t7 列。

        真实库里 return_t7 是**路径依赖**收益（触及 SL/TP 即提前出场），
        42.5% 的行被钉在出场档位（+9.945 出现 93 次），与真实价格变动
        一致率仅 88.9%。档位截断会制造大量并列值，破坏 rank-IC 的尾部排序。
        """
        db = tmp_path / "trunc.db"
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, ticker TEXT, dimension_scores TEXT,
            price_at_predict REAL, price_t7 REAL,
            return_t7 REAL, checked_t7 INTEGER DEFAULT 0)""")
        d0 = datetime.date.fromisoformat("2026-03-02")
        n = 0
        i = 0
        while n < 12:
            d = d0 + datetime.timedelta(days=i)
            i += 1
            if d.weekday() >= 5:
                continue
            n += 1
            for j in range(6):
                dims = {"signal": float(j), "catalyst": 5.0, "sentiment": 5.0,
                        "odds": 5.0, "risk_adj": 5.0}
                # 真实价格单调递增；return_t7 全部钉死在同一"止盈档位"
                con.execute(
                    "INSERT INTO predictions (date,ticker,dimension_scores,"
                    "price_at_predict,price_t7,return_t7,checked_t7) "
                    "VALUES (?,?,?,?,?,?,1)",
                    (d.isoformat(), f"T{j}", json.dumps(dims),
                     100.0, 100.0 + j, 9.945))
        con.commit()
        con.close()

        ic_price, _, _ = icd.load_daily_ic(db, "return_t7", "checked_t7",
                                           target="price", horizon="t7")
        ic_path, _, _ = icd.load_daily_ic(db, "return_t7", "checked_t7",
                                          target="path", horizon="t7")
        # 干净口径能看出完美正相关；截断口径全是并列值 → 无法计算
        assert mean_of(ic_price["signal"]) == pytest.approx(1.0, abs=1e-9)
        assert ic_path["signal"] == {}, "全并列的目标不应产出 IC"

    def test_close_target_is_default(self):
        """
        默认口径必须是 close —— 它是**唯一**未被路径截断的收益源。

        v0.45.19 更正：原断言默认是 `price`。那条断言的理由（"path 会因档位
        截断产生大量并列值"）是对的，但 `price` 并不干净——`price_t7` 存的是
        `path["exit_price"]`，自 2026-05 起 100% 等于 `exit_price`，同样带
        SL/TP 截断。真正未截断的是 `close_t7`（v0.45.17 新增并回填）。

        换口径后 5 维排名直接反转（risk_adj 从四口径全过掉到 jackknife 失效、
        sentiment 从无口径通过升到三口径过），所以这个默认值是**有后果的**，
        不是风格选择——故保留断言，只把目标值改对。
        """
        import inspect
        sig = inspect.signature(icd.load_daily_ic)
        assert sig.parameters["target"].default == "close"
        assert icd.TARGETS == ("close", "price", "path")
        # `price` 必须仍在：历史报告要靠它复现
        assert "price" in icd.TARGETS

    def test_readonly_db_access(self, fake_db):
        """必须以只读模式打开，诊断工具不得意外写生产库"""
        before = fake_db.stat().st_mtime_ns
        icd.load_daily_ic(fake_db, "return_t7", "checked_t7")
        assert fake_db.stat().st_mtime_ns == before


def mean_of(d):
    from statistics import mean
    return mean(d.values())


class TestBenchmarkSuite:
    """v0.43.1 基准套件

    存在理由：单看「综合分 IC=−0.09, t=−2.8」无法回答唯一重要的问题
    ——**这比什么都不做强吗？** 2026-07-30 排查时正是靠临时加的动量对照，
    才把结论从「蜂群设计有问题」修正为「这个任务在此尺度上极难」。
    """

    def _panel(self, rng_seed=1, n_days=40, n_names=8, signal=0.0):
        """构造 {因子: {date: [(值, 收益)]}}，signal 控制真实相关强度"""
        import random as _r
        rng = _r.Random(rng_seed)
        panel = {"🐝 综合分 final_score": {}, "🎲 随机(单次抽样)": {}}
        d0 = datetime.date.fromisoformat("2026-03-02")
        i = 0
        made = 0
        while made < n_days:
            d = d0 + datetime.timedelta(days=i)
            i += 1
            if d.weekday() >= 5:
                continue
            made += 1
            key = d.isoformat()
            pairs, rnd = [], []
            for j in range(n_names):
                x = rng.gauss(0, 1)
                ret = signal * x + rng.gauss(0, 1)
                pairs.append((x, ret))
                rnd.append((rng.random(), ret))
            panel["🐝 综合分 final_score"][key] = pairs
            panel["🎲 随机(单次抽样)"][key] = rnd
        return panel

    def test_noise_floor_is_positive_and_bounded(self):
        """噪音地板必须是一个有限正数，且通过口径数远低于 4"""
        panel = self._panel(signal=0.0)
        f = icd.noise_floor(panel, lag=7, period="周", draws=60)
        assert f["n_draws"] >= 30
        assert 0 < f["ic_p95"] < 0.5, f"噪音地板异常: {f['ic_p95']}"
        assert f["ic_p50"] < f["ic_p95"]
        assert f["passed_mean"] < 2.0, "纯噪音的平均通过口径数不应接近 4"

    def test_real_signal_exceeds_noise_floor(self):
        """有真信号时必须超出噪音地板（否则地板设得太高，工具没有辨别力）"""
        panel = self._panel(signal=1.2)
        f = icd.noise_floor(panel, lag=7, period="周", draws=60)
        s = icd._ic_series_from_pairs(panel["🐝 综合分 final_score"])
        r = icd.diagnose(s, 7, "周")
        assert abs(r["daily_ic"]) > f["ic_p95"], "强信号未超出噪音地板"

    def test_pure_noise_does_not_exceed_floor(self):
        """纯噪音因子不得被判为超出地板（否则会产生假阳性建议）"""
        panel = self._panel(signal=0.0)
        f = icd.noise_floor(panel, lag=7, period="周", draws=80)
        s = icd._ic_series_from_pairs(panel["🎲 随机(单次抽样)"])
        r = icd.diagnose(s, 7, "周")
        assert abs(r["daily_ic"]) <= f["ic_p95"] * 1.5, \
            "纯噪音因子的 |IC| 明显超出地板，说明地板估计有误"

    def test_noise_floor_is_deterministic(self):
        """同一输入必须给出同一地板 —— 否则无法作为上线门槛"""
        panel = self._panel(signal=0.0)
        a = icd.noise_floor(panel, lag=7, period="周", draws=50)
        b = icd.noise_floor(panel, lag=7, period="周", draws=50)
        assert a["ic_p95"] == pytest.approx(b["ic_p95"])

    def test_empty_panel_degrades_gracefully(self):
        assert icd.noise_floor({}, lag=7, period="周", draws=10) == {}

    def test_price_load_failure_does_not_crash(self, monkeypatch, tmp_path):
        """行情拉取失败时基准降级，不得让整个工具崩掉"""
        monkeypatch.setattr(icd, "_load_prices", lambda *a, **k: None)
        db = tmp_path / "p.db"
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, ticker TEXT, final_score REAL, dimension_scores TEXT,
            price_at_predict REAL, price_t7 REAL, checked_t7 INTEGER DEFAULT 0)""")
        d0 = datetime.date.fromisoformat("2026-03-02")
        n = 0
        i = 0
        while n < 15:
            d = d0 + datetime.timedelta(days=i)
            i += 1
            if d.weekday() >= 5:
                continue
            n += 1
            for j in range(8):
                dims = {k: float(j) for k in icd.DIMS}
                con.execute(
                    "INSERT INTO predictions (date,ticker,final_score,"
                    "dimension_scores,price_at_predict,price_t7,checked_t7) "
                    "VALUES (?,?,?,?,?,?,1)",
                    (d.isoformat(), f"T{j}", float(j), json.dumps(dims),
                     100.0, 100.0 + j))
        con.commit()
        con.close()

        panel = icd.build_benchmark_panel(db, "return_t7", "checked_t7", "t7")
        assert "🐝 综合分 final_score" in panel, "系统自身基准必须始终可用"
        assert not any(k.startswith("📈") for k in panel), \
            "行情不可用时不应出现价格类因子"


class TestNoiseFloorBaseKey:
    """v0.43.3 回归：地板基准不得依赖 dict 迭代顺序

    旧实现写死匹配 `"🐝 综合分 final_score"`（只有 build_benchmark_panel 产生该键），
    在 signal_archive 面板上必然落空并 fallback 到 `next(iter(...))` ——
    即依赖 SQL 行序。实测地板对基准骨架高度敏感：
    64 天/宽10.4 → 0.076，49 天/宽8.0 → 0.116，相差 52%，足以翻转判定。
    """

    def _panel(self):
        import random as _r
        rng = _r.Random(5)
        def mk(n_days, width):
            out = {}
            d0 = datetime.date.fromisoformat("2026-03-02")
            i = made = 0
            while made < n_days:
                d = d0 + datetime.timedelta(days=i); i += 1
                if d.weekday() >= 5:
                    continue
                made += 1
                out[d.isoformat()] = [(rng.random(), rng.gauss(0, 1))
                                      for _ in range(width)]
            return out
        return {"wide": mk(50, 12), "composite.final_score": mk(50, 12),
                "narrow": mk(18, 6)}

    def test_explicit_base_key_is_used(self):
        p = self._panel()
        wide = icd.noise_floor(p, 7, "周", draws=40, base_key="wide")
        narrow = icd.noise_floor(p, 7, "周", draws=40, base_key="narrow")
        assert wide["ic_p95"] != pytest.approx(narrow["ic_p95"]), \
            "不同基准应给出不同地板（否则参数没生效）"
        assert narrow["ic_p95"] > wide["ic_p95"], "更窄/更短的基准地板应更高"

    def test_falls_back_to_composite_final_score(self):
        """未显式指定时，应优先匹配 composite.final_score 而非 dict 首项"""
        p = self._panel()
        auto = icd.noise_floor(p, 7, "周", draws=40)
        explicit = icd.noise_floor(p, 7, "周", draws=40,
                                   base_key="composite.final_score")
        assert auto["ic_p95"] == pytest.approx(explicit["ic_p95"])

    def test_fallback_is_deterministic_when_no_known_key(self):
        """都匹配不上时取覆盖最多的，而非 dict 首项 —— 必须确定性"""
        p = {"z": self._panel()["narrow"], "a": self._panel()["wide"]}
        a = icd.noise_floor(p, 7, "周", draws=40)
        b = icd.noise_floor(dict(reversed(list(p.items()))), 7, "周", draws=40)
        assert a["ic_p95"] == pytest.approx(b["ic_p95"]), \
            "地板不得随 dict 顺序变化"

    def test_missing_base_key_does_not_crash(self):
        p = self._panel()
        assert icd.noise_floor(p, 7, "周", draws=20, base_key="不存在")["n_draws"] > 0
