"""
signal_archive.analyze() 按世代切片（v0.45.265）

背景：`analyze()` / `load_panel()` 直接拉整张 `signal_archive` 表。v0.45.182 / v0.45.256
用「改名 + 抽取器按 census_source 取值」拆开了 `guard.consistency_census`，但有一类问题
抽取器处理不了：**生产者换了口径、抽取器与归档一致**。例如 v0.45.163 让
`agent.GuardBeeSentinel.score/direction` 换了量（risk_adj Δ 均值 −0.485、方向 26.2% 不一致），
全量 `--backfill --dry-run` 显示「改值 0」，而 `analyze()` 会把两个世代池化。
各蜂输出分每改一次逻辑就换一次量，逐次改名不可行 ⇒ 判别放在做聚合的那一层。

⚠️ 不一刀切：原始市场观测（options.* / insider 金额 / 行情 …）不读系统输出，
上游蜂改逻辑与它们无关，切它们只会白丢样本。
"""

import datetime
import os
import random
import sqlite3
import sys
from fnmatch import fnmatchcase

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


BOUNDARY = "2026-05-04"          # 周一
PRE = _bdays("2026-03-02", 40)   # 全在边界之前
POST = _bdays(BOUNDARY, 20)      # ≥16 天，split_stability 才会真的分段
assert PRE[-1] < BOUNDARY <= POST[0]
TICKERS = [f"T{i}" for i in range(10)]
W = len(TICKERS)
N_FULL, N_POST = (len(PRE) + len(POST)) * W, len(POST) * W


