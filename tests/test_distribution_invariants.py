"""
生产端产出分布的不变式（v0.44.0）

为什么需要**另一类**测试
------------------------
把本项目的 bug 史摊开看，同一个形状反复出现：

  · ChronosBee 950 条记录 **bearish=0**（分支结构性不可达）
  · PEAD 方向逻辑写了但被后面的赋值冲掉（死代码）
  · BullVeto 从未真正生效
  · CodeExecutorAgent 恒定看多
  · yfinance MultiIndex 静默失效，**20 处**
  · VIX 兜底常量 20.0 撑了 13/88 天（真实值 14.25，方向相反）
  · 权重优化器静默 inert 11 周，日志空白到与"任务挂了"无法区分

全都不是逻辑写错，而是**单元测试全绿、退出码为 0、日志正常，但输出分布早已退化**。
这类缺陷逃得过所有基于代码路径的测试——`test_chronos_bee_direction.py` 那种
"给定 PEAD 输入 → 断言方向"的机制测试是必要的，但它证明的是"这条路走得通"，
不是"这条路真的被走过"。

本文件断言的是后者：**生产库里实际产出的分布**。两类测试互补，都要有。

设计约束
--------
1. 读**生产** `pheromone.db`，因此显式绕过 conftest 的 tmp 隔离（那个 autouse
   fixture 只改环境变量，不影响这里的直接路径读取）。全程只读（`mode=ro`）。
2. 数据不足时 **skip 而非 fail** —— 新克隆、CI、干净环境下必须绿。
3. 门槛守的是**结构性退化**（某方向不可达），不是"分布好不好看"。
   现状最偏的是 `RivalBeeVanguard` 95.2% 看多——离门槛只剩 4pp，
   刻意不把门槛设到能拦住它：那属于建模判断，不该由测试替用户拍板。
   "覆盖率够不够"同理，那是 `scan_continuity.py` 的职责（它有分级门槛与
   退出码），本文件只拦"扫描实质上已经死了"。
"""

import datetime as dt
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROD_DB = PROJECT_ROOT / "pheromone.db"
VIX_CSV = PROJECT_ROOT / "cache" / "vix_history_cboe.csv"

# 观察窗口：最近 N 个**有扫描的业务日**。用扫描日而非自然日，
# 才能在扫描稀疏时仍取到足够样本，也让已修复的 bug 反映在窗口内。
RECENT_SCAN_DAYS = 12
MIN_RECORDS = 60          # 少于此不做分布判断

# 单一方向占比上限。ChronosBee 事故是 100%（950/0），现状最偏 95.2%。
# 设 0.99 = 拦住"结构性不可达"，放过"真实偏斜"。
MAX_SINGLE_DIRECTION_SHARE = 0.99

# 维度分数去重比下限。低于此说明大量并列，rank-IC 会失真。
MIN_DISTINCT_RATIO = 0.25
# catalyst 是**已知且已接受**的退化项：ChronosBee 是纯单向事件强度累加器，
# 2026-07 已结算样本 cat≥7 占比 0%、55% 恰为 6.0。暂不修的理由见 MEMORY
# （爆炸半径大：62 个 .py 引用 catalyst，且会切出第三个不可比世代）。
#
# v0.45.60：口径从**全局去重比**改为**每个扫描日的不同值数（中位）**。
#
# 原口径 `distinct/n` 对离散维度会随窗口填满**机械性衰减**：catalyst 全局只有
# ~13 个取值，distinct 几乎不增长而 n 线性增长，比值必然下滑。实测
# 2026-08-28：n=265 distinct=13 → 0.049，跌破 0.05 地板 —— 但**每个扫描日
# 仍稳定有 5.5 个不同值**，与 8/24–8/26 完全一致，分布根本没退化。
# 机器恢复日更后窗口会填到 12×30=360 行，比值将跌到 ~0.036：这条闸注定
# 天天红，而红的原因与它想守的东西无关。
#
# 一条会因为**数据变多**而报警的不变式，是在训练人忽略它。
#
# 每日不同值数不随 n 变化，直接对应它真正要守的那句话：「别彻底塌成常数」。
# 实测 catalyst 每日中位 5.5；塌成常数是 1。地板取 3。
# 其余 4 个维度是连续量（比值 0.45~0.71，每日 17~25 个不同值），
# 原比值口径对它们依然有效，不动。
KNOWN_DEGRADED_DIMS_MIN_DAILY_DISTINCT = {"catalyst": 3}

