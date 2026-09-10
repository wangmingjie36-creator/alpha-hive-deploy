"""评分重放工具的守卫（v0.45.33）。

重点不在「算得对」，而在**不会被误读**：
- 功效不足时必须明说，且退出码非 0
- 有效样本量必须按不重叠周报，naive n 会高估数倍
- 默认只用最新世代，跨世代必须显式放宽并标注不可比
- 收益口径必须是未截断的 close_t7，用 return_t7 是无效对比
"""

import datetime as dt
import json
import math
import os
import sqlite3
import subprocess
import sys

import pytest


def _reject_json_constant(name):
    """给 `json.loads(parse_constant=...)`：碰到 NaN/Infinity 就抛，模拟严格解析器。"""
    raise ValueError(f"非法 JSON 常量: {name}")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import replay_scoring as rs  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write_predictions_db(path, rows):
    """rows: (date, ticker, dims dict, p0, close_t7, ambiguous) → 返回 path 字符串。

    v0.45.165 从 `db` fixture 里抽出来：需要**两个**夹具库的测试
    （`test_cli_power_verdict_tracks_sample_size` 要比较少样本/多样本两种结论）
    调两次 fixture 会撞同一个 `t.db`，报 "table predictions already exists"。
    """
    p = str(path)
    con = sqlite3.connect(p)
    con.execute(
        "CREATE TABLE predictions (date TEXT, ticker TEXT, final_score REAL,"
        " dimension_scores TEXT, price_at_predict REAL, close_t7 REAL,"
        " dir_ambiguous_t7 INTEGER, return_t7 REAL)")
    con.executemany(
        "INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?)",
        [(d, t, sum(dims.values()) / len(dims), json.dumps(dims), p0, c7, amb, 999.0)
         for d, t, dims, p0, c7, amb in rows])
    con.commit()
    con.close()
    return p


@pytest.fixture
def db(tmp_path):
    return lambda rows: _write_predictions_db(tmp_path / "t.db", rows)


#: v0.45.176：默认从 2 只加宽到 6 只。`evaluate` 改成横截面口径后，单日不足
#: `rs.MIN_WIDTH`(=5) 只标的**算不出横截面 IC**，该日不贡献任何「周」。
#: 旧夹具每天只有 2 只 ⇒ 全部 0 周 ⇒ 功效护栏那两条会红。
#: 这不是实现退化，是**夹具编码了旧语义**：一天只有 1~2 只标的本来就没有
#: 「同一天该挑哪只票」的信息，把它算作一周的功效一直是高估。
#: 生产是每天 30 只，6 只是能过闸的最小生产形状。
def _rows(n_weeks, start="2026-01-05",
          tickers=("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"), amb=0):
    out = []
    d0 = dt.date.fromisoformat(start)
    for w in range(n_weeks):
        d = (d0 + dt.timedelta(weeks=w)).isoformat()
        for i, t in enumerate(tickers):
            dims = {k: 5.0 + i + w * 0.1 for k in rs.DIMS}
            out.append((d, t, dims, 100.0, 100.0 + i + w, amb))
    return out


