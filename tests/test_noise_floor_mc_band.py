"""
噪音地板的蒙特卡洛不确定带（v0.45.324）

背景：`signal_archive.analyze()` / `ic_diagnostics --benchmark` 的判定都拿 |日度IC| 去比
「随机排序 N 次的 |IC| 95 分位」。旧默认 N=200：p95 只靠 ~10 个超出值定位，相对标准误 6.7%。
v0.45.321 在生产快照上看到 200 次地板比 2000 次低 9–15%，贴地板的信号
（`crowding.comp.short_squeeze_risk`）随抽样数在 🟡/⚪ 之间翻 —— **判定取决于蒙特卡洛
分辨率，而不是数据。**

v0.45.324 的处置分两层，本文件各锁一层：
  1. 地板带上自身的 MC 不确定带（次序统计量精确二项 CI，`quantile_band`），判定对**带的两沿**
     比（`floor_position`），带内显式标 ◐，不替它挑一边；
  2. 默认抽样数 200 → 2000（`RANDOM_DRAWS`，两个工具共用），让带足够窄、◐ 足够少。

⚠️ 只加大 N 治不了根：任何有限 N 下总有信号恰好贴着地板，点估计判定照样会翻。
锁住的是「**不许矛盾**」—— 同一骨架、不同抽样数，任何 |IC| 不许一边判 above、另一边判 below。
"""

import datetime
import math
import os
import random
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ic_diagnostics as icd
import ic_rerun_readiness as irr
import signal_archive as sa


def _bdays(start: str, n: int):
    d = datetime.date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def _skeleton(seed: int = 3, n_days: int = 40, width: int = 8):
    """{date: [(占位值, 收益)]} —— noise_floor 只读骨架（日期 / 宽度 / 收益），值会被随机化掉。"""
    rng = random.Random(seed)
    return {d: [(0.0, rng.gauss(0, 1)) for _ in range(width)]
            for d in _bdays("2026-03-02", n_days)}


# ══════════════════════════════════════════════════════════════════════════
# 1. 带本身：覆盖率、单调、抽样太少时不假装有上沿
# ══════════════════════════════════════════════════════════════════════════