VIX_MAX_STALE_TRADING_DAYS = 5
CATASTROPHIC_NO_SCAN_TRADING_DAYS = 10


pytestmark = pytest.mark.skipif(
    not PROD_DB.exists(),
    reason="生产 pheromone.db 不存在（新克隆/CI 环境）——分布不变式不适用",
)


# ────────────────────────────────────────────────────────────────────────────
# 取数
# ────────────────────────────────────────────────────────────────────────────

def _con():
    return sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)


@pytest.fixture(scope="module")
def recent_dates():
    with _con() as con:
        rows = con.execute(
            "SELECT DISTINCT date FROM predictions "
            "WHERE date IS NOT NULL ORDER BY date DESC LIMIT ?",
            (RECENT_SCAN_DAYS,),
        ).fetchall()
    dates = sorted(r[0] for r in rows)
    if len(dates) < 3:
        pytest.skip(f"扫描日不足 3 天（{len(dates)}）")
    return dates


@pytest.fixture(scope="module")
def agent_directions(recent_dates):
    """{蜂名: Counter({方向: 次数})}，近 RECENT_SCAN_DAYS 个扫描日。"""
    out = defaultdict(Counter)
    with _con() as con:
        rows = con.execute(
            "SELECT agent_directions FROM predictions "
            "WHERE date >= ? AND agent_directions IS NOT NULL",
            (recent_dates[0],),
        ).fetchall()
    for (raw,) in rows:
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        for agent, direction in d.items():
            if isinstance(direction, str) and direction:
                out[agent][direction] += 1
    if not out:
        pytest.skip("窗口内没有可解析的 agent_directions")
    return dict(out)


@pytest.fixture(scope="module")
def dimension_scores_by_day(recent_dates):
    """{维度: {扫描日: [分数, ...]}}，近 RECENT_SCAN_DAYS 个扫描日。

    与 `dimension_scores` 同一份数据，只是保留了日期分组 —— 离散维度必须
    按日看不同值数，全局去重比会随 n 机械衰减（见 KNOWN_DEGRADED_DIMS_*
    处的注释）。
    """
    out = defaultdict(lambda: defaultdict(list))
    with _con() as con:
        rows = con.execute(
            "SELECT date, dimension_scores FROM predictions "
            "WHERE date >= ? AND dimension_scores IS NOT NULL",
            (recent_dates[0],),
        ).fetchall()
    for day, raw in rows:
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        for dim, val in d.items():
            try:
                out[dim][str(day)[:10]].append(float(val))
            except (TypeError, ValueError):
                continue
    return {k: dict(v) for k, v in out.items()}


@pytest.fixture(scope="module")
def dimension_scores(recent_dates):
    """{维度: [分数, ...]}，近 RECENT_SCAN_DAYS 个扫描日。"""
    out = defaultdict(list)
    with _con() as con:
        rows = con.execute(
            "SELECT dimension_scores FROM predictions "
            "WHERE date >= ? AND dimension_scores IS NOT NULL",
            (recent_dates[0],),
        ).fetchall()
    for (raw,) in rows:
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        for dim, val in d.items():
            try:
                out[dim].append(float(val))
            except (TypeError, ValueError):
                continue
    if not out:
        pytest.skip("窗口内没有可解析的 dimension_scores")
    return dict(out)


# ────────────────────────────────────────────────────────────────────────────
# 判定谓词（抽成纯函数，才能验证"守卫真的会红"）
#
# 这一步不是洁癖。本文件守的就是"看着在跑其实早废了"，如果判定逻辑内联在
# 断言里、只喂过生产数据，那它自己就是一个从未被证明会触发的守卫 ——
# 与 BullVeto「从未真正生效」同构。所以每个谓词都有一对测试：
# 生产数据必须干净，合成的退化数据必须被抓住。
# ────────────────────────────────────────────────────────────────────────────