class TestPowerHonesty:
    """功效护栏 —— 这是本工具最重要的部分。"""

    def test_reports_iso_weeks_not_just_n(self, db):
        r = rs.evaluate("x", lambda row: row["dims"]["signal"],
                        rs.load_samples(db(_rows(4)), all_cohorts=True)["rows"])
        assert r["weeks"] == 4, "有效样本量必须按不重叠 ISO 周报"
        assert r["n"] == 24, "naive n 也要报，但不能只报它"   # 4 周 × 6 只

    def test_underpowered_run_exits_nonzero(self, db, monkeypatch):
        """喂退化：只有 4 周（远低于 25）→ 必须非 0 退出码 + 明确警示。"""
        monkeypatch.setattr(rs, "DB_PATH", db(_rows(4)))
        monkeypatch.setattr(sys, "argv", ["replay_scoring.py", "--all-cohorts"])
        code = rs.main()
        assert code == 1, "功效不足却返回 0 —— 会被脚本当成通过"

    def test_main_actually_uses_patched_db(self, db, monkeypatch):
        """元守卫：证明 monkeypatch 真的改到了 main() 读的库。

        本类其余测试全部依赖 `setattr(rs, "DB_PATH", ...)` 生效。曾经不生效——
        `load_samples(db_path=DB_PATH)` 在 import 时绑死默认值，`main()` 于是
        绕开夹具去读真 `pheromone.db`，退化测试变成假守卫（v0.45.37 修）。
        这条不测业务，只测「夹具接上了没有」，喂一个必然不存在的路径看它红。
        """
        monkeypatch.setattr(rs, "DB_PATH", "/nonexistent/definitely-not-a.db")
        monkeypatch.setattr(sys, "argv", ["replay_scoring.py", "--all-cohorts"])
        assert rs.main() == 3, (
            "main() 没读到被 patch 的 DB_PATH —— 说明它绕开夹具读了真库，"
            "本文件所有喂退化数据的测试都因此失效")

    def test_no_samples_exits_3_not_0(self, db, monkeypatch):
        monkeypatch.setattr(rs, "DB_PATH", db([]))
        monkeypatch.setattr(sys, "argv", ["replay_scoring.py"])
        assert rs.main() == 3

    def test_cli_prints_power_warning(self, db, tmp_path):
        """端到端跑一次真 CLI，输出里必须有功效结论。

        v0.45.165 —— 这条此前**在每一台机器上都恒 skip**，包括生产机，
        而根因和「生产库在不在」毫无关系：

          · CLI 经 `PATHS.db` 解析库位置，`PATHS.db` 读 `ALPHA_HIVE_HOME`
          · conftest 的 autouse `_isolate_env` 把 `ALPHA_HIVE_HOME` 指向 tmp 沙箱
          · **子进程继承了这个环境变量** ⇒ 它读的永远是空沙箱库
          · 于是输出恒为「无可用样本」⇒ `pytest.skip` 恒中 ⇒ 下面两条断言从未求值

        也就是说：**测试隔离泄漏进了子进程**，把一条端到端测试悄悄变成了空转。
        与 v0.45.157 同一个后果（断言从未被求值），但机制不同 ——
        那次是 gitignore 的文件只在一台机器上，这次是环境变量穿透进程边界。

        修法照 v0.45.157：依赖由测试自己构造 —— 用 `ALPHA_HIVE_DB_PATH` 把 CLI
        显式钉到夹具库上（比 `ALPHA_HIVE_HOME` 更直接，绕开沙箱的其他产物），
        skip 随之升级为断言：库既然是本测试造的，造坏了就必须变红。
        """
        env = dict(os.environ)
        env["ALPHA_HIVE_DB_PATH"] = db(_rows(8))     # 8 个不重叠周 < 25 ⇒ 功效不足
        out = subprocess.run(
            [sys.executable, os.path.join(_ROOT, "replay_scoring.py"), "--all-cohorts"],
            capture_output=True, text=True, timeout=180, cwd=_ROOT, env=env)
        combined = out.stdout + out.stderr

        # 正面核对夹具真的接上了 —— 少了这句，`ALPHA_HIVE_DB_PATH` 将来改名
        # 或被内联，CLI 会静默退回读别的库，本条又变回空转（同 v0.45.157 的
        # 「断言要成对」：「没读生产库」必须配「确实读到了夹具库」）。
        assert "无可用样本" not in combined, (
            f"CLI 没读到夹具库 —— ALPHA_HIVE_DB_PATH 这条钩子没打中。输出：\n{combined[:600]}"
        )
        assert ("功效不足" in combined) or ("达到检出" in combined), \
            "输出未给出功效结论，IC 会被当成可直接采信的数字"
        assert "不重叠" in combined

    def test_cli_power_verdict_tracks_sample_size(self, tmp_path):
        """配对的另一半：样本够多时必须给出**另一个**结论，不是同一句话。

        只验「功效不足」会被一个恒输出该字样的实现骗过 —— 那正是本文件
        `test_main_actually_uses_patched_db` 那条元守卫要防的形状，
        只不过这次防的是「结论有没有真的随样本量变化」。
        """
        def verdict(n_weeks):
            env = dict(os.environ)
            env["ALPHA_HIVE_DB_PATH"] = _write_predictions_db(
                tmp_path / f"w{n_weeks}.db", _rows(n_weeks))
            o = subprocess.run(
                [sys.executable, os.path.join(_ROOT, "replay_scoring.py"),
                 "--all-cohorts"],
                capture_output=True, text=True, timeout=180, cwd=_ROOT, env=env)
            return o.stdout + o.stderr

        few, many = verdict(8), verdict(40)
        assert "无可用样本" not in few and "无可用样本" not in many, "夹具没接上"
        assert "功效不足" in few, f"8 个不重叠周应判功效不足：\n{few[:400]}"
        assert "功效不足" not in many, (
            f"40 个不重叠周仍报功效不足 —— 结论没有随样本量变化：\n{many[:400]}")