def _build_db(tmp_path):
    """合成库：旧世代里「值 ↔ 收益」正相关、新世代里负相关（`flip`），
    另有全程正相关的 `stable`。每天 10 只、收益 = x + 噪声。"""
    rng = random.Random(7)
    db = tmp_path / "p.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
        price_at_predict REAL, close_t7 REAL, checked_t7 INTEGER DEFAULT 0)""")
    con.commit()
    con.close()
    sa.ensure_schema(db)

    flip = ("agent.GuardBeeSentinel.score",   # 被边界直接改的系统输出
            "options.iv_current",             # 原始观测（同样异号，才能看出「没被切」）
            "guard.consistency")              # 退役名：不在 SIGNAL_EXTRACTORS
    stable = ("composite.final_score", "agent.ChronosBeeHorizon.score",
              "agent.BearBeeContrarian.score")
    pre_only = ("agent.GuardBeeSentinel.direction",)   # 只有旧世代数据 ⇒ 本世代 0 条

    preds, arch = [], []
    for d in PRE + POST:
        late = d >= BOUNDARY
        for tk in TICKERS:
            x = rng.gauss(0, 1)
            ret = x + rng.gauss(0, 0.3)
            preds.append((d, tk, 100.0, 100.0 * (1 + ret / 100.0), 1))
            for s in flip:
                arch.append((d, tk, s, -x if late else x))
            for s in stable:
                arch.append((d, tk, s, x))
            if not late:
                for s in pre_only:
                    arch.append((d, tk, s, x))
    with sqlite3.connect(db) as c:
        c.executemany("INSERT INTO predictions (date,ticker,price_at_predict,close_t7,checked_t7)"
                      " VALUES (?,?,?,?,?)", preds)
        c.executemany(f"INSERT INTO {sa.TABLE} (date,ticker,signal,value) VALUES (?,?,?,?)", arch)
    return db


@pytest.fixture
def one_boundary(monkeypatch):
    """一条边界、只直接改 Guard。影响面表与边界表都换成夹具，行为测试不依赖真实表的内容。"""
    monkeypatch.setattr(irr, "_COHORT_HISTORY", [(BOUNDARY, "vTEST", "测试边界")])
    monkeypatch.setattr(sa, "COHORT_SIGNAL_SCOPE", {"vTEST": ("agent.GuardBeeSentinel.*",)})


def _run(db, **kw):
    res = sa.analyze(db, draws=kw.pop("draws", 60), **kw)
    assert res, "夹具面板为空 —— 下面的断言全是空转"
    rows, floor, gens = res
    return {r["signal"]: r for r in rows}, floor, gens


class TestAnalyzeSlicesByGeneration:

    def test_system_output_uses_only_current_generation(self, tmp_path, one_boundary):
        rows, _, _ = _run(_build_db(tmp_path))
        g = rows["agent.GuardBeeSentinel.score"]
        assert g["n_samples"] == N_POST, (
            f"Guard 分被边界 {BOUNDARY} 重新定义，却用了 {g['n_samples']} 条（本世代 {N_POST} 条）"
            " —— 两个世代被池化")
        assert g["gen_start"] == BOUNDARY and g["gen_version"] == "vTEST"
        assert g["n_excluded"] == N_FULL - N_POST and g["n_days"] == len(POST)
        assert g["daily_ic"] < -0.5, "应描述新世代（负相关），不是两段的平均"
        # IC 之外还有两处跨日聚合，同样只许看本世代：
        assert g["ic_within"] < -0.5, "固定效应/时变分解仍在用全史面板"
        assert g["stability"] == "稳定" and g["ic_train"] < 0 and g["ic_test"] < 0, (
            f"训练/测试分段仍跨世代（{g['stability']}）—— 旧世代成了「训练期」")

    def test_raw_observation_keeps_full_history(self, tmp_path, one_boundary):
        """成对断言：没有这条，「所有信号一律只取最新世代」也能让上一条全绿。"""
        rows, _, _ = _run(_build_db(tmp_path))
        r = rows["options.iv_current"]
        assert r["n_samples"] == N_FULL, "原始观测不读系统输出，切它只会白丢样本"
        assert r["gen_start"] is None and r["n_excluded"] == 0

    def test_pool_generations_is_the_old_behaviour(self, tmp_path, one_boundary):
        """前提自证：夹具确实会被池化出错 —— 不切时 Guard 分的 IC 是两段异号的平均。"""
        rows, _, gens = _run(_build_db(tmp_path), pool_generations=True)
        g = rows["agent.GuardBeeSentinel.score"]
        assert g["n_samples"] == N_FULL
        assert abs(g["daily_ic"]) < 0.5, g["daily_ic"]
        assert gens["pooled"] is True

    def test_final_score_always_follows_cohort_history(self, tmp_path, one_boundary):
        """影响面里没写 composite.final_score 也要切：`_COHORT_HISTORY` 本来就是它的世代表，
        与 `ic_rerun_readiness.assess()` 用同一条边界。"""
        rows, _, _ = _run(_build_db(tmp_path))
        assert rows["composite.final_score"]["gen_start"] == BOUNDARY

    def test_downstream_of_changed_output_is_sliced(self, tmp_path, one_boundary):
        """Bear 读 Guard 的 consistency/conflict ⇒ Guard 换代，Bear 同一天换代。"""
        rows, _, _ = _run(_build_db(tmp_path))
        assert rows["agent.BearBeeContrarian.score"]["gen_start"] == BOUNDARY
        # 成对：不读 Guard 的蜂不许被连坐
        assert rows["agent.ChronosBeeHorizon.score"]["gen_start"] is None

    def test_unknown_signal_is_sliced_by_every_boundary(self, tmp_path, one_boundary):
        """退役名 / 不认识的名字：不知道它读什么 ⇒ 最保守。"""
        rows, _, _ = _run(_build_db(tmp_path))
        assert rows["guard.consistency"]["gen_start"] == BOUNDARY

    def test_undeclared_boundary_slices_everything_and_says_so(self, tmp_path, monkeypatch):
        """追加了边界却没声明影响面 ⇒ 按全部信号切，并在返回值里点名（谁会红？）。"""
        monkeypatch.setattr(irr, "_COHORT_HISTORY", [(BOUNDARY, "vNEW", "忘了声明")])
        monkeypatch.setattr(sa, "COHORT_SIGNAL_SCOPE", {})
        rows, _, gens = _run(_build_db(tmp_path))
        assert rows["options.iv_current"]["gen_start"] == BOUNDARY
        assert gens["undeclared_versions"] == ["vNEW"]

    def test_sliced_away_signal_is_reported_not_silently_dropped(self, tmp_path, one_boundary):
        """本世代 0 条的信号不进结果表 —— 但必须出现在世代报告里，
        否则「被切光」与「从来没有数据」长得一样。"""
        rows, _, gens = _run(_build_db(tmp_path))
        assert "agent.GuardBeeSentinel.direction" not in rows
        e = gens["signals"]["agent.GuardBeeSentinel.direction"]
        assert (e["n_in_generation"], e["n_excluded"]) == (0, len(PRE) * W)
        assert e["gen_start"] == BOUNDARY

    def test_sliced_signal_noise_floor_uses_its_own_skeleton(self, tmp_path, one_boundary):
        """地板对天数高度敏感（天数少 ⇒ 地板高）。被切到 20 天的信号若沿用 60 天骨架的地板，
        会系统性偏低 ⇒ 假阳性。未被切的信号仍用全表地板（既有行为不变）。"""
        rows, floor, _ = _run(_build_db(tmp_path), draws=100)
        # 实测 0.146 vs 0.104（×1.41，理论 √3≈1.73）；沿用全表地板的变异恰为 ×1.00
        assert rows["agent.GuardBeeSentinel.score"]["noise_floor"] > floor["ic_p95"] * 1.15
        assert rows["options.iv_current"]["noise_floor"] == floor["ic_p95"]

    def test_report_prints_generation_section(self, tmp_path, one_boundary, capsys):
        rows, floor, gens = sa.analyze(_build_db(tmp_path), draws=30)
        sa.print_report(rows, floor, "t7", generations=gens)
        out = capsys.readouterr().out
        assert "agent.GuardBeeSentinel.direction" in out, "被切光的信号没在报告里露面"
        assert BOUNDARY in out

    def test_report_warns_when_pooled(self, tmp_path, one_boundary, capsys):
        rows, floor, gens = sa.analyze(_build_db(tmp_path), draws=30, pool_generations=True)
        sa.print_report(rows, floor, "t7", generations=gens)
        assert "跨世代" in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════════════════
# 真实影响面表：与 `_COHORT_HISTORY` 对账
# ══════════════════════════════════════════════════════════════════════════

#: 不读任何系统输出、且没有任何边界直接改过其算法的信号 —— 必须保留全史。
#: 若日后某条边界确实改了其中一个的定义：把它从这里移除，并在 CHANGELOG 写明依据。
NEVER_SLICED_TODAY = (
    "options.iv_current", "options.put_call_ratio",
    "options.total_oi", "options.iv_rank", "options.iv_percentile", "options.iv_rank_is_real",
    "insider.filings", "insider.dollar_bought", "insider.dollar_sold",
    "insider.distinct_buyers", "insider.officer_buys",
    # v0.45.275: price.momentum_5d / price.volatility_20d 移出本表——各自在
    # 2026-08-15（scout_bee v0.43.25）/ 2026-08-26（data_pipeline v0.45.3）改过
    # 哨兵语义（缺失伪造 0.0 → 诚实 None），见 DOCUMENTED_REDEFINITIONS。
    # price.volume_ratio 留在本表：dataclass 默认值同批改了，但至少一条取值路径
    # （CBOESource）当时仍硬编码 `or 1.0` 未清，是否可达未查清，不认领。
    "price.volume_ratio",
    "fund.pe_ratio", "fund.market_cap", "market.fear_greed", "market.fear_greed_is_cnn",
    "sentiment.pct", "crowding.comp.social_volume", "crowding.comp.google_trends",
)

#: CHANGELOG 里实测过「换了量」的系统输出 → 其世代起点不得早于该日。
DOCUMENTED_REDEFINITIONS = {
    # v0.45.472（补登 v0.34.0）：期权链主源 yfinance → CBOE，主链 GEX 的 gamma 来源与 S 都换了——yfinance 时代 gamma 是
    # BS 补的、S 取行权价中位数；CBOE 时代 gamma 来自 CBOE。原在 NEVER_SLICED_TODAY，依据见 CHANGELOG v0.45.472。
    "options.gamma_exposure": "2026-06-30",
    "agent.GuardBeeSentinel.score": "2026-09-07",      # v0.45.163 普查读法
    "agent.GuardBeeSentinel.direction": "2026-09-07",  # v0.45.163 方向 26.2% 不一致
    "guard.consistency_census": "2026-09-07",          # v0.45.163
    # v0.45.349：Oracle 分与方向由 09-05（v0.45.128 IV 换源）/ 09-11（v0.45.201 去掉关键词投票）
    # 挪到 09-28 —— gex_signal 恒 1.0（那 +1 折到 Oracle 分 +0.846，负 GEX 47 行里方向翻 7 行，2026-09-27 调查实测）
    "agent.OracleBeeEcho.direction": "2026-09-28",
    "agent.OracleBeeEcho.score": "2026-09-28",
    "bear.options_bear": "2026-09-28",                 # v0.45.349 删 gex<0 ⇒ ≥5.0 下限（178 行负 GEX 触发 157 行）
    "bear.insider_bear": "2026-09-28",                 # 同上，经新依赖边：Oracle 方向 → consensus_strength → Scout 方向
    "bear.overval_bear": "2026-09-05",                 # v0.45.128 P/E 复活
    "ml.expected_7d": "2026-09-07",                    # v0.45.151 catalyst_quality
    "ml.expected_30d": "2026-09-07",
    "composite.swarm_agreement": "2026-09-11",         # 读全部蜂方向，含 Oracle
    "composite.final_score": "2026-09-09",             # v0.45.172 权重
    # v0.45.275：补登（P1 审计）—— 均在 `_COHORT_HISTORY` 第一条（08-17）前后
    # 真实发生、但从未进过 COHORT_SIGNAL_SCOPE 的哨兵语义 / 来源变更。
    "price.momentum_5d": "2026-08-15",                 # v0.43.25 scout_bee 缺失伪造 0.0→None
    "guard.macro_adj": "2026-08-15",                   # v0.43.24 Step2 VIX 改走 CBOE，降级日不再一刀切丢弃
    "agent.BuzzBeeWhisper.score": "2026-08-26",        # v0.45.2/3 volatility_20d/背离检测 None 语义
    "agent.BuzzBeeWhisper.direction": "2026-08-26",
    "price.volatility_20d": "2026-08-26",              # v0.45.3 data_pipeline 缺失伪造 0.0→None
    "agent.ChronosBeeHorizon.score": "2026-08-26",     # v0.45.31 抓取失败改 error（不再冒充 4.0）
    "agent.ChronosBeeHorizon.direction": "2026-08-26",
    "catalyst.count": "2026-08-26",                    # v0.45.31/32 同源：来源集合与失败语义都变了
    "catalyst.nearest_days": "2026-08-26",
    "catalyst.max_weight": "2026-08-26",
}


class TestCohortSignalScopeTable:

    def test_boundary_versions_are_unique(self):
        """前提：影响面表按 version 键，重名会让两条边界共用一份声明。"""
        vs = [v for _d, v, _r in irr._COHORT_HISTORY]
        assert len(vs) == len(set(vs)), vs

    def test_every_boundary_declares_its_scope(self):
        """追加世代边界的人必须回答「它改了哪些归档信号」—— 答不上来这里红。"""
        missing = [v for _d, v, _r in irr._COHORT_HISTORY if v not in sa.COHORT_SIGNAL_SCOPE]
        assert not missing, (
            f"{missing} 没在 signal_archive.COHORT_SIGNAL_SCOPE 声明影响面 —— "
            "analyze() 会按全部信号切（保守），原始观测白丢样本")

    def test_no_stale_scope_entries(self):
        known = {v for _d, v, _r in irr._COHORT_HISTORY}
        stale = sorted(set(sa.COHORT_SIGNAL_SCOPE) - known)
        assert not stale, f"影响面表里有 _COHORT_HISTORY 不存在的版本：{stale}"

    def test_every_pattern_resolves(self):
        """拼错一个模式 ⇒ 静默「不影响任何信号」，与「确实不影响」同形。"""
        names = set(sa.SIGNAL_EXTRACTORS) | set(sa.UNARCHIVED_NODES)
        pats = [(f"COHORT_SIGNAL_SCOPE[{v}]", p)
                for v, ps in sa.COHORT_SIGNAL_SCOPE.items() for p in ps]
        pats += [("SIGNAL_UPSTREAM 键", k) for k in sa.SIGNAL_UPSTREAM]
        pats += [(f"SIGNAL_UPSTREAM[{k}]", e.up)
                 for k, es in sa.SIGNAL_UPSTREAM.items() for e in es]
        dead = [(where, p) for where, p in pats if not any(fnmatchcase(n, p) for n in names)]
        assert not dead, dead

    def test_every_extractor_is_classified(self):
        """新增抽取器必须二选一：进 SIGNAL_LEAVES（不读系统输出），或在 SIGNAL_UPSTREAM
        登记它读哪些系统输出。都不写 ⇒ 日后上游换代时它会被静默池化。"""
        classified = set()
        for n in sa.SIGNAL_EXTRACTORS:
            as_leaf = n in sa.SIGNAL_LEAVES
            as_derived = any(fnmatchcase(n, k) for k in sa.SIGNAL_UPSTREAM)
            assert not (as_leaf and as_derived), f"{n} 同时被登记为叶子与派生"
            if as_leaf or as_derived or n in sa.ALWAYS_SLICED:
                classified.add(n)
        assert set(sa.SIGNAL_EXTRACTORS) == classified, sorted(set(sa.SIGNAL_EXTRACTORS) - classified)

    def test_final_score_generation_matches_readiness_gate(self):
        """与 `ic_rerun_readiness.assess()` 用同一条边界，两处不许漂开。"""
        g = sa.generation_boundaries(["composite.final_score"])
        assert g["composite.final_score"]["date"] == irr.cohort_start()["date"]

    @pytest.mark.parametrize("sig", NEVER_SLICED_TODAY)
    def test_raw_observations_are_not_sliced(self, sig):
        assert sig in sa.SIGNAL_EXTRACTORS, f"{sig} 已不是现役信号，更新本名单"
        assert sa.generation_boundaries([sig]) == {}, (
            f"{sig} 不读系统输出，却被切了世代 —— 一刀切会白丢样本")

    @pytest.mark.parametrize("sig,not_before", sorted(DOCUMENTED_REDEFINITIONS.items()))
    def test_documented_redefinitions_are_sliced(self, sig, not_before):
        g = sa.generation_boundaries([sig]).get(sig)
        assert g is not None and g["date"] >= not_before, (
            f"{sig} 在 {not_before} 换过量（见 CHANGELOG），世代起点却是 {g}")


# ══════════════════════════════════════════════════════════════════════════
# 读同伴的边 ⇔ 代码里的读板位点：双向对账（v0.45.561；前身是 v0.45.349 只管 Bear、只查漏边的那条）
# ══════════════════════════════════════════════════════════════════════════
#
# 为什么要双向：v0.45.349 那条只问「读了没登记」（漏边）。v0.45.561 修的毛病恰好是反方向的
# **多边** —— `ml.*` 一个通配让 `ml.expected_7d/30d` 挂着它根本不读的拥挤度与 Phase-1 方向，
# 于是 v0.45.445（换 Buzz 新闻源）这种无关边界把它们的序列白切了 32 天。只查一个方向的守卫
# 对这种错恒绿。
#
# 怎么对：AST 把各蜂模块里**每一个读板位点**找出来（`_read_peer` / 普查 / 拥挤度看多数 / 共振 / 快照），
# 每个位点读了条目的哪些字段也由 AST 取；人工维护的只有两张表 ——
#   · 位点表（下面的 `_PEER_SITES` / `_CENSUS_SITES`）：读到的字段**流向哪个入档信号**（数据流不做 AST）；
#   · 字段分类：方向 / 分 ⇒ 节点 `agent.<蜂>.direction|score`；details 的键逐个登记成节点或原始观测。
# 位点多了少了、字段多了少了、没分类的字段，都先在这一层红；再把位点表推出的（下游, 上游）节点对
# 与 `SIGNAL_UPSTREAM` 里**今天生效**的 `peer` / `census` 边逐对比较：漏边、多边都红。
# 已知不追踪：方法内部从读到的值到哪个输出的数据流（由位点表声明）；普查对象是谁（由阶段顺序声明，
# 见 `_PEERS_*`，顺序本身由 `tests/test_rival_bee_peer_features.py::TestPhaseOrderingContract` 钉着）。

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
#: swarm_agents/ 下**不扫**的模块 → (理由, 是否允许出现 board)。其余 *.py 一律当「可能读同伴的蜂」扫——
#: 清单由目录推出（v0.45.587；此前手写 7 个名字，新加一个读同伴的蜂模块守卫照样全绿，实测）。
#: 允许 False 的条目由 `test_module_inventory_is_complete` 核「源码里确实没有 board」，理由不是只写在这里。
_NOT_BEE_MODULES = {
    "__init__": ("包入口，只做导出", False),
    "_config": ("配置常量", False),
    "cache": ("缓存工具", False),
    "utils": ("打分 / 错误结果工具函数", False),
    "sentiment": ("BuzzBee 的情绪计算辅助，不持有 board", False),
    "base": ("读板机制本身（`_read_peer` 定点索引 + 排行榜回退、`publish`），读法由 "
             "tests/test_bear_bee_census_eviction.py 等专门测试管；各蜂经 `self._read_peer` 的调用在蜂模块里被扫", True),
    "queen_distiller": ("汇总层：读板结果（共振、快照）只进 composite.final_score——ALWAYS_SLICED、受全部边界约束，"
                        "不产生依赖边", True),
}
_BEE_MODULES = {p.stem: p for p in sorted((_REPO / "swarm_agents").glob("*.py"))
                if p.stem not in _NOT_BEE_MODULES}
#: 不在 swarm_agents/ 里的 Phase-1 蜂（`_PHASE1` 里的 CodeExecutorAgent）
_BEE_MODULES["code_executor_agent"] = _REPO / "code_executor_agent.py"

#: 读板的机制本身（不是消费者）：它们的内部读法由各自的专门测试管（定点索引 / 普查回退）
_MECHANISM_METHODS = {("guard_bee", "_read_census")}

#: 条目（`PheromoneEntry`）上不携带系统输出数值的字段 —— 读它们不构成边
_IDENTITY = "identity"
#: `discovery` 是一句话摘要：消费方只拿它判「条目在不在」、或用正则抠原样转述的原始量（内幕金额 / P/C /
#: 情绪百分比的兜底解析，取值与 details 里登记过的同一个量相同），或整句拼进 LLM 提示词
_RAW_DISCOVERY = "raw: 摘要文本（存在性闸 / 正则抠原样转述的量 / LLM 提示词）"

#: 阶段顺序：Phase-1 并行 → Rival → Guard → Bear（`alpha_hive_daily_report` 的分阶段调用）
_PEERS_BEFORE_RIVAL = sa._PHASE1
_PEERS_BEFORE_GUARD = sa._PHASE1 + ("RivalBeeVanguard",)
_PEERS_BEFORE_BEAR = _PEERS_BEFORE_GUARD + ("GuardBeeSentinel",)

#: Bear 两个子分方法里同伴的 self_score 只拼进看空理由的**文字**（`f"…（{x.self_score:.1f}分）"`），
#: 不进子分；LLM 模式下理由文字进提示词、LLM 的分回写 bear_score ⇒ 按代码出边保守挂到 bear.score
_SCORE_IN_REASON_TEXT = ("bear.score",)

#: `_read_peer` 位点：(模块, 方法, 被读的蜂) → {字段: (上游节点模式, 下游信号模式…) | 不成边的理由}
_PEER_SITES = {
    ("rival_bee", "analyze", "ChronosBeeHorizon"): {
        # 只进 `catalyst_quality_from_score` → `expected_returns`；Rival 的分 v0.45.538 起 = 5.0 + 技术指标
        "self_score": ("agent.ChronosBeeHorizon.score", ("ml.expected_7d", "ml.expected_30d")),
    },
    ("bear_bee", "_assess_insider_selling", "ScoutBeeNova"): {
        "direction": ("agent.ScoutBeeNova.direction", ("bear.insider_bear",)),
        "self_score": ("agent.ScoutBeeNova.score", _SCORE_IN_REASON_TEXT),
        "discovery": _RAW_DISCOVERY,
        "details.insider_sold_usd": "raw: SEC Form 4 金额（入档为叶子 insider.dollar_sold）",
        "details.insider_bought_usd": "raw: SEC Form 4 金额（入档为叶子 insider.dollar_bought）",
    },
    ("bear_bee", "_assess_options_puts", "OracleBeeEcho"): {
        "direction": ("agent.OracleBeeEcho.direction", ("bear.options_bear",)),
        "self_score": ("agent.OracleBeeEcho.score", _SCORE_IN_REASON_TEXT),
        "discovery": _RAW_DISCOVERY,
        "details.pc_ratio": "raw: 期权链 P/C（入档为叶子 options.put_call_ratio）",
        "details.iv_rank": "raw: IV Rank（入档为叶子 options.iv_rank）",
        "details.iv_skew": "raw: 25Δ put/call IV 比（期权链观测）",
        "details.gex": "raw: 主链 GEX（入档为叶子 options.gamma_exposure）；v0.45.349 起只进 options_data（LLM 提示词）",
    },
    ("bear_bee", "_assess_news_sentiment", "BuzzBeeWhisper"): {
        # Buzz 看空 ⇒ news_bear 下限 5.5（v0.45.561 对账时发现，此前表里没有这条 peer 边）
        "direction": ("agent.BuzzBeeWhisper.direction", ("bear.score",)),
        "self_score": ("agent.BuzzBeeWhisper.score", _SCORE_IN_REASON_TEXT),
        "discovery": _RAW_DISCOVERY,
        # Buzz 发布的 sentiment_score 就是归档 details.sentiment_pct 的同一个 bullish_pct（buzz_bee.py）
        "details.sentiment_score": ("sentiment.pct", ("bear.score",)),
        "details.reddit_momentum": "raw: ApeWisdom 提及排名变化（reddit_data.momentum 原样转发）",
    },
    ("bear_bee", "_assess_catalyst_risk", "ChronosBeeHorizon"): {
        "discovery": _RAW_DISCOVERY,
        "details.nearest_days": ("catalyst.nearest_days", ("bear.score",)),
        "details.analyst_upside_pct": "raw: yfinance 分析师目标价相对现价",
    },
    ("bear_bee", "_assess_ml_prediction", "RivalBeeVanguard"): {
        "discovery": _RAW_DISCOVERY,
        "details.expected_7d": ("ml.expected_7d", ("bear.score",)),
    },
    ("bear_bee", "_assess_signal_consistency", "GuardBeeSentinel"): {
        "discovery": _RAW_DISCOVERY,
        "details.consistency": ("guard.consistency_census", ("bear.score",)),
        # 只有 LLM 模式才非空（生产恒 --no-llm）；按代码出边保守列。不入档 ⇒ 以 Guard 的输出代表
        "details.conflict_type": ("agent.GuardBeeSentinel.*", ("bear.score",)),
    },
}

#: 被 `return` 出位点方法的条目：(模块, 方法, 被读的蜂) → 理由。出去以后在本模块别处读的字段并回该位点。
_PEER_ENTRY_ESCAPES = {
    ("bear_bee", "_assess_news_sentiment", "BuzzBeeWhisper"):
        "analyze 把它传给 _generate_llm_bear_thesis，只读 .discovery 拼 LLM 提示词",
}

#: 普查位点：(模块, 方法, 读法) → (读到哪些蜂, {字段: 下游信号模式… | 不成边的理由}, 字段从哪儿 AST)
#: 字段来源 = (文件, 函数, 取条目列表的调用名…)；None = 取不到字段（整份交给 LLM），按代码出边保守列。
_CENSUS_SITES = {
    ("guard_bee", "analyze", "_read_census"): (
        _PEERS_BEFORE_GUARD,
        {"self_score": ("agent.GuardBeeSentinel.*",),                       # avg_score
         "direction": ("agent.GuardBeeSentinel.*", "guard.consistency_census",  # 一致性 / 多数方向
                       "guard.adj_factor")},                                # 看多数 → 拥挤度
        ("swarm_agents/guard_bee.py", "analyze", "_read_census"),
    ),
    ("guard_bee", "analyze", "detect_resonance"): (
        tuple(a for a in _PEERS_BEFORE_GUARD if a not in {"GuardBeeSentinel"}),   # RESONANCE_EXCLUDED_AGENTS
        {"direction": ("agent.GuardBeeSentinel.*",), "agent_id": _IDENTITY},
        ("pheromone_board.py", "detect_resonance", "_live_agent_entries"),
    ),
    ("guard_bee", "analyze", "get_real_crowding_metrics"): (
        # `real_data_sources._PHASE1_PEERS`（不含 Scout）；Guard 随后用自己普查的 bull 覆盖 bullish_agents
        ("OracleBeeEcho", "BuzzBeeWhisper", "ChronosBeeHorizon", "CodeExecutorAgent"),
        {"direction": ("guard.adj_factor",), "agent_id": _IDENTITY},
        ("real_data_sources.py", "get_bullish_agents_detail", "get_live_signals"),
    ),
    ("guard_bee", "analyze", "snapshot"): (
        _PEERS_BEFORE_GUARD,
        # LLM 冲突合成（生产恒 --no-llm）：整份快照交给 LLM ⇒ 分与方向都算读了
        {"self_score": ("agent.GuardBeeSentinel.*",), "direction": ("agent.GuardBeeSentinel.*",)},
        None,
    ),
    ("bear_bee", "_generate_llm_bear_thesis", "get_live_signals"): (
        _PEERS_BEFORE_BEAR,
        # LLM 看空论点（生产恒 --no-llm）：看多同伴的分 / 方向 / 摘要进提示词，LLM 的 bear_score 回写 final_bear_score
        {"self_score": ("bear.score",), "direction": ("bear.score",),
         "agent_id": _IDENTITY, "discovery": _RAW_DISCOVERY},
        ("swarm_agents/bear_bee.py", "_generate_llm_bear_thesis", "get_live_signals"),
    ),
    # v0.45.441 起 Scout 传 `peer_census=False`：不读板（登记成空表，翻回 True 就会多出读点而红）
    ("scout_bee", "_analyze", "get_real_crowding_metrics"): ((), {}, None),
}

_BOARD_FUNCS = frozenset({"get_real_crowding_metrics", "get_bullish_agents_count",
                          "get_bullish_agents_detail"})
_SELF_READS = frozenset({"_read_peer", "_read_census"})


def _parse(rel_or_path) -> ast.Module:
    path = rel_or_path if isinstance(rel_or_path, Path) else _REPO / rel_or_path
    return ast.parse(path.read_text(encoding="utf-8"))


def _functions(tree: ast.Module) -> dict:
    """模块顶层函数 + 类里的方法（各蜂模块一个类，方法名不撞）。"""
    out = {}
    for n in tree.body:
        if isinstance(n, ast.ClassDef):
            out.update({m.name: m for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))})
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = n
    return out


def _call_name(call: ast.Call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)


def _is_self_board(node) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "board"
            and isinstance(node.value, ast.Name) and node.value.id == "self")


def _board_read_sites():
    """各蜂模块里的全部读板位点：{(模块, 方法, 读法): 调用节点列表}，读法 = 被读的蜂（_read_peer）或调用名。

    认这几种写法：`self._read_peer(t, "<蜂>")`、`self._read_census(t)`、`self.board.<任何非 publish 的属性>`、
    `getattr(self.board, "<名>")`、以及 `get_real_crowding_metrics` 等读板函数（`peer_census=False` 的不算读）。
    """
    sites, problems = {}, []
    for mod, path in _BEE_MODULES.items():
        for meth, fn in _functions(_parse(path)).items():
            if (mod, meth) in _MECHANISM_METHODS:
                continue
            problems += [f"{mod}.{meth}: {p}" for p in _escaped_board_uses(fn)]
            for n in ast.walk(fn):
                how = None
                if isinstance(n, ast.Call):
                    name = _call_name(n)
                    if (name in _SELF_READS and isinstance(n.func, ast.Attribute)
                            and isinstance(n.func.value, ast.Name) and n.func.value.id == "self"):
                        if name == "_read_peer":
                            if len(n.args) < 2 or not isinstance(n.args[1], ast.Constant):
                                problems.append(f"{mod}.{meth}: _read_peer 的蜂名不是字面量，AST 认不出读谁")
                                continue
                            how = n.args[1].value
                        else:
                            how = name
                    elif name in _BOARD_FUNCS:
                        census = next((k.value for k in n.keywords if k.arg == "peer_census"), None)
                        if census is not None and not (isinstance(census, ast.Constant) and census.value is False):
                            problems.append(f"{mod}.{meth}: peer_census 不是字面量 False/缺省，AST 判不了读不读板")
                        how = name
                    elif name == "getattr" and n.args and _is_self_board(n.args[0]) \
                            and len(n.args) > 1 and isinstance(n.args[1], ast.Constant):
                        how = n.args[1].value
                elif isinstance(n, ast.Attribute) and _is_self_board(n.value) and n.attr != "publish":
                    how = n.attr
                if how is not None:
                    sites.setdefault((mod, meth, how), []).append(n)
    return sites, problems


def _escaped_board_uses(fn) -> list:
    """`self.board` 出现在 AST 追不到读法的位置 ⇒ 每处一条问题（v0.45.587）。

    认得的用法只有：`self.board.<属性>`、`getattr(self.board, "<名>")`、直接作为参数交给已登记的读板函数
    （`_BOARD_FUNCS`）、真假判断（`if self.board` / `and` / `not` / `is None`）、以及给它赋值。
    其余——赋给别名（`b = self.board; b.get_live_signals(t)`）、当参数传给别的函数、放进容器——读法都看不见，
    此前一律静默绿（实测：Rival 里经别名读普查，本守卫全绿）。
    """
    parent = {c: p for p in ast.walk(fn) for c in ast.iter_child_nodes(p)}
    out = []
    for n in ast.walk(fn):
        if not _is_self_board(n) or isinstance(n.ctx, ast.Store):
            continue
        p = parent.get(n)
        ok = ((isinstance(p, ast.Attribute) and p.value is n)
              or (isinstance(p, ast.Call) and _call_name(p) == "getattr" and p.args and p.args[0] is n)
              or (isinstance(p, ast.Call) and _call_name(p) in _BOARD_FUNCS and n in p.args)
              or (isinstance(p, ast.keyword) and isinstance(parent.get(p), ast.Call)
                  and _call_name(parent[p]) in _BOARD_FUNCS)
              or (isinstance(p, (ast.If, ast.IfExp, ast.While)) and p.test is n)
              or isinstance(p, ast.BoolOp)
              or (isinstance(p, ast.UnaryOp) and isinstance(p.op, ast.Not))
              or (isinstance(p, ast.Compare) and all(isinstance(o, (ast.Is, ast.IsNot)) for o in p.ops)))
        if not ok:
            out.append(f"第 {n.lineno} 行 `self.board` 被赋给别名 / 外传（{type(p).__name__}），"
                       "AST 追不到读了谁、读了什么——改成直接 `self.board.<方法>` 或 `self._read_peer`，或登记新写法")
    return out


def _entry_reads(fn, var: str) -> set:
    """方法 `fn` 里读了绑定在 `var` 上的条目的哪些字段：属性、`getattr(var, "x")`、details 的键。"""
    fields, details_vars = set(), set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == var:
            fields.add(n.attr)
        elif (isinstance(n, ast.Call) and _call_name(n) == "getattr" and len(n.args) >= 2
              and isinstance(n.args[0], ast.Name) and n.args[0].id == var
              and isinstance(n.args[1], ast.Constant)):
            fields.add(n.args[1].value)
    if "details" in fields:
        fields.discard("details")
        for n in ast.walk(fn):    # D = getattr(var, "details", {}) or {}   /   D = var.details or {}
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                if any((isinstance(x, ast.Attribute) and x.attr == "details"
                        and isinstance(x.value, ast.Name) and x.value.id == var)
                       or (isinstance(x, ast.Call) and _call_name(x) == "getattr" and len(x.args) >= 2
                           and isinstance(x.args[0], ast.Name) and x.args[0].id == var
                           and isinstance(x.args[1], ast.Constant) and x.args[1].value == "details")
                       for x in ast.walk(n.value)):
                    details_vars.add(n.targets[0].id)
        keys = set()
        for n in ast.walk(fn):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                    and isinstance(n.func.value, ast.Name) and n.func.value.id in details_vars
                    and n.args and isinstance(n.args[0], ast.Constant)):
                keys.add(n.args[0].value)
            elif (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                  and n.value.id in details_vars and isinstance(n.slice, ast.Constant)):
                keys.add(n.slice.value)
            elif (isinstance(n, ast.Compare) and isinstance(n.left, ast.Constant)
                  and any(isinstance(op, (ast.In, ast.NotIn)) for op in n.ops)
                  and any(isinstance(c, ast.Name) and c.id in details_vars for c in n.comparators)):
                keys.add(n.left.value)
        # details 被读却一个键也没认出来 ⇒ 整体外传 / 写法没见过，宁可红
        fields |= {f"details.{k}" for k in keys} if keys else {"details.<无法识别>"}
    return fields


def _peer_site_fields():
    """`_read_peer` 位点 → AST 实测字段；`return` 出方法的条目，再并上本模块里同名变量的读取。"""
    out, escapes = {}, set()
    for mod, path in _BEE_MODULES.items():
        funcs = _functions(_parse(path))
        for meth, fn in funcs.items():
            for n in ast.walk(fn):
                if not (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                        and _call_name(n.value) == "_read_peer" and len(n.value.args) >= 2
                        and isinstance(n.value.args[1], ast.Constant)):
                    continue
                var = next(t.id for t in n.targets if isinstance(t, ast.Name))
                key = (mod, meth, n.value.args[1].value)
                fields = _entry_reads(fn, var)
                if any(isinstance(r, ast.Return) and r.value is not None
                       and any(isinstance(x, ast.Name) and x.id == var for x in ast.walk(r.value))
                       for r in ast.walk(fn)):
                    escapes.add(key)
                    for other, ofn in funcs.items():
                        if other != meth:
                            fields |= {f for f in _entry_reads(ofn, var) if not f.startswith("details.<")}
                out.setdefault(key, set()).update(fields)
    return out, escapes


def _census_fields(rel: str, func: str, source: str) -> set:
    """`func` 里经 `source` 调用取得的条目列表（及由它派生的列表），其元素被读了哪些属性。"""
    fn = _functions(_parse(rel))[func]
    lists, callables = set(), set()

    def yields(e) -> bool:
        if isinstance(e, ast.Call):
            nm = _call_name(e)
            if nm == source or (isinstance(e.func, ast.Name) and e.func.id in callables):
                return True
            return nm in ("list", "sorted", "tuple", "reversed") and bool(e.args) and yields(e.args[0])
        if isinstance(e, ast.BoolOp):
            return any(yields(v) for v in e.values)
        if isinstance(e, ast.IfExp):
            return yields(e.body) or yields(e.orelse)
        if isinstance(e, ast.Name):
            return e.id in lists
        if isinstance(e, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
            return any(yields(g.iter) for g in e.generators)
        return False

    grew = True
    while grew:
        before = (len(lists), len(callables))
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    tgt = t.elts[0] if isinstance(t, ast.Tuple) and t.elts else t
                    if isinstance(tgt, ast.Name) and yields(n.value):
                        lists.add(tgt.id)
                    if (isinstance(tgt, ast.Name) and isinstance(n.value, ast.Call)
                            and _call_name(n.value) == "getattr" and len(n.value.args) >= 2
                            and isinstance(n.value.args[1], ast.Constant) and n.value.args[1].value == source):
                        callables.add(tgt.id)
        grew = (len(lists), len(callables)) != before
    # 属性只在「迭代普查列表」的那个推导式 / for 循环体内收（同名循环变量在别处另有所指，如 Guard 的快照 dict）
    attrs = set()
    for n in ast.walk(fn):
        scopes = []
        if isinstance(n, (ast.ListComp, ast.GeneratorExp, ast.SetComp, ast.DictComp)):
            scopes = [(g.target, n) for g in n.generators if yields(g.iter)]
        elif isinstance(n, ast.For) and yields(n.iter):
            scopes = [(n.target, ast.Module(body=n.body, type_ignores=[]))]
        for target, scope in scopes:
            if isinstance(target, ast.Name):
                attrs |= {x.attr for x in ast.walk(scope) if isinstance(x, ast.Attribute)
                          and isinstance(x.value, ast.Name) and x.value.id == target.id}
    return attrs


def _universe() -> set:
    return set(sa.SIGNAL_EXTRACTORS) | set(sa.UNARCHIVED_NODES)


def _pairs(downs, ups, universe) -> set:
    d_nodes = sa._matching(universe, downs)
    u_nodes = sa._matching(universe, ups)
    assert d_nodes and u_nodes, f"模式解析为空：{downs} ← {ups}"
    return {(d, u) for d in d_nodes for u in u_nodes}


def _pairs_from_code(kind: str) -> set:
    """位点表（字段已经 AST 核过）推出的（下游节点, 上游节点）对；`kind` = "peer" 或 "census"。"""
    universe, out = _universe(), set()
    for (_mod, _meth, peer), fields in (_PEER_SITES.items() if kind == "peer" else ()):
        for spec in fields.values():
            if isinstance(spec, tuple):
                up, downs = spec
                out |= _pairs(downs, (up,), universe)
    for (_mod, _meth, _how), (peers, fields, _src) in (_CENSUS_SITES.items() if kind == "census" else ()):
        for field, spec in fields.items():
            if not isinstance(spec, tuple):
                continue
            node = {"direction": "direction", "self_score": "score"}[field]
            out |= _pairs(spec, tuple(f"agent.{p}.{node}" for p in peers), universe)
    return out


def _pairs_from_table(kinds=("peer", "census"), on="9999-12-31") -> set:
    """`SIGNAL_UPSTREAM` 里 `on` 当天生效的这几类边 → 节点对（缺省 = 今天仍生效、没有止期的）。"""
    universe, out = _universe(), set()
    for down, edges in sa.SIGNAL_UPSTREAM.items():
        ups = tuple(e.up for e in edges if e.via in kinds and sa.edge_active(e, on))
        if ups:
            out |= _pairs((down,), ups, universe)
    return out


class TestPeerReadEdgesMatchCode:

    def test_module_inventory_is_complete(self):
        """扫哪些模块由 swarm_agents/*.py 推出；排除项必须真实存在，声称「不碰 board」的源码里确实没有 board。
        变红的变异：新建一个读同伴的 swarm_agents 模块（被扫 ⇒ 位点没登记而红）；排除项里写一个不存在的模块名。"""
        stems = {p.stem for p in (_REPO / "swarm_agents").glob("*.py")}
        assert set(_NOT_BEE_MODULES) <= stems, f"排除项里有不存在的模块：{sorted(set(_NOT_BEE_MODULES) - stems)}"
        assert stems - set(_NOT_BEE_MODULES) == set(_BEE_MODULES) - {"code_executor_agent"}
        touching = [m for m, (_why, may) in _NOT_BEE_MODULES.items()
                    if not may and "board" in (_REPO / "swarm_agents" / f"{m}.py").read_text(encoding="utf-8")]
        assert not touching, f"{touching} 登记为不碰 board，源码里却出现了 board —— 移出排除项让守卫扫它"
        assert {"scout_bee", "rival_bee", "guard_bee", "bear_bee"} <= set(_BEE_MODULES), "推出的清单漏了已知读同伴的蜂"

    def test_alias_board_use_is_flagged(self):
        """正反对照：别名读板要报出来；直接 `self.board.<方法>`、`if self.board`、传给已登记的读板函数不报。"""
        bad = ast.parse("def f(self, t):\n    b = self.board\n    return b.get_live_signals(t)\n").body[0]
        good = ast.parse("def f(self, t, s):\n    if self.board and self.board is not None:\n"
                         "        get_real_crowding_metrics(t, s, self.board, peer_census=False)\n"
                         "        return self.board.get_live_signals(t)\n").body[0]
        assert len(_escaped_board_uses(bad)) == 1, _escaped_board_uses(bad)
        assert _escaped_board_uses(good) == [], _escaped_board_uses(good)

    def test_every_board_read_site_is_registered(self):
        """位点多一个少一个都红：新加一处 `_read_peer` / 普查却没登记数据流 ⇒ 依赖表无从对账。"""
        sites, problems = _board_read_sites()
        assert not problems, problems
        registered = set(_PEER_SITES) | set(_CENSUS_SITES)
        assert set(sites) == registered, {
            "代码里有、表里没有（新读点，登记它读的字段流向哪）": sorted(set(sites) - registered),
            "表里有、代码里没有（读点已删，删登记 + 核依赖边）": sorted(registered - set(sites))}
        # 拥挤度取数：`peer_census=False` ⇔ 登记成「不读板」（空字段表）；两头各红一次
        for key, calls in sites.items():
            if key[2] != "get_real_crowding_metrics":
                continue
            reads = not all(any(k.arg == "peer_census" and isinstance(k.value, ast.Constant)
                                and k.value.value is False for k in c.keywords) for c in calls)
            assert reads == bool(_CENSUS_SITES[key][1]), (
                f"{key}：代码{'读' if reads else '不读'}板（peer_census），登记却{'不读' if reads else '读'}")

    def test_peer_site_fields_are_exactly_registered(self):
        """`_read_peer` 位点读了哪些字段（含 details 的键）—— AST 实测与登记逐个相等，两个方向都红。"""
        got, escapes = _peer_site_fields()
        assert set(got) == set(_PEER_SITES), (sorted(got), sorted(_PEER_SITES))
        bad = {k: {"读了没登记": sorted(got[k] - set(v)), "登记了没读": sorted(set(v) - got[k])}
               for k, v in _PEER_SITES.items() if got[k] != set(v)}
        assert not bad, bad
        assert escapes == set(_PEER_ENTRY_ESCAPES), (
            f"被 return 出方法的条目变了：{sorted(escapes)} —— 出去以后读了什么、流向哪，登记到 _PEER_ENTRY_ESCAPES")

    def test_census_site_fields_are_exactly_registered(self):
        """普查位点读了条目的哪些属性 —— AST 实测与登记相等（取不到字段的 LLM 快照除外，按代码出边保守列）。"""
        bad = {}
        for key, (peers, fields, src) in _CENSUS_SITES.items():
            if src is None:
                continue
            got = _census_fields(*src)
            assert got, f"{key}: 一个字段也没认出来 —— 取数写法变了，AST 失明（宁可红）"
            if got != set(fields):
                bad[key] = {"读了没登记": sorted(got - set(fields)), "登记了没读": sorted(set(fields) - got)}
        assert not bad, bad

    def test_census_peer_sets_follow_code_constants(self):
        """普查读到谁由阶段顺序与两个过滤常量决定：常量变了，位点表跟着改。"""
        import pheromone_board
        import real_data_sources
        assert set(_CENSUS_SITES[("guard_bee", "analyze", "detect_resonance")][0]) == (
            set(_PEERS_BEFORE_GUARD) - set(pheromone_board.PheromoneBoard.RESONANCE_EXCLUDED_AGENTS))
        assert set(_CENSUS_SITES[("guard_bee", "analyze", "get_real_crowding_metrics")][0]) == set(
            real_data_sources._PHASE1_PEERS)

    def test_every_field_is_classified(self):
        """字段要么成边（给出上游节点与下游信号），要么写明为什么不成边。"""
        for key, fields in list(_PEER_SITES.items()) + [(k, v[1]) for k, v in _CENSUS_SITES.items()]:
            for f, spec in fields.items():
                assert (isinstance(spec, tuple) or (isinstance(spec, str) and spec)), (key, f, spec)
                if isinstance(spec, str):
                    assert f in ("discovery", "agent_id") or f.startswith("details."), (
                        f"{key}.{f}：方向 / 分是系统输出，不能登成「不成边」")

    def test_table_peer_edges_equal_code_reads(self):
        """核心断言：今天生效的 peer / census 边 ≡ 代码里读同伴推出的边（节点对逐对比）。

        变红的变异（v0.45.561 实测）：删 `ml.expected_7d ← agent.ChronosBeeHorizon.score`（漏边）；
        给 `ml.expected_7d` 加回 `_PHASE1_DIRS`（多边 —— 正是本版修掉的那条）；
        删 Bear 两个子分读同伴方向的边（v0.45.349 之前的状态）。
        """
        # 按读法分开比：并起来比的话，一条 LLM 普查边会把同一对节点的 peer 漏边盖住（v0.45.561 变异实测）
        for kind in ("peer", "census"):
            code, table = _pairs_from_code(kind), _pairs_from_table((kind,))
            assert code == table, {
                "读法": kind,
                "读了没登记（漏边：上游换代时下游被静默池化）": sorted(code - table),
                "登记了没读（多边：无关边界白切下游）": sorted(table - code)}


# ══════════════════════════════════════════════════════════════════════════
# 依赖边的生效区间（v0.45.561）
# ══════════════════════════════════════════════════════════════════════════

class TestEdgeWindows:

    def test_edges_are_well_formed(self):
        for down, edges in sa.SIGNAL_UPSTREAM.items():
            assert isinstance(edges, tuple) and edges, down
            for e in edges:
                assert isinstance(e, sa.Edge) and e.via in sa.EDGE_KINDS, (down, e)
                for d in (e.since, e.until):
                    assert d is None or datetime.date.fromisoformat(d), (down, e)
                assert not (e.since and e.until) or e.since < e.until, (down, e)

    def test_window_endpoints_are_boundaries(self):
        """区间端点 = 下游自己换了定义（多读 / 少读了一个量）⇒ 那天必须有一条边界的闭包盖住下游，
        否则那次换代被静默池化。变红的变异：把 consensus_strength 那条的止期改成 10-10（当天无边界）。"""
        universe, missing = _universe(), []
        for down, edges in sa.SIGNAL_UPSTREAM.items():
            nodes = sa._matching(universe, (down,))
            for e in edges:
                for end in (e.since, e.until):
                    if end is None:
                        continue
                    if not any(d == end and v in sa.COHORT_SIGNAL_SCOPE
                               and nodes <= sa._scope_closure(sa.COHORT_SIGNAL_SCOPE[v], universe, on=end)
                               for d, v, _r in irr._COHORT_HISTORY):
                        missing.append((down, e.up, e.via, end))
        assert not missing, missing

    def test_generation_boundaries_use_edges_active_on_the_boundary_day(self, monkeypatch):
        """`generation_boundaries` 必须按边界当天的边闭包 —— 止期之后的上游边界不再传下来，起期之前的也不传。
        变红的变异：`generation_boundaries` 里调 `_scope_closure` 时不传 `on=date`（v0.45.561 之前的行为）。"""
        monkeypatch.setattr(sa, "COHORT_SIGNAL_SCOPE", {"vA": ("agent.OracleBeeEcho.score",),
                                                        "vB": ("agent.OracleBeeEcho.score",)})
        hist = [("2099-01-05", "vA", "边内"), ("2099-02-02", "vB", "边外")]
        monkeypatch.setattr(sa, "SIGNAL_UPSTREAM", {
            "ml.expected_7d": (sa.Edge("agent.OracleBeeEcho.score", "peer", until="2099-02-01"),)})
        assert sa.generation_boundaries(["ml.expected_7d"], hist)["ml.expected_7d"]["version"] == "vA"
        monkeypatch.setattr(sa, "SIGNAL_UPSTREAM", {
            "ml.expected_7d": (sa.Edge("agent.OracleBeeEcho.score", "peer", since="2099-02-01"),)})
        assert sa.generation_boundaries(["ml.expected_7d"], hist[:1]) == {}
        assert sa.generation_boundaries(["ml.expected_7d"], hist)["ml.expected_7d"]["version"] == "vB"

    def test_edge_active_is_half_open(self):
        e = sa.Edge("x", "peer", "2026-08-17", "2026-10-09")
        assert not sa.edge_active(e, "2026-08-16") and sa.edge_active(e, "2026-08-17")
        assert sa.edge_active(e, "2026-10-08") and not sa.edge_active(e, "2026-10-09")
        assert sa.edge_active(e, None), "不带日期 ⇒ 生效过就算（保守并集）"

    def test_ended_edge_still_carries_older_boundaries(self):
        """consensus_strength ← 同伴方向止于 10-09（v0.45.441）：09-28 的 v0.45.349（Oracle 方向）仍经它
        切 Scout；10-09 的 v0.45.445（Buzz 新闻）不再经它切。这正是 441 当时不敢删这条边的原因。"""
        universe = _universe()
        on_349 = sa._scope_closure(sa.COHORT_SIGNAL_SCOPE["v0.45.349"], universe, on="2026-09-28")
        on_445 = sa._scope_closure(sa.COHORT_SIGNAL_SCOPE["v0.45.445"], universe, on="2026-10-09")
        assert {"crowding.comp.consensus_strength", "agent.ScoutBeeNova.direction"} <= on_349, sorted(on_349)
        assert "crowding.comp.consensus_strength" not in on_445, sorted(on_445)
        assert "guard.consistency_census" in on_445, "Guard 普查读 Buzz 方向，这条边今天仍在"

    @pytest.mark.parametrize("version", ["v0.45.30", "v0.45.349", "v0.45.441", "v0.45.445", "v0.45.538"])
    def test_ml_expected_not_cut_by_boundaries_it_does_not_read(self, version):
        """`ml.expected_*` 只读 ChronosBee 分（与原始动量）：拥挤度 / 同伴方向 / Rival 分的边界不该切它。
        变红的变异：给 `ml.expected_7d` 加回 `crowding.score` 或 `_PHASE1_DIRS`。"""
        date = next(d for d, v, _r in irr._COHORT_HISTORY if v == version)
        hit = sa._scope_closure(sa.COHORT_SIGNAL_SCOPE[version], _universe(), on=date)
        assert not hit & {"ml.expected_7d", "ml.expected_30d", "agent.RivalBeeVanguard.direction"}, sorted(hit)

    def test_ml_expected_generation_set_by_its_own_inputs(self):
        """当前世代起点必须来自直接点名 `ml.expected_*` 或其唯一上游（ChronosBee 分）的边界。"""
        g = sa.generation_boundaries(["ml.expected_7d", "ml.expected_30d"])
        for sig in ("ml.expected_7d", "ml.expected_30d"):
            scope = sa.COHORT_SIGNAL_SCOPE[g[sig]["version"]]
            assert sa._matching({sig, "agent.ChronosBeeHorizon.score"}, scope), (sig, g[sig], scope)


# ══════════════════════════════════════════════════════════════════════════
# 行为差分：只动同伴方向 / 拥挤度，Rival 的 expected_7d/30d 与方向逐位不变（证明非依赖）
# ══════════════════════════════════════════════════════════════════════════

class TestRivalExpectedReturnsIgnorePeers:
    """AST 只证明「没读」；这里证明「读了也不影响」——两层各挡一种回归。

    正对照：同一套夹具里只改 ChronosBee 的分（B+ → A+），expected_* 必须变 ⇒ 夹具不是恒定的。
    变红的变异：在 `rival_bee.analyze` 里把 `_read_peer(ticker, "ScoutBeeNova").direction` 或
    `get_real_crowding_metrics` 的结果掺进 `momentum_5d`。
    """

    _PEERS = ("ScoutBeeNova", "OracleBeeEcho", "BuzzBeeWhisper", "CodeExecutorAgent")

    @pytest.fixture(autouse=True)
    def _offline(self, stub_yfinance, stub_reddit):
        pass

    def _run(self, monkeypatch, *, peer_dir, peer_score, crowd, chronos_score=7.0, chronos_dir="neutral"):
        import crowding_detector
        import real_data_sources
        from pheromone_board import PheromoneBoard, PheromoneEntry
        from swarm_agents.rival_bee import RivalBeeVanguard

        def entry(agent, score, direction):
            return PheromoneEntry(agent_id=agent, ticker="TEST", discovery="x", source="test",
                                  self_score=score, direction=direction, details={})

        bee = RivalBeeVanguard.__new__(RivalBeeVanguard)
        bee.board = PheromoneBoard()
        bee.board.publish(entry("ChronosBeeHorizon", chronos_score, chronos_dir))
        for a in self._PEERS:
            bee.board.publish(entry(a, peer_score, peer_dir))
        # 拥挤度（若有人把它接回 Rival）在两条取数路上都换掉
        monkeypatch.setattr(real_data_sources, "get_real_crowding_metrics",
                            lambda *a, **k: {"crowding_score": crowd, "bullish_agents": 4 if crowd else 0})
        monkeypatch.setattr(crowding_detector.CrowdingDetector, "calculate_crowding_score",
                            lambda self, m: (crowd, {}))
        monkeypatch.setattr(bee, "_get_stock_data",
                            lambda t: {"momentum_5d": 2.5, "volatility_20d": 30.0, "price": 100.0})
        monkeypatch.setattr(bee, "_get_history_context", lambda t: "")
        monkeypatch.setattr(bee, "_validate_ticker", lambda t: None)
        monkeypatch.setattr(bee, "_calc_technical_indicators",
                            lambda t: {"tech_score_adj": 0.0, "tech_direction": "neutral", "summary": ""})
        monkeypatch.setattr(bee, "_assess_eps_revision", lambda t: {"revision_signal": "unknown"})
        monkeypatch.setattr(bee, "_publish", lambda *a, **k: None)
        out = bee.analyze("TEST")
        d = out["details"]
        return (d["expected_3d"], d["expected_7d"], d["expected_30d"], out["direction"])

    def test_peer_directions_and_crowding_do_not_move_expected_returns(self, monkeypatch):
        base = self._run(monkeypatch, peer_dir="bullish", peer_score=9.0, crowd=10.0)
        assert base[1] != 0.0, "夹具动量为 0 ⇒ expected 恒 0，差分空转"
        for kw in (dict(peer_dir="bearish", peer_score=1.0, crowd=90.0),
                   dict(peer_dir="neutral", peer_score=5.0, crowd=None),
                   dict(peer_dir="bearish", peer_score=9.0, crowd=10.0, chronos_dir="bearish")):
            assert self._run(monkeypatch, **kw) == base, kw

    def test_positive_control_chronos_score_does_move_them(self, monkeypatch):
        lo = self._run(monkeypatch, peer_dir="bullish", peer_score=9.0, crowd=10.0, chronos_score=7.0)
        hi = self._run(monkeypatch, peer_dir="bullish", peer_score=9.0, crowd=10.0, chronos_score=9.0)
        assert lo[1] != hi[1] and lo[2] != hi[2], (lo, hi)


# ══════════════════════════════════════════════════════════════════════════
# Bear 子分读同伴方向（v0.45.349 起是依赖边；对账已并入 TestPeerReadEdgesMatchCode）
# ══════════════════════════════════════════════════════════════════════════

_BEAR_SUBSCORES = ("bear.insider_bear", "bear.options_bear", "bear.overval_bear", "bear.short_int_bear")


class TestBearSubscoreSlicing:

    def test_upstream_direction_boundary_slices_the_bear_subscore(self, monkeypatch):
        """行为：只直接点名 Oracle / Scout 方向的边界，要把对应的 Bear 子分一并切开；
        不读同伴的 overval / short_int 不许被连坐（成对，防「一刀切」也能让前半绿）。

        变红的变异：删 Bear 两个子分的依赖边、两个名字放回 SIGNAL_LEAVES（v0.45.349 之前的状态）。
        """
        monkeypatch.setattr(sa, "COHORT_SIGNAL_SCOPE", {"vORA": ("agent.OracleBeeEcho.direction",),
                                                        "vSCOUT": ("agent.ScoutBeeNova.direction",)})
        g_ora = sa.generation_boundaries(_BEAR_SUBSCORES, [("2099-01-05", "vORA", "测试")])
        g_scout = sa.generation_boundaries(_BEAR_SUBSCORES, [("2099-01-05", "vSCOUT", "测试")])
        assert "bear.options_bear" in g_ora and "bear.insider_bear" in g_scout, (g_ora, g_scout)
        for g in (g_ora, g_scout):
            assert "bear.overval_bear" not in g and "bear.short_int_bear" not in g, g

    def test_v0_45_349_scope_reaches_both_bear_subscores(self):
        """真表：v0.45.349 的闭包带出 options_bear（直接点名）与 insider_bear（经 Oracle 方向 → 拥挤度 → Scout 方向，
        那天 consensus_strength 的边还在），而 `options.gamma_exposure` 本身不切（算法没动，照算照落盘）。

        变红的变异：把 `COHORT_SIGNAL_SCOPE["v0.45.349"]` 写成只有 `("agent.OracleBeeEcho.score",)`。
        """
        hit = sa._scope_closure(sa.COHORT_SIGNAL_SCOPE["v0.45.349"], _universe(), on="2026-09-28")
        assert {"agent.OracleBeeEcho.score", "agent.OracleBeeEcho.direction",
                "bear.options_bear", "bear.insider_bear"} <= hit, sorted(hit)
        assert "options.gamma_exposure" not in hit and "bear.overval_bear" not in hit, sorted(hit)


# ══════════════════════════════════════════════════════════════════════════
# 刻意池化（v0.45.565）：已知边界改了它的值、用户决定先不切 —— 不许静默，到期要出声
# ══════════════════════════════════════════════════════════════════════════

class TestDeliberatelyPooled:
    """`sentiment.pct` = int(Buzz 合成值)；v0.45.445 换新闻源改了新闻通道 ⇒ 改了它。用户 2026-10-20 定先不切，
    11 月复检（理由、反证、复检条件与做法都在 `signal_archive.DELIBERATELY_POOLED`）。

    这里不按真实钟判「到期」（那是拨钟审计专抓的时间炸弹）：到期提醒走报告与 `ic_rerun_readiness` 的
    attention，按传入的业务日判，下面用固定日期测两侧。
    """

    def test_record_is_well_formed(self):
        history = {v: d for d, v, _r in irr._COHORT_HISTORY}
        for sig, rec in sa.DELIBERATELY_POOLED.items():
            assert sig in sa.SIGNAL_EXTRACTORS, sig
            assert rec["boundary"] in history and rec["boundary"] in sa.COHORT_SIGNAL_SCOPE, rec
            assert datetime.date.fromisoformat(rec["recheck_on_or_after"]) > datetime.date.fromisoformat(
                history[rec["boundary"]]), rec
            for k in ("why", "known_counter_evidence", "recheck_when", "how"):
                assert rec[k].strip(), (sig, k)

    def test_boundary_really_changed_the_signal_input(self):
        """「刻意不切」的前提是边界真的改了它——边界必须直接点名 `changed_via`，且它确实是本信号的输入。"""
        rec = sa.DELIBERATELY_POOLED["sentiment.pct"]
        assert sa._matching({rec["changed_via"]}, sa.COHORT_SIGNAL_SCOPE[rec["boundary"]]), rec
        src = (_REPO / "swarm_agents" / "buzz_bee.py").read_text(encoding="utf-8")
        assert "bullish_pct = int(sentiment_composite)" in src and '"sentiment_pct": bullish_pct' in src, (
            "sentiment.pct 不再是 int(合成值) —— 前提变了，重审这条池化")
        assert "news_signal        * _bw.get(\"news\"" in src, "新闻通道不再进合成值 —— 前提变了，重审这条池化"

    def test_signal_is_actually_not_sliced(self):
        """真切了却忘删记录 ⇒ 报告 / attention 会对一个已经切了的信号喊复检。变红的变异：把 sentiment.pct 加进 445 的范围。"""
        for sig, rec in sa.DELIBERATELY_POOLED.items():
            entry = next(e for e in irr._COHORT_HISTORY if e[1] == rec["boundary"])
            assert sa.generation_boundaries([sig], [entry]) == {}, (sig, "已被切，删掉 DELIBERATELY_POOLED 里这条")

    def test_attention_fires_on_and_after_recheck_day_only(self):
        rec = sa.DELIBERATELY_POOLED["sentiment.pct"]
        due = datetime.date.fromisoformat(rec["recheck_on_or_after"])
        def ids(today):
            return {a["id"] for a in irr.build_attention(
                {}, {}, {"status": "not_ready"}, {"status": "not_ready"}, {}, today=today)}
        key = "ic_rerun.deliberately_pooled.sentiment.pct"
        assert key not in ids((due - datetime.timedelta(days=1)).isoformat())
        assert key in ids(due.isoformat()) and key in ids((due + datetime.timedelta(days=30)).isoformat())
        assert key not in ids(None), "不传业务日 ⇒ 不判（不许偷读真实钟）"

    def test_attention_survives_unimportable_registry(self, monkeypatch):
        """读不到登记表 ⇒ 一条可见的 warn，不抛（`ic_rerun_readiness` 有只带 step_contract 的最小运行环境，
        `tests/test_ic_rerun_fg_budget.py` 的真进程树就是；v0.45.565 初版在那里让整个工具退出码 3）。"""
        monkeypatch.setitem(sys.modules, "signal_archive", None)
        items = irr._pooled_recheck_attention("2099-01-01")
        assert [a["id"] for a in items] == ["ic_rerun.deliberately_pooled.unavailable"], items
        assert items[0]["level"] == "warn", items

    def test_report_names_the_pooled_signal(self, capsys):
        gens = {"signals": {"sentiment.pct": {"gen_start": None, "gen_version": None, "n_total": 10,
                                              "n_in_generation": 10, "n_excluded": 0}},
                "undeclared_versions": []}
        sa._print_generations([], gens)
        out = capsys.readouterr().out
        assert "刻意池化：sentiment.pct" in out and "DELIBERATELY_POOLED" in out, out