def single_direction_offenders(agent_dirs, max_share=MAX_SINGLE_DIRECTION_SHARE,
                              min_records=MIN_RECORDS):
    """单一方向占比 ≥ max_share 的蜂。"""
    out = []
    for agent, cnt in agent_dirs.items():
        total = sum(cnt.values())
        if total < min_records:
            continue
        top_dir, top_n = Counter(cnt).most_common(1)[0]
        share = top_n / total
        if share >= max_share:
            out.append(f"{agent}: {top_dir} {share:.1%} ({top_n}/{total})")
    return out


def agents_missing_bearish(agent_dirs, min_records=MIN_RECORDS):
    """窗口内一次都没产出 bearish 的蜂。"""
    out = []
    for agent, cnt in agent_dirs.items():
        total = sum(cnt.values())
        if total < min_records:
            continue
        if cnt.get("bearish", 0) == 0:
            out.append(f"{agent}（{total} 条记录，bearish=0，实际分布 {dict(cnt)}）")
    return out


def low_spread_dims(dim_scores, floor=MIN_DISTINCT_RATIO,
                    min_records=MIN_RECORDS, exempt=frozenset()):
    """去重比低于 floor 的维度。"""
    out = []
    for dim, vals in dim_scores.items():
        if dim in exempt or len(vals) < min_records:
            continue
        ratio = len(set(vals)) / len(vals)
        if ratio < floor:
            out.append(f"{dim}: 去重比 {ratio:.3f} ({len(set(vals))}/{len(vals)})")
    return out


class TestGuardsHaveTeeth:
    """喂合成的退化数据，确认上面三个谓词真的会触发。

    没有这一组，本文件的"全绿"证明不了任何事。
    """

    def test_catches_structurally_single_direction(self):
        """复刻 ChronosBee 事故的形状：950 条记录、bearish 一条都没有。"""
        skewed = {"ChronosBeeHorizon": Counter({"neutral": 900, "bullish": 50})}
        assert single_direction_offenders(skewed) == [], \
            "94.7% 不该触发（门槛 99%）—— 守的是不可达，不是偏斜"
        total_collapse = {"ChronosBeeHorizon": Counter({"neutral": 950})}
        assert single_direction_offenders(total_collapse), \
            "100% 单一方向必须被抓住"

    def test_catches_missing_bearish(self):
        no_bear = {"ChronosBeeHorizon": Counter({"neutral": 789, "bullish": 96})}
        hits = agents_missing_bearish(no_bear)
        assert hits and "bearish=0" in hits[0]

    def test_bearish_present_even_once_passes(self):
        """v0.43.0 修复后的真实状态是 bearish=3/189。不该因为"太少"而红 ——
        那是建模判断，不是结构性缺陷。
        """
        barely = {"ChronosBeeHorizon": Counter(
            {"neutral": 179, "bullish": 7, "bearish": 3})}
        assert agents_missing_bearish(barely) == []

    def test_catches_collapsed_dimension(self):
        frozen = {"catalyst": [6.0] * 200}
        assert low_spread_dims(frozen)
        assert low_spread_dims(frozen, exempt={"catalyst"}) == [], \
            "豁免名单必须真的生效"

    def test_below_min_records_is_not_judged(self):
        """样本不足时必须沉默，否则新环境下会假红。"""
        tiny = {"SomeBee": Counter({"bullish": 5})}
        assert single_direction_offenders(tiny) == []
        assert agents_missing_bearish(tiny) == []
        assert low_spread_dims({"signal": [1.0] * 5}) == []


# ────────────────────────────────────────────────────────────────────────────
# 不变式 1：每只蜂的方向都必须真的可达
# ────────────────────────────────────────────────────────────────────────────