class TestCrossSectionalCaliber:
    """v0.45.176：`evaluate` 必须算**横截面** IC，不是池化 IC。

    此前 `evaluate` 把所有日期的所有行摊平成一个大 Spearman，ISO 周只用来
    数周数打功效警告。那测的是「哪天分高哪天涨」（时序），不是「同一天该挑
    哪只票」（横截面）—— 而后者才是评分的用途。

    实测代价：同一份 pheromone.db，池化把 risk_adj 算成 **+0.047**，
    横截面是 **−0.060**，符号相反。而 risk_adj 的负 IC 正是 v0.45.172
    归零它的依据之一 ⇒ 用旧口径的本工具复核那个决策会得出相反结论。
    CLAUDE.md 又指定本工具做聚合层决策的第一站，所以这个偏差污染的是
    **将来每一个**聚合层结论，不只是过去某一次。
    """

    @staticmethod
    def _simpson_rows():
        """构造「池化为正、横截面为负」的数据（辛普森悖论的标准形状）。

        每天内部：分越高收益越低（横截面 IC = −1）。
        跨天之间：第二、三天分整体更高、收益也整体更高（池化被这个层级差主导）。
        三天分属三个 ISO 周，故不重叠子采样后是 3 个观测。
        """
        out = []
        for wk, (day, base_score, base_ret) in enumerate([
                ("2026-01-05", 1.0, 1.0), ("2026-01-12", 4.0, 8.0),
                ("2026-01-19", 7.0, 15.0)]):
            for i in range(6):
                score = base_score + i * 0.3
                ret = base_ret + (5 - i) * 0.2          # 日内与 score 反向
                dims = {k: score for k in rs.DIMS}
                out.append((day, f"T{i}", dims, 100.0, 100.0 + ret, 0))
        return out

    def test_reports_cross_sectional_not_pooled(self, db):
        """变红的变异：把 `evaluate` 改回跨日期摊平的池化 Spearman。"""
        rows = rs.load_samples(db(self._simpson_rows()), all_cohorts=True)["rows"]
        r = rs.evaluate("x", lambda row: row["dims"]["signal"], rows)

        assert r["ic"] is not None and r["ic"] < -0.5, (
            f"横截面 IC 应为强负（日内分越高收益越低），实得 {r['ic']}——"
            "多半是又在跨日期池化")
        assert r["ic_pooled"] is not None and r["ic_pooled"] > 0.5, (
            "本夹具的前提是池化口径会给出**正**值；前提不成立的话，"
            "上一条断言就不是在区分两种口径了（同 MEMORY「探针要先自证」）")
        assert r["weeks"] == 3, f"应有 3 个不重叠周，实得 {r['weeks']}"

    def test_sign_conflict_is_surfaced(self, db):
        """两种口径符号相反时必须显式告警，不能悄悄换掉了事。

        变红的变异：把 `sign_conflict` 恒设为 False。
        """
        rows = rs.load_samples(db(self._simpson_rows()), all_cohorts=True)["rows"]
        r = rs.evaluate("x", lambda row: row["dims"]["signal"], rows)
        assert r["sign_conflict"] is True, (
            "横截面 −、池化 + 却没标符号冲突 —— 下一个读输出的人无从知道"
            "自己看的是哪个量")

    def test_degenerate_series_emits_no_nan(self, db):
        """周度 IC 全部相同（stdev=0）时 t 是 NaN —— 不许让它漏进输出。

        `json.dumps(float("nan"))` 吐出裸 `NaN`：Python 自己读得回来，但那不是
        合法 JSON，jq / JS `JSON.parse` / Go 一律拒收。`--json` 是给别的程序读的，
        静默产出解析不了的输出属「失败没传导到下游」。

        变红的变异：把 `evaluate` 里的
        `t_val = t if isinstance(t, float) and math.isfinite(t) else None`
        改回 `t_val = t`。
        """
        rows = []
        for day in ("2026-01-05", "2026-01-12", "2026-01-19"):
            for i in range(6):
                rows.append((day, f"T{i}", {k: float(i) for k in rs.DIMS},
                             100.0, 100.0 + i, 0))
        loaded = rs.load_samples(db(rows), all_cohorts=True)["rows"]
        r = rs.evaluate("退化：每日 IC 恒为 +1", lambda row: row["dims"]["signal"], loaded)

        assert r["ic"] == pytest.approx(1.0), "本夹具的前提是每日 IC 恒为 +1"
        assert r["weeks"] == 3, "前提二：确实取到 3 个不重叠周（否则走的是另一条分支）"
        for k in ("ic", "t", "p", "ic_pooled"):
            v = r[k]
            assert v is None or math.isfinite(v), f"{k} 是非有限值 {v!r}"

        dumped = json.dumps(r)
        assert "NaN" not in dumped and "Infinity" not in dumped, \
            f"--json 会产出非法 JSON：{dumped[:200]}"
        json.loads(dumped, parse_constant=_reject_json_constant)   # 严格解析必须过

    def test_narrow_day_contributes_no_week(self, db):
        """单日标的数 < MIN_WIDTH 的日子没有横截面信息，不许计入功效分母。

        变红的变异：把 `MIN_WIDTH` 降到 1（那会让「一天一只票」也算一周功效，
        正是旧口径高估功效的方式之一）。
        """
        narrow = rs.load_samples(
            db(_rows(4, tickers=("AAA", "BBB"))), all_cohorts=True)["rows"]
        r = rs.evaluate("x", lambda row: row["dims"]["signal"], narrow)
        assert r["n"] == 8, "样本行本身应照常载入（naive n 不受影响）"
        assert r["weeks"] == 0, (
            f"每天只有 2 只标的却报了 {r['weeks']} 周功效——"
            "「同一天该挑哪只票」在 2 只票上算不出来")


