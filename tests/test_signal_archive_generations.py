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
    "options.iv_current", "options.put_call_ratio", "options.gamma_exposure",
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
        pats += [(f"SIGNAL_UPSTREAM[{k}]", p)
                 for k, ps in sa.SIGNAL_UPSTREAM.items() for p in ps]
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
# Bear 子分读同伴方向 ⇒ 是依赖边，不是叶子（v0.45.349）
# ══════════════════════════════════════════════════════════════════════════

#: BearBee 里产出各归档子分的方法 → 归档信号名（`agent_details.BearBeeContrarian.details.<名>`）。
#: 方法改名 / 消失时 `test_bear_peer_direction_reads_have_edges` 先红，不会静默跳过。
_BEAR_ASSESSORS = {
    "_assess_insider_selling": "bear.insider_bear",
    "_assess_options_puts": "bear.options_bear",
    "_assess_valuation": "bear.overval_bear",
    "_assess_short_interest": "bear.short_int_bear",
}


def _bear_peer_direction_reads():
    """AST：各子分方法里 `x = self._read_peer(ticker, "<蜂>")` 之后读了 `x.direction` 的那些蜂。

    只认 `.direction`：同处读的 details（内幕金额 / P/C / IV Rank / skew / gex）是原始观测、不是边；
    `.self_score` 在这两个方法里只拼进看空理由的文字，不进分值。
    """
    import ast
    src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "swarm_agents", "bear_bee.py")
    with open(src_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BearBeeContrarian")
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    out = {}
    for meth, sig in _BEAR_ASSESSORS.items():
        assert meth in methods, f"BearBeeContrarian.{meth} 不见了 —— 更新 _BEAR_ASSESSORS 的映射"
        peer_of = {}
        for node in ast.walk(methods[meth]):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == "_read_peer" and len(node.value.args) >= 2
                    and isinstance(node.value.args[1], ast.Constant)):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        peer_of[t.id] = node.value.args[1].value
        agents = {peer_of[n.value.id] for n in ast.walk(methods[meth])
                  if isinstance(n, ast.Attribute) and n.attr == "direction"
                  and isinstance(n.value, ast.Name) and n.value.id in peer_of}
        out[sig] = agents
    return out


class TestBearPeerReadsAreEdges:
    """`bear.insider_bear` 读 Scout 方向（bearish ⇒ 下限 6.0）、`bear.options_bear` 读 Oracle 方向
    （bearish ⇒ 下限 5.5）—— 此前却登记在 SIGNAL_LEAVES「不读任何系统输出」⇒ Oracle / Scout 换代时
    这两个子分被静默池化。v0.45.349 改成 SIGNAL_UPSTREAM 的边（补边本身经前后对比不挪任何已有起点）。
    """

    def test_bear_peer_direction_reads_have_edges(self):
        """变红的变异：把两条边删掉、两个名字放回 SIGNAL_LEAVES（v0.45.349 之前的状态）。"""
        reads = _bear_peer_direction_reads()
        assert reads["bear.insider_bear"] == {"ScoutBeeNova"} and reads["bear.options_bear"] == {"OracleBeeEcho"}, (
            f"前提（AST 实测）变了：{reads} —— 核对 bear_bee.py 后更新本断言与 SIGNAL_UPSTREAM")
        problems = []
        for sig, agents in reads.items():
            for a in agents:
                ups = [p for k, ps in sa.SIGNAL_UPSTREAM.items() if fnmatchcase(sig, k) for p in ps]
                if not any(fnmatchcase(f"agent.{a}.direction", p) for p in ups):
                    problems.append(f"{sig} 读 {a} 的方向，SIGNAL_UPSTREAM 却没有这条边")
            if agents and sig in sa.SIGNAL_LEAVES:
                problems.append(f"{sig} 读系统输出，却登记在 SIGNAL_LEAVES")
        assert not problems, problems

    def test_upstream_direction_boundary_slices_the_bear_subscore(self, monkeypatch):
        """行为：只直接点名 Oracle / Scout 方向的边界，要把对应的 Bear 子分一并切开；
        不读同伴的 overval / short_int 不许被连坐（成对，防「一刀切」也能让前半绿）。

        变红的变异：同上一条。
        """
        monkeypatch.setattr(sa, "COHORT_SIGNAL_SCOPE", {"vORA": ("agent.OracleBeeEcho.direction",),
                                                        "vSCOUT": ("agent.ScoutBeeNova.direction",)})
        sigs = list(_BEAR_ASSESSORS.values())
        g_ora = sa.generation_boundaries(sigs, [("2099-01-05", "vORA", "测试")])
        g_scout = sa.generation_boundaries(sigs, [("2099-01-05", "vSCOUT", "测试")])
        assert "bear.options_bear" in g_ora and "bear.insider_bear" in g_scout, (g_ora, g_scout)
        for g in (g_ora, g_scout):
            assert "bear.overval_bear" not in g and "bear.short_int_bear" not in g, g

    def test_v0_45_349_scope_reaches_both_bear_subscores(self):
        """真表：v0.45.349 的闭包带出 options_bear（直接点名）与 insider_bear（经 Oracle 方向 → 拥挤度 → Scout 方向），
        而 `options.gamma_exposure` 本身不切（算法没动，照算照落盘）。

        变红的变异：把 `COHORT_SIGNAL_SCOPE["v0.45.349"]` 写成只有 `("agent.OracleBeeEcho.score",)`。
        """
        universe = set(sa.SIGNAL_EXTRACTORS) | set(sa.UNARCHIVED_NODES)
        hit = sa._scope_closure(sa.COHORT_SIGNAL_SCOPE["v0.45.349"], universe)
        assert {"agent.OracleBeeEcho.score", "agent.OracleBeeEcho.direction",
                "bear.options_bear", "bear.insider_bear"} <= hit, sorted(hit)
        assert "options.gamma_exposure" not in hit and "bear.overval_bear" not in hit, sorted(hit)