class TestAgentDirectionReachability:
    """守的正是 ChronosBee「950 条记录 bearish=0」那一类缺陷。

    那个 bug 的本质是 `elif score <= 4.5` 在 score 恒 ≥5.5 时不可达 ——
    逻辑没写错，是**结构上永远走不到**。单元测试给它构造一个低分输入就能过，
    生产里却一次都没发生。只有对实际产出分布的断言能拦住。
    """

    def test_no_agent_is_effectively_single_direction(self, agent_directions):
        offenders = single_direction_offenders(agent_directions)
        assert not offenders, (
            "有蜂的方向输出已结构性退化（单一方向占比 ≥ "
            f"{MAX_SINGLE_DIRECTION_SHARE:.0%}）：\n  " + "\n  ".join(offenders)
            + "\n这是 ChronosBee 950/0 那类缺陷的signature。"
        )

    def test_bearish_is_reachable_for_every_agent(self, agent_directions):
        """看空方向单独拎出来断言 —— 历史上塌掉的恰恰总是 bearish。

        信息素板多空失衡会自我强化（多头票越多，下游越倾向看多），
        所以 bearish 不可达的代价不止于该蜂本身。
        """
        missing = agents_missing_bearish(agent_directions)
        assert not missing, (
            "以下蜂在窗口内一次都没有产出 bearish：\n  " + "\n  ".join(missing)
        )

    def test_agents_are_actually_present(self, agent_directions):
        """蜂整体消失（Phase 挂了）也是一种静默降级 —— 退出码依然是 0。"""
        core = {"ScoutBeeNova", "OracleBeeEcho", "BuzzBeeWhisper",
                "ChronosBeeHorizon", "RivalBeeVanguard", "GuardBeeSentinel"}
        seen = set(agent_directions)
        assert core <= seen, f"核心蜂缺席: {sorted(core - seen)}"


# ────────────────────────────────────────────────────────────────────────────
# 不变式 2：维度分数不得塌成常数
# ────────────────────────────────────────────────────────────────────────────

class TestDimensionScoreSpread:
    """去重比过低 → 大量并列 → rank-IC 尾部排序失真。

    `experiments/ic_power_analysis.py` 里实测过这件事的下游影响：并列结构直接
    决定置换零分布的方差，是那份功效计算第一版算错的根源。
    """

    def test_healthy_dims_keep_enough_distinct_values(self, dimension_scores):
        offenders = low_spread_dims(dimension_scores,
                                    exempt=frozenset(KNOWN_DEGRADED_DIMS_MIN_DAILY_DISTINCT))
        assert not offenders, (
            f"维度分数并列过多（门槛 {MIN_DISTINCT_RATIO}）：\n  "
            + "\n  ".join(offenders)
        )

    def test_known_degraded_dims_have_not_fully_collapsed(self, dimension_scores_by_day):
        """catalyst 已知退化且**已决定暂不修**，但仍守一条底线：
        彻底塌成常数就是另一回事了，那意味着它连噪音都不再贡献。

        按**扫描日**看不同值数，不看全局去重比 —— 后者对离散维度会随窗口
        填满而机械下滑，报的是"数据变多了"，不是"分布坏了"。见常量处注释。
        """
        import statistics as _st
        for dim, floor in KNOWN_DEGRADED_DIMS_MIN_DAILY_DISTINCT.items():
            per_day = dimension_scores_by_day.get(dim, {})
            counts = [len(set(v)) for v in per_day.values() if v]
            if sum(len(v) for v in per_day.values()) < MIN_RECORDS:
                continue
            if not counts:
                continue
            med = _st.median(counts)
            assert med >= floor, (
                f"{dim} 每个扫描日中位只有 {med:.1f} 个不同值（地板 {floor}）"
                f"——逐日：{ {d: len(set(v)) for d, v in sorted(per_day.items())} }。"
                "它比记录在案的状态又坏了：接近塌成常数。"
            )

    def test_all_five_dims_present(self, dimension_scores):
        expected = {"signal", "catalyst", "sentiment", "odds", "risk_adj"}
        assert expected <= set(dimension_scores), (
            f"缺维度: {sorted(expected - set(dimension_scores))}"
        )


# ────────────────────────────────────────────────────────────────────────────
# 不变式 3：宏观数据没有静默退回兜底常量
# ────────────────────────────────────────────────────────────────────────────