class TestCohortDefault:
    def test_defaults_to_latest_cohort(self, db, monkeypatch):
        """默认只取最新世代 —— 混算是静默的，数字照出但没意义。"""
        boundary = "2026-06-01"
        monkeypatch.setattr(rs, "latest_cohort_start", lambda: boundary)
        rows = _rows(3, start="2026-01-05") + _rows(2, start="2026-06-01")
        d = rs.load_samples(db(rows))
        assert all(r["date"] >= boundary for r in d["rows"]), "世代之前的样本被算进来了"
        assert d["cohort_start"] == boundary

    def test_all_cohorts_flags_incomparability(self, db):
        d = rs.load_samples(db(_rows(3)), all_cohorts=True)
        assert any("不可比" in n for n in d["notes"]), \
            "跨世代混算却没标注不可比"


class TestCleanReturnCaliber:
    def test_uses_close_t7_not_return_t7(self, db):
        """return_t7 对方向单是钳位离场收益，直接用即无效对比。
        构造：return_t7 全填 999，若被误用，前向收益就会变成常数。"""
        d = rs.load_samples(db(_rows(3)), all_cohorts=True)
        vals = {round(r["fwd_return_pct"], 4) for r in d["rows"]}
        assert 999.0 not in vals, "用了 return_t7 —— 钳位口径不可比"
        assert len(vals) > 1, "前向收益成了常数，取数有误"

    def test_ambiguous_samples_excluded(self, db):
        rows = _rows(3) + _rows(2, start="2026-03-02", amb=1)
        d = rs.load_samples(db(rows), all_cohorts=True)
        assert len(d["rows"]) == 18          # 3 周 × 6 只；amb 的那 2 周全被剔除
        assert any("模糊样本" in n for n in d["notes"])


class TestNoWeightRecommendation:
    """工具不得给出「最优权重」建议 —— 权重 v0.44.0 起只读。"""

    def test_no_optimizer_shaped_api(self):
        forbidden = {"best_weights", "optimize", "recommend_weights", "fit_weights",
                     "tune", "search_weights"}
        public = {n for n in dir(rs) if not n.startswith("_")}
        assert not (public & forbidden), f"出现调参出口：{public & forbidden}"

    def test_output_states_the_caveat(self, db, monkeypatch, capsys):
        monkeypatch.setattr(rs, "DB_PATH", db(_rows(4)))
        monkeypatch.setattr(sys, "argv", ["replay_scoring.py", "--all-cohorts"])
        rs.main()
        assert "最优权重" in capsys.readouterr().out