class TestQuantileBand:

    def test_band_brackets_point_estimate(self):
        rng = random.Random(1)
        v = [abs(rng.gauss(0, 1)) for _ in range(500)]
        lo, hi = icd.quantile_band(v, 0.95)
        p = sorted(v)[int(0.95 * len(v))]      # 与 noise_floor 的点估计同一取法
        assert lo < p < hi

    def test_coverage_is_nominal(self):
        """已知分布 |N(0,1)| 的 p95 = 1.959964。400 次 × n=200：覆盖率应在 95% 附近。

        带太窄（例如退回点估计、或只取 ±1 个次序统计量）会在这里红 ——
        那样的带会把 MC 噪声重新渲染成「已定」。"""
        true_p95 = 1.959964
        rng = random.Random(20260923)
        hit = 0
        trials = 400
        for _ in range(trials):
            lo, hi = icd.quantile_band([abs(rng.gauss(0, 1)) for _ in range(200)], 0.95)
            hit += lo <= true_p95 <= hi
        assert 0.92 <= hit / trials <= 0.995, f"覆盖率 {hit / trials:.3f}"

    def test_band_narrows_with_draws(self):
        """相对宽度 ∝ 1/√N：×10 抽样数，带宽应缩到约 1/3。"""
        rng = random.Random(2)
        pool = [abs(rng.gauss(0, 1)) for _ in range(2000)]
        lo200, hi200 = icd.quantile_band(pool[:200])
        lo2000, hi2000 = icd.quantile_band(pool)
        w200, w2000 = hi200 - lo200, hi2000 - lo2000
        assert w2000 < w200 / 2.2, (w200, w2000)

    def test_too_few_draws_has_no_upper_edge(self):
        """0.95^71 > 0.025 ⇒ 71 次抽样给不出 p95 的 97.5% 上界。返回 +inf，
        **不能**退回样本最大值 —— 否则 `--draws 50` 会安静地产出一排「超出」。"""
        assert math.isinf(icd.quantile_band([float(i) for i in range(71)])[1])
        assert math.isfinite(icd.quantile_band([float(i) for i in range(72)])[1])
        rng = random.Random(4)
        lo, hi = icd.quantile_band([rng.random() for _ in range(50)])
        assert icd.floor_position(1e6, lo, hi) != "above", "抽样太少时不许判超出"

    def test_band_level_is_read_at_call_time(self, monkeypatch):
        """置信水平只有一处真相。`floor_band_text` 调用时读 `FLOOR_BAND_ALPHA` 印标签，
        带若在 def 期冻结了旧值，改常量后标签写 50%、带仍是 95% —— 报告自相矛盾。"""
        rng = random.Random(5)
        v = [abs(rng.gauss(0, 1)) for _ in range(2000)]
        lo95, hi95 = icd.quantile_band(v, alpha=0.05)
        monkeypatch.setattr(icd, "FLOOR_BAND_ALPHA", 0.5)
        lo, hi = icd.quantile_band(v)
        assert (lo, hi) == icd.quantile_band(v, alpha=0.5)
        assert lo95 < lo and hi < hi95, "改了 FLOOR_BAND_ALPHA，默认带却没变窄"
        assert "50%" in icd.floor_band_text({"ic_p95_lo": lo, "ic_p95_hi": hi, "n_draws": 2000})

    def test_floor_position_edges(self):
        assert icd.floor_position(0.2, 0.1, 0.15) == "above"
        assert icd.floor_position(0.12, 0.1, 0.15) == "band"
        assert icd.floor_position(0.15, 0.1, 0.15) == "band"
        assert icd.floor_position(0.1, 0.1, 0.15) == "below"
        assert icd.floor_position(0.2, float("nan"), float("nan")) == "n/a"


# ══════════════════════════════════════════════════════════════════════════
# 2. 核心不变式：判定不随抽样数矛盾
# ══════════════════════════════════════════════════════════════════════════

class TestVerdictDoesNotFlipWithDrawCount:
    """同一骨架上 ×200 与 ×2000 的地板。v0.45.321 的事故就是两次点估计之间的信号换了判定。"""

    @pytest.fixture(scope="class")
    def floors(self):
        panel = {"composite.final_score": _skeleton()}
        return {n: icd.noise_floor(panel, 7, "周", draws=n) for n in (200, 2000)}

    def test_fixture_reproduces_the_incident_shape(self, floors):
        """没有这一条，下面的「不矛盾」可能只是两个点估计恰好相等 —— 空转。"""
        a, b = floors[200]["ic_p95"], floors[2000]["ic_p95"]
        assert abs(a - b) / b > 0.01, f"夹具两档点估计几乎相同（{a:.4f} vs {b:.4f}），测不出翻转"

    def test_no_value_is_above_at_one_draw_count_and_below_at_the_other(self, floors):
        a, b = floors[200]["ic_p95"], floors[2000]["ic_p95"]
        grid = [min(a, b) * 0.5 + i * (max(a, b) * 1.5 - min(a, b) * 0.5) / 4000
                for i in range(4001)]
        # 牙齿：点估计判定（旧逻辑 |IC| > ic_p95）在这些值上确实会翻
        naive_flips = [x for x in grid if (x > a) != (x > b)]
        assert naive_flips, "网格没覆盖两次点估计之间 —— 测不出旧 bug"
        bad = []
        for x in grid:
            p = {n: icd.floor_position(x, floors[n]["ic_p95_lo"], floors[n]["ic_p95_hi"])
                 for n in (200, 2000)}
            if {p[200], p[2000]} == {"above", "below"}:
                bad.append((x, p))
        assert not bad, (f"{len(bad)} 个 |IC| 在 ×200 与 ×2000 下判定相反，首个 {bad[0]} —— "
                         f"地板带没盖住蒙特卡洛误差，判定又取决于抽样数了")

    def test_default_draws_keep_band_tight(self):
        """默认抽样数的意义：带窄到 ◐ 罕见。退回 200 时相对半宽 ~16%，这里红。

        判据用相对半宽而不是钉死常量 —— 换成别的够大的 N 不该红。"""
        f = icd.noise_floor({"composite.final_score": _skeleton()}, 7, "周")
        assert f["n_draws"] == icd.RANDOM_DRAWS
        half = (f["ic_p95_hi"] - f["ic_p95_lo"]) / 2 / f["ic_p95"]
        assert half < 0.07, f"默认 ×{f['n_draws']} 的地板带相对半宽 {half:.1%}，太宽"