class TestMacroNotSilentlyDegraded:
    def test_macro_adjustment_is_not_frozen(self, recent_dates):
        """`guard.macro_adj` 塌成单一值 = 宏观上游全挂后 GuardBee 在吃常量。

        这是 v0.43.24 那次事故的可观测代价：VIX 兜底 20.0 撑了 13/88 天，
        而当天真实 VIX 是 14.6 —— 方向相反的信号。
        """
        with _con() as con:
            vals = [v for (v,) in con.execute(
                "SELECT value FROM signal_archive "
                "WHERE signal = 'guard.macro_adj' AND date >= ?",
                (recent_dates[0],),
            )]
        if len(vals) < MIN_RECORDS:
            pytest.skip(f"guard.macro_adj 样本不足（{len(vals)}）")
        assert len(set(vals)) > 1, (
            f"guard.macro_adj 在 {len(vals)} 条记录里只有一个取值 "
            f"{vals[0]} —— 宏观输入很可能已全程降级到兜底常量"
        )

    @pytest.mark.skipif(not VIX_CSV.exists(),
                        reason="CBOE VIX 历史缓存不存在")
    def test_cboe_vix_history_is_fresh(self):
        """CBOE 抓取静默死掉的表现就是这个文件停止增长，
        而系统会无声地退回 `vix=20.0` 常量。守住源头比守下游便宜。
        """
        lines = [ln for ln in VIX_CSV.read_text().splitlines() if ln.strip()]
        assert len(lines) > 1, "VIX 历史文件只有表头"
        last = lines[-1].split(",")[0].strip()
        # CBOE 的格式是 MM/DD/YYYY
        m, d, y = last.split("/")
        last_date = dt.date(int(y), int(m), int(d))

        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        from is_trading_day import is_trading_day

        stale = 0
        cur = dt.date.today()
        while cur > last_date and stale <= VIX_MAX_STALE_TRADING_DAYS + 1:
            cur -= dt.timedelta(days=1)
            if cur > last_date and is_trading_day(cur)[0]:
                stale += 1
        assert stale <= VIX_MAX_STALE_TRADING_DAYS, (
            f"CBOE VIX 历史最后一条是 {last_date}，已过 {stale} 个交易日 "
            f"（门槛 {VIX_MAX_STALE_TRADING_DAYS}）——CBOE 抓取可能已静默失效，"
            f"下游会退回 vix=20.0 兜底常量"
        )


# ────────────────────────────────────────────────────────────────────────────
# 不变式 4：扫描没有实质性停摆
# ────────────────────────────────────────────────────────────────────────────

class TestScanNotDead:
    """刻意只拦"扫描实质上已经死了"。

    "覆盖率 80% 够不够"是分级判断，归 `scan_continuity.py`（有门槛参数与
    退出码，供编排器决策）。测试里放一个会因运维波动而红的断言，
    结果是整个套件被习惯性忽略 —— 那比没有测试更糟。
    """

    def test_scanned_within_recent_trading_days(self, recent_dates):
        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        from scan_continuity import recent_trading_days

        window = {d.isoformat() for d in
                  recent_trading_days(CATASTROPHIC_NO_SCAN_TRADING_DAYS)}
        assert window & set(recent_dates), (
            f"最近 {CATASTROPHIC_NO_SCAN_TRADING_DAYS} 个交易日内没有任何扫描"
            f"（库里最后一次是 {recent_dates[-1]}）—— 扫描很可能已停摆"
        )

    def test_每日标的数没有腰斩(self, recent_dates):
        """标的数从 30 掉回 10 而扫描"成功"，是静默降级的另一种形状
        （例如 --tickers 默认值被覆盖、或限流后静默截断）。
        """
        with _con() as con:
            rows = con.execute(
                "SELECT date, COUNT(DISTINCT ticker) FROM predictions "
                "WHERE date >= ? GROUP BY date ORDER BY date",
                (recent_dates[0],),
            ).fetchall()
        counts = {d: n for d, n in rows}
        if len(counts) < 3:
            pytest.skip("扫描日不足")
        peak = max(counts.values())
        latest_date = max(counts)
        latest = counts[latest_date]
        assert latest >= peak * 0.5, (
            f"最近一次扫描（{latest_date}）只有 {latest} 只标的，"
            f"而窗口内峰值是 {peak} —— 疑似标的池被静默截断"
        )