class TestDimensionInputsArchived:
    """#5 的验收：维度输入已入档，维度计算层的改动才可重放。"""

    def test_new_input_signals_registered(self):
        from signal_archive import SIGNAL_EXTRACTORS as S
        for sig in ("crowding.comp.social_volume", "catalyst.count",
                    "catalyst.nearest_days", "catalyst.max_weight",
                    "buzz.comp.momentum_signal", "options.iv_rank_is_real"):
            assert sig in S, f"{sig} 未注册 —— 该维度的计算改动仍无法重放"

    def test_crowding_extractor_reads_legacy_key(self):
        """v0.45.30 前后键名不同，读不了旧名等于丢掉改名前的全部历史样本。"""
        from signal_archive import SIGNAL_EXTRACTORS as S
        fn = S["crowding.comp.social_volume"]
        legacy = {"agent_details": {"ScoutBeeNova": {"details": {
            "components": {"stocktwits_volume": 42.0}}}}}
        assert fn(legacy) == 42.0, "旧键名读不到"
        current = {"agent_details": {"ScoutBeeNova": {"details": {
            "components": {"social_volume": 7.0}}}}}
        assert fn(current) == 7.0

    def test_catalyst_count_none_when_source_missing(self):
        """来源不可得（v0.45.31 返回 error、details 缺失）必须是 None，不是 0。
        0 的语义是「查过了，确实没有」。"""
        from signal_archive import SIGNAL_EXTRACTORS as S
        fn = S["catalyst.count"]
        assert fn({"agent_details": {"ChronosBeeHorizon": {}}}) is None
        assert fn({"agent_details": {"ChronosBeeHorizon": {
            "details": {"catalysts": []}}}}) == 0.0

    def test_inputs_attachable_to_samples(self, db):
        d = rs.load_samples(db(_rows(2)), all_cohorts=True, with_inputs=True)
        assert all("inputs" in r for r in d["rows"])


class TestRankCorrelationTies:
    """并列值必须用平均秩 —— 递增秩会凭空造出相关性（v0.45.35 实测 0.0 → +0.2967）。

    catalyst 只有约 6 个不同取值（30 只标的），是最容易被放大的维度。
    """

    def test_ties_give_zero_when_unrelated(self):
        xs = [1, 1, 1, 2, 2, 2, 3, 3, 3] * 3
        ys = [1, 2, 3, 1, 2, 3, 1, 2, 3] * 3
        ic = rs.rank_ic(xs, ys)
        assert abs(ic) < 1e-9, (
            f"并列值处理错误：无关数据得到 IC={ic:+.4f}，应为 0。"
            "多半是又自己写了一份 _rank 而非用 ic_diagnostics.spearman")

    def test_matches_project_spearman(self):
        """必须与项目现有实现同源，不得再复制第二份。"""
        from ic_diagnostics import spearman
        xs = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0, 5.0, 3.0, 5.0]
        ys = [2.0, 7.0, 1.0, 8.0, 2.0, 8.0, 1.0, 8.0, 2.0, 8.0, 4.0]
        assert rs.rank_ic(xs, ys) == spearman(xs, ys)

    def test_no_local_rank_helper(self):
        """出现自建 _rank 即视为回归 —— 那正是 bug 的来源。"""
        import inspect
        src = inspect.getsource(rs)
        assert "def _rank(" not in src, "又自己写了一份秩函数"


class TestCatalystWeightTablesSameSource:
    """归档的催化剂权重必须与 ChronosBee 同源（v0.45.35 修）。

    初版手抄一份，漏了 6 个类型且默认值 0.8 vs 蜂内 0.7。
    实测最常见的 Dividend/Ex-Dividend（蜂内 0.4/0.3）被按 0.8 算，高估一倍。
    """

    def test_tables_identical_to_bee(self):
        from swarm_agents.chronos_bee import ChronosBeeHorizon as C
        import signal_archive as sa
        tw, sm, td = sa._cat_tables()
        assert tw == C.CATALYST_TYPE_WEIGHTS, "type 权重表已漂移"
        assert sm == C.CATALYST_SEVERITY_MULT, "severity 表已漂移"
        assert td == C._CATALYST_TYPE_DEFAULT, "未知类型默认值已漂移"

    def test_dividend_weight_not_default(self):
        """喂退化：股息类必须拿到自己的低权重，不能落默认值。"""
        import signal_archive as sa
        fn = sa.SIGNAL_EXTRACTORS["catalyst.max_weight"]
        tr = {"agent_details": {"ChronosBeeHorizon": {"details": {"catalysts": [
            {"type": "exDividendDate", "severity": "medium"}]}}}}
        w = fn(tr)
        assert w is not None and w < 0.6, (
            f"exDividendDate 得到 {w}，说明落了默认值 —— 权重表又被复制了")