# ══════════════════════════════════════════════════════════════════════════
# 3. analyze() / 报告接线
# ══════════════════════════════════════════════════════════════════════════

TICKERS = [f"T{i}" for i in range(10)]
BOUNDARY = "2026-05-04"
PRE = _bdays("2026-03-02", 30)
POST = _bdays(BOUNDARY, 20)


def _build_db(tmp_path):
    """合成库：`weak` 与收益弱相关、`strong` 强相关；两个 Guard 信号在边界后骨架完全相同，
    `agent.GuardBeeSentinel.gappy` 在边界后缺一天（骨架不同）。"""
    rng = random.Random(11)
    db = tmp_path / "p.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
        price_at_predict REAL, close_t7 REAL, checked_t7 INTEGER DEFAULT 0)""")
    con.commit()
    con.close()
    sa.ensure_schema(db)
    preds, arch = [], []
    for d in PRE + POST:
        for tk in TICKERS:
            x = rng.gauss(0, 1)
            ret = x + rng.gauss(0, 0.3)
            preds.append((d, tk, 100.0, 100.0 * (1 + ret / 100.0), 1))
            arch += [(d, tk, "composite.final_score", x),
                     (d, tk, "strong", x),
                     (d, tk, "weak", 0.15 * x + rng.gauss(0, 1)),
                     (d, tk, "agent.GuardBeeSentinel.score", x),
                     (d, tk, "agent.GuardBeeSentinel.direction", x + rng.gauss(0, 1))]
            if d != POST[5]:
                arch.append((d, tk, "agent.GuardBeeSentinel.gappy", x))
    with sqlite3.connect(db) as c:
        c.executemany("INSERT INTO predictions (date,ticker,price_at_predict,close_t7,checked_t7)"
                      " VALUES (?,?,?,?,?)", preds)
        c.executemany(f"INSERT INTO {sa.TABLE} (date,ticker,signal,value) VALUES (?,?,?,?)", arch)
    return db


@pytest.fixture
def no_boundaries(monkeypatch):
    monkeypatch.setattr(irr, "_COHORT_HISTORY", [])


@pytest.fixture
def guard_boundary(monkeypatch):
    monkeypatch.setattr(irr, "_COHORT_HISTORY", [(BOUNDARY, "vTEST", "测试边界")])
    monkeypatch.setattr(sa, "COHORT_SIGNAL_SCOPE", {"vTEST": ("agent.GuardBeeSentinel.*",)})


class TestAnalyzeWiring:

    def test_default_draws_come_from_random_draws(self, tmp_path, no_boundaries, monkeypatch):
        """两个工具只有一个默认值：`analyze()` 不传 draws 时读**调用时**的 RANDOM_DRAWS。
        旧代码在 signal_archive 里另写死了一个 200（def 默认值 + argparse 各一处）。"""
        monkeypatch.setattr(icd, "RANDOM_DRAWS", 90)
        _rows, floor, _ = sa.analyze(_build_db(tmp_path))
        assert floor["n_draws"] == 90

    def test_rows_carry_band_and_beats_noise_requires_upper_edge(self, tmp_path, no_boundaries):
        rows, floor, _ = sa.analyze(_build_db(tmp_path), draws=300)
        assert rows, "夹具面板为空 —— 断言全是空转"
        for r in rows:
            assert r["noise_floor_lo"] <= r["noise_floor"] <= r["noise_floor_hi"]
            assert r["floor_position"] == icd.floor_position(
                abs(r["daily_ic"]), r["noise_floor_lo"], r["noise_floor_hi"])
            assert r["beats_noise"] == (r["floor_position"] == "above")

    def test_signal_inside_band_is_marked_not_decided(self, tmp_path, no_boundaries,
                                                        monkeypatch, capsys):
        """把地板带摆到 `weak` 的 |IC| 两侧：它必须是 ◐，且报告写出带。
        旧逻辑下它会按点估计落 🟡 或 ⚪ —— 取哪边只看这一次抽了多少次。"""
        db = _build_db(tmp_path)
        rows, _, _ = sa.analyze(db, draws=100)
        x = abs(next(r for r in rows if r["signal"] == "weak")["daily_ic"])
        real = icd.noise_floor

        def fake(panel, lag, period, draws=None, base_key=None):
            f = real(panel, lag, period, draws=draws, base_key=base_key)
            f.update(ic_p95=x * 0.98, ic_p95_lo=x * 0.9, ic_p95_hi=x * 1.1)
            return f

        monkeypatch.setattr(icd, "noise_floor", fake)
        rows, floor, gens = sa.analyze(db, draws=100)
        weak = next(r for r in rows if r["signal"] == "weak")
        assert weak["floor_position"] == "band" and not weak["beats_noise"]
        assert sa.verdict_mark(weak) == "◐ 贴地板"
        assert sa.verdict_mark(next(r for r in rows if r["signal"] == "strong")) == "🟢 候选"
        sa.print_report(rows, floor, "t7", generations=gens)
        out = capsys.readouterr().out
        assert "◐ 贴地板" in out and "MC 95% 带" in out

    def test_report_says_so_when_draws_too_few_for_upper_edge(self, tmp_path, no_boundaries,
                                                               capsys):
        rows, floor, gens = sa.analyze(_build_db(tmp_path), draws=40)
        assert not any(r["beats_noise"] for r in rows), "×40 抽样给不出上沿，不许判超出"
        sa.print_report(rows, floor, "t7", generations=gens)
        assert "无上沿" in capsys.readouterr().out

    def test_sliced_floor_cache_shares_only_identical_skeletons(self, tmp_path, guard_boundary,
                                                                monkeypatch):
        """被切信号的地板按逐字相同的骨架缓存。同骨架两个信号只算一次、地板相同；
        骨架差一天的必须单算 —— 缓存键放宽了会把别人的地板安到它头上。"""
        calls = []
        real = icd.noise_floor

        def spy(panel, lag, period, draws=None, base_key=None):
            calls.append(base_key)
            return real(panel, lag, period, draws=draws, base_key=base_key)

        monkeypatch.setattr(icd, "noise_floor", spy)
        db = _build_db(tmp_path)
        rows, _, _ = sa.analyze(db, draws=100)
        rows = {r["signal"]: r for r in rows}
        score, direction, gappy = (rows[f"agent.GuardBeeSentinel.{k}"]
                                   for k in ("score", "direction", "gappy"))
        assert score["n_excluded"] and direction["n_excluded"] and gappy["n_excluded"]
        assert score["noise_floor"] == direction["noise_floor"]
        sliced_calls = [k for k in calls if k != "composite.final_score"]
        assert len(sliced_calls) == 2, sliced_calls
        # 缓存命中的值 == 直接单算的值（精确缓存，不是近似）
        post = {d: v for d, v in sa.load_panel(db, "t7", 5)
                ["agent.GuardBeeSentinel.direction"].items() if d >= BOUNDARY}
        direct = real({"x": post}, 7, "周", draws=100, base_key="x")
        assert direct["ic_p95"] == direction["noise_floor"]
