"""冻结版 Buzz 评分 buzz_v1（v0.45.556，维度 IC 协议修订 1 · 阶段 2 第 2 块）。

buzz_v1 的价值全在「冻结」+「自证」：
1. **金值**：9 行真实生产记录（锚点代码在生产里留下的事实：输入 → 分）必须永远复现；外加按锚点公式手算的边界用例——
   背离调整全史从未触发、clamp 边界与「无参照」在自证窗口里一次没出现，这几条路径只能靠手算金值钉住。
2. **冻结**：本模块不许 import 生产评分代码或 config（AST）；只要还没登记「只点名冻结层」的边界，冻结常数 / 函数就必须与
   生产逐位一致——生产一改冻结层，这里在开发期就红，提醒去登记边界（否则只能等运行期自证失配才发现）。
3. **参照**：日期按锚点规则选；值在锚点后由通道 × 冻结权重重算（不读 `sentiment.pct`），锚点前读归档 pct。
4. **自证**：合成库上 全等 ⇒ 过；差 0.01 / 缺输入 / 空 / 读库失败 ⇒ 不过；只点名冻结层的边界截止窗口，点名输入层的不截止。

全部用合成库 / 内存数据，零外部依赖、不需要 skip。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

import buzz_v1 as B
import signal_archive

_ROOT = Path(__file__).resolve().parent.parent


def _protocol():
    spec = importlib.util.spec_from_file_location("dim_ic_protocol", _ROOT / "experiments" / "dim_ic_protocol.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 1. 金值 ─────────────────────────────────────────────────────────────────

#: 真实生产行（2026-09-28 ~ 10-02，生产库 signal_archive + predictions.dimension_scores.sentiment，v0.45.556 摘录）：
#: (日期, 标的, 7 通道, 原始 5 日动量, 3 日参照 pct, 生产分)
REAL_ROWS = [
    ('2026-10-01', 'AMC', {'momentum_signal': 19.387744344573534, 'volume_signal': 50.0, 'volatility_signal': 25.0, 'reddit_signal': 50.0, 'news_signal': 10.0, 'yahoo_signal': 45.0, 'fear_greed_signal': 28.0}, -6.122451131085294, 61, 2.52),   # 动量 -0.5，参照锚点后
    ('2026-09-28', 'CRCL', {'momentum_signal': 3.536348311434798, 'volume_signal': 35.0, 'volatility_signal': 25.0, 'reddit_signal': 50.0, 'news_signal': 66.2, 'yahoo_signal': 45.0, 'fear_greed_signal': 34.0}, -9.29273033771304, 59, 3.52),   # 动量 -0.5，参照锚点前
    ('2026-10-01', 'ABBV', {'momentum_signal': 41.6584631096228, 'volume_signal': 35.0, 'volatility_signal': 75.0, 'reddit_signal': 50.0, 'news_signal': 66.2, 'yahoo_signal': 45.0, 'fear_greed_signal': 28.0}, -1.6683073780754398, 56, 4.77),   # 动量 -0.2，参照锚点后
    ('2026-09-28', 'AMZN', {'momentum_signal': 32.684912583318805, 'volume_signal': 35.0, 'volatility_signal': 60.0, 'reddit_signal': 75.0, 'news_signal': 37.0, 'yahoo_signal': 45.0, 'fear_greed_signal': 34.0}, -3.4630174833362393, 52, 4.47),   # 动量 -0.2，参照锚点前
    ('2026-10-01', 'AMZN', {'momentum_signal': 47.116188464839404, 'volume_signal': 35.0, 'volatility_signal': 60.0, 'reddit_signal': 45.0, 'news_signal': 55.0, 'yahoo_signal': 45.0, 'fear_greed_signal': 28.0}, -0.5767623070321193, 46, 4.6),   # 动量 +0.0，参照锚点后
    ('2026-09-28', 'AMC', {'momentum_signal': 100.0, 'volume_signal': 80.0, 'volatility_signal': 25.0, 'reddit_signal': 65.0, 'news_signal': 37.0, 'yahoo_signal': 75.0, 'fear_greed_signal': 34.0}, 10.774408422902848, 60, 6.19),   # 动量 +0.0，参照锚点前
    ('2026-10-01', 'CRM', {'momentum_signal': 55.704636612681945, 'volume_signal': 35.0, 'volatility_signal': 60.0, 'reddit_signal': 50.0, 'news_signal': 64.0, 'yahoo_signal': 45.0, 'fear_greed_signal': 28.0}, 1.1409273225363892, 44, 5.32),   # 动量 +0.2，参照锚点后
    ('2026-09-28', 'ABBV', {'momentum_signal': 52.01728320706951, 'volume_signal': 50.0, 'volatility_signal': 75.0, 'reddit_signal': 50.0, 'news_signal': 77.5, 'yahoo_signal': 45.0, 'fear_greed_signal': 34.0}, 0.4034566414139018, 50, 5.87),   # 动量 +0.2，参照锚点前
    ('2026-10-02', 'TSLA', {'momentum_signal': 68.38017020859527, 'volume_signal': 50.0, 'volatility_signal': 60.0, 'reddit_signal': 75.0, 'news_signal': 73.0, 'yahoo_signal': 75.0, 'fear_greed_signal': 31.0}, 3.6760340417190562, 37, 7.05),   # 动量 +0.5，参照锚点后
]

_FLAT = {c: 50.0 for c in B.CHANNELS}


class TestGolden:
    @pytest.mark.parametrize("d,t,ch,mom,ref,want", REAL_ROWS, ids=[f"{r[0]}-{r[1]}" for r in REAL_ROWS])
    def test_real_production_rows(self, d, t, ch, mom, ref, want):
        assert B.score(ch, mom, ref)["score"] == want

    def test_composite_of_flat_channels(self):
        assert B.composite(_FLAT) == pytest.approx(50.0)

    @pytest.mark.parametrize("d3,adj", [(None, 0.0), (16, 0.5), (15, 0.2), (6, 0.2), (5, 0.0), (-5, 0.0),
                                        (-6, -0.2), (-15, -0.2), (-16, -0.5)])
    def test_momentum_thresholds(self, d3, adj):
        assert B.momentum_adj(d3) == adj

    @pytest.mark.parametrize("pct,mom,adj", [
        (66, -3.1, -0.3),    # 看多陷阱：int(1/10)=0 + int(3.1/3)=1 ⇒ 1 档
        (80, -7.0, -0.9),    # int(15/10)=1 + int(7/3)=2 ⇒ 3 档（上限 3）
        (99, -30.0, -0.9),   # 上限 3
        (65, -9.0, 0.0),     # 65 不 > 65
        (70, -3.0, 0.0),     # −3.0 不 < −3.0
        (34, 3.1, 0.3),      # 隐藏机会：int(1/10)=0 + int(3.1/3)=1
        (10, 9.5, 0.9),      # int(25/10)=2 + int(9.5/3)=3 ⇒ min(3, 5)=3
        (35, 9.0, 0.0),      # 35 不 < 35
        (20, 3.0, 0.0),
        (50, -20.0, 0.0),    # 情绪不极端
        (90, None, 0.0),     # 动量不可得 ⇒ unavailable、0
    ])
    def test_divergence_formula(self, pct, mom, adj):
        """背离调整全史 2026-03 起从未在生产里触发（v0.45.556 实测）——这条路径只能靠手算金值钉住。"""
        assert B.divergence_adj(pct, mom) == adj

    def test_inner_clamp_happens_before_adjustments(self):
        """判别：合成 / 10 = 0.5 ⇒ 先 clamp 到 1.0 再 +0.5 = 1.5；若没有内层 clamp 会得 1.0。"""
        ch = {c: 5.0 for c in B.CHANNELS}
        assert B.score(ch, None, int(B.composite(ch)) - 16)["score"] == 1.5

    def test_outer_clamp_both_ends(self):
        hi = {c: 100.0 for c in B.CHANNELS}
        assert B.score(hi, None, 0)["score"] == B.SCORE_MAX
        lo = {c: 0.0 for c in B.CHANNELS}
        assert B.score(lo, None, 100)["score"] == B.SCORE_MIN

    def test_no_reference_means_no_momentum_adjustment(self):
        r = B.score(_FLAT, None, None)
        assert r["delta_3d"] is None and r["momentum_adj"] == 0.0 and r["score"] == 5.0


# ── 2. 冻结 ─────────────────────────────────────────────────────────────────

class TestFrozen:
    def test_module_does_not_import_production_scoring_code(self):
        tree = ast.parse((_ROOT / "buzz_v1.py").read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        allowed = {"__future__", "argparse", "datetime", "json", "sqlite3", "sys", "collections", "pathlib",
                   "typing", "importlib", "hive_logger", "signal_archive", "ic_rerun_readiness"}
        assert mods <= allowed, f"冻结评分器不许依赖生产评分代码 / config：{sorted(mods - allowed)}"

    def test_protocol_constants_agree(self):
        P = _protocol()
        assert B.ANCHOR_VERSION == P.H1_ANCHOR_VERSION
        assert B.FROZEN_LAYER_SIGNALS == P.H1_FROZEN_LAYER_SIGNALS
        import ic_rerun_readiness as rr
        assert min(d for d, v, _r in rr._COHORT_HISTORY if v == B.ANCHOR_VERSION) == B.ANCHOR_DATE

    def test_production_frozen_layer_still_matches_until_a_frozen_boundary(self):
        """生产冻结层（合成权重 / clamp / 两个调整的常数与公式 / 参照宽限）与本模块逐位一致，
        **直到**有人登记了「只点名冻结层」的边界。生产先改了却没登记 ⇒ 这里红（开发期），而不是等运行期自证失配。"""
        if B.frozen_only_boundary() is not None:
            return   # 已登记：生产与冻结版按设计分开了，这条对照不再适用（自证窗口同时截止）
        import config
        from swarm_agents import sentiment as S
        from swarm_agents.utils import clamp_score_cfg
        bw = config.AGENT_SCORING["buzz_weights"]
        assert (bw["momentum"], bw["volume"], bw["volatility"], bw["reddit"], bw["news"], bw["yahoo"],
                bw["fear_greed"]) == (B.W_MOMENTUM, B.W_VOLUME, B.W_VOLATILITY, B.W_REDDIT, B.W_NEWS,
                                      B.W_YAHOO, B.W_FEAR_GREED)
        sm = config.SENTIMENT_MOMENTUM_CONFIG
        assert (sm["surge_threshold"], sm["rise_threshold"], sm["crash_threshold"], sm["decline_threshold"]) == \
            (B.SURGE, B.RISE, B.CRASH, B.DECLINE)
        assert S._MOMENTUM_REF_SLACK_DAYS == B.REF_SLACK_DAYS
        for x in (-5.0, 0.5, 1.0, 5.55, 10.0, 12.3):
            assert clamp_score_cfg(x) == B.clamp(x)
        for pct in (0, 20, 34, 35, 36, 50, 64, 65, 66, 80, 100):
            for mom in (None, -30.0, -9.0, -3.01, -3.0, 0.0, 3.0, 3.01, 9.0, 30.0):
                assert S._detect_sentiment_price_divergence(pct, mom, "T")["score_adj"] == B.divergence_adj(pct, mom)

    def test_production_momentum_adjustment_amounts_and_lag_still_match(self, tmp_path):
        """v0.45.562 补：上一条只核了阈值与宽限常数，**调整幅度（±0.5 / ±0.2）与 3 日回看**写死在生产
        `_get_sentiment_momentum` 里、不在 config——直接在沙箱库上跑那个生产函数逐点对照。同样到登记冻结层边界为止。"""
        if B.frozen_only_boundary() is not None:
            return
        from swarm_agents import sentiment as S
        as_of = "2026-11-10"
        for k in range(1, 10):                                          # 参照日 = as_of − k 天
            db = _db(tmp_path, [(B._shift(as_of, -k), "T", "sentiment.pct", 50.0)], name=f"lag{k}.db")
            got = S._get_sentiment_momentum("T", 70, as_of=as_of, db_path=db)
            eligible = B.REF_LAG_DAYS <= k <= B.REF_LAG_DAYS + B.REF_SLACK_DAYS
            assert (got["delta_3d"] is not None) is eligible, (k, got)
        db = _db(tmp_path, [(B._shift(as_of, -B.REF_LAG_DAYS), "T", "sentiment.pct", 50.0)], name="amt.db")
        for delta in range(-30, 31):
            got = S._get_sentiment_momentum("T", 50 + delta, as_of=as_of, db_path=db)
            assert got["delta_3d"] == delta and got["momentum_score_adj"] == B.momentum_adj(delta), (delta, got)


# ── 3. 归档输入与参照 ───────────────────────────────────────────────────────

def _db(tmp_path, rows, preds=(), name="p.db"):
    """rows: [(date, ticker, signal, value)]；preds: [(date, ticker, sentiment)]。真实 schema 经 ensure_schema。"""
    db = tmp_path / name
    signal_archive.ensure_schema(db)
    con = sqlite3.connect(db)
    con.executemany(f"INSERT INTO {signal_archive.TABLE} (date,ticker,signal,value) VALUES (?,?,?,?)", rows)
    con.execute("CREATE TABLE predictions (date TEXT, ticker TEXT, dimension_scores TEXT)")
    con.executemany("INSERT INTO predictions VALUES (?,?,?)",
                    [(d, t, json.dumps({"sentiment": s})) for d, t, s in preds])
    con.commit()
    con.close()
    return db


def _chan_rows(d, t, ch, pct=None, mom=None):
    out = [(d, t, f"buzz.comp.{c}", v) for c, v in ch.items()]
    out.append((d, t, "sentiment.pct", float(int(B.composite(ch)) if pct is None else pct)))
    if mom is not None:
        out.append((d, t, "price.momentum_5d", mom))
    return out


def _with(v):
    return {**_FLAT, "news_signal": v}


class TestReference:
    def _rec(self, tmp_path, rows, key, name="p.db"):
        db = _db(tmp_path, rows, name=name)
        con = sqlite3.connect(db)
        try:
            return B.recompute(con, [key])[key]
        finally:
            con.close()

    def test_post_anchor_reference_is_recomputed_from_channels_not_read_from_pct(self, tmp_path):
        """判别：参照日的归档 pct 故意写成与通道不符的 99；冻结版必须用通道重算（50），不读 99。"""
        rows = _chan_rows("2026-10-05", "T", _FLAT, pct=99) + _chan_rows("2026-10-08", "T", _with(130.0))
        r = self._rec(tmp_path, rows, ("2026-10-08", "T"))
        assert r["ref_date"] == "2026-10-05" and r["delta_3d"] == int(B.composite(_with(130.0))) - 50

    def test_pre_anchor_reference_reads_the_archived_pct(self, tmp_path):
        rows = _chan_rows("2026-09-25", "T", _FLAT, pct=40) + _chan_rows("2026-09-29", "T", _FLAT)
        r = self._rec(tmp_path, rows, ("2026-09-29", "T"))
        assert r["ref_date"] == "2026-09-25" and r["delta_3d"] == 50 - 40

    def test_reference_slack_is_inclusive_at_n_plus_4(self, tmp_path):
        ok = self._rec(tmp_path, _chan_rows("2026-10-08", "T", _FLAT) + _chan_rows("2026-10-15", "T", _FLAT),
                       ("2026-10-15", "T"), "ok.db")
        stale = self._rec(tmp_path, _chan_rows("2026-10-07", "T", _FLAT) + _chan_rows("2026-10-15", "T", _FLAT),
                          ("2026-10-15", "T"), "stale.db")
        assert ok["ref_date"] == "2026-10-08" and stale["ref_date"] is None and stale["momentum_adj"] == 0.0

    def test_latest_qualifying_date_wins_and_recent_days_are_skipped(self, tmp_path):
        rows = (_chan_rows("2026-10-06", "T", _FLAT) + _chan_rows("2026-10-09", "T", _with(10.0))
                + _chan_rows("2026-10-11", "T", _with(90.0)) + _chan_rows("2026-10-12", "T", _FLAT))
        assert self._rec(tmp_path, rows, ("2026-10-12", "T"))["ref_date"] == "2026-10-09"

    def test_missing_channel_is_missing_input(self, tmp_path):
        rows = [r for r in _chan_rows("2026-10-08", "T", _FLAT) if r[2] != "buzz.comp.yahoo_signal"]
        assert "missing" in self._rec(tmp_path, rows, ("2026-10-08", "T"))

    def test_post_anchor_reference_with_incomplete_channels_is_missing_input(self, tmp_path):
        ref = [r for r in _chan_rows("2026-10-05", "T", _FLAT) if r[2] != "buzz.comp.news_signal"]
        rows = ref + _chan_rows("2026-10-08", "T", _FLAT)
        assert "参照日" in self._rec(tmp_path, rows, ("2026-10-08", "T"))["missing"]

    def test_absent_momentum_means_divergence_unavailable(self, tmp_path):
        rows = _chan_rows("2026-10-08", "T", {**_FLAT, "news_signal": 0.0, "reddit_signal": 0.0})
        r = self._rec(tmp_path, rows, ("2026-10-08", "T"))
        assert r["divergence_adj"] == 0.0


# ── 4. 自证 ─────────────────────────────────────────────────────────────────

def _proof_db(tmp_path, off_by=0.0, drop_channel=False):
    rows, preds = [], []
    for d, t, ch, mom, ref, want in REAL_ROWS[:4]:
        rows += [r for r in _chan_rows(d, t, ch, mom=mom) if not (drop_channel and r[2] == "buzz.comp.volume_signal")]
        rows.append(("2026-09-24", t, "sentiment.pct", float(ref)))      # 锚点前参照：只要 pct
        preds.append((d, t, want))
    preds[0] = (preds[0][0], preds[0][1], round(preds[0][2] + off_by, 2))
    return _db(tmp_path, rows, preds)


class TestSelfProof:
    """合成库只放锚点前参照（09-24 的 pct），让 4 行真实输入在库里原样复现生产分。"""

    def test_exact_match_passes(self, tmp_path):
        # 合成库里每只票只有 09-24 一个参照日（值 = 生产当时的参照 pct），四行都落在宽限内 ⇒ 参照值与生产相同
        res = B.self_proof(str(_proof_db(tmp_path)), end="2026-10-02", history=[])
        assert res["n"] == 4 and res["ok"], res

    def test_one_cent_off_fails_and_is_listed(self, tmp_path):
        res = B.self_proof(str(_proof_db(tmp_path, off_by=0.01)), end="2026-10-02", history=[])
        assert not res["ok"] and res["n_mismatched"] == 1
        assert res["mismatched"][0]["ticker"] == REAL_ROWS[0][1]

    def test_missing_input_fails(self, tmp_path):
        res = B.self_proof(str(_proof_db(tmp_path, drop_channel=True)), end="2026-10-02", history=[])
        assert not res["ok"] and res["n_missing"] == len(REAL_ROWS[:4])

    def test_empty_window_is_not_a_pass(self, tmp_path):
        res = B.self_proof(str(_db(tmp_path, [])), end="2026-10-02", history=[])
        assert res["n"] == 0 and not res["ok"]

    def test_unreadable_db_is_not_a_pass(self, tmp_path):
        res = B.self_proof(str(tmp_path / "absent.db"), end="2026-10-02", history=[])
        assert not res["ok"] and "error" in res

    def test_frozen_only_boundary_stops_the_window_input_boundary_does_not(self, monkeypatch):
        monkeypatch.setattr(signal_archive, "COHORT_SIGNAL_SCOPE", {
            **signal_archive.COHORT_SIGNAL_SCOPE,
            "vF": ("agent.BuzzBeeWhisper.score",),                                  # 只改冻结层
            "vI": ("buzz.comp.news_signal", "agent.BuzzBeeWhisper.*")})            # 改输入层（连带分）
        hist = [("2026-10-09", "vI", ""), ("2026-10-20", "vF", ""), ("2026-09-20", "vF", "")]
        assert B.frozen_only_boundary(hist) == {"date": "2026-10-20", "version": "vF"}
        assert B.frozen_only_boundary([("2026-10-09", "vI", "")]) is None

    @staticmethod
    def _scope_vf(monkeypatch):
        monkeypatch.setattr(signal_archive, "COHORT_SIGNAL_SCOPE", {
            **signal_archive.COHORT_SIGNAL_SCOPE, "vF": ("agent.BuzzBeeWhisper.score",)})

    def test_boundary_dated_on_the_anchor_day_is_not_a_stop(self, monkeypatch):
        """锚点那天的边界本来就是锚点那一组（v0.45.423 / 438 也登记在 09-28），不截止自证窗口（`<=` 判别）。"""
        self._scope_vf(monkeypatch)
        assert B.frozen_only_boundary([(B.ANCHOR_DATE, "vF", "")]) is None
        assert B.frozen_only_boundary([(B._shift(B.ANCHOR_DATE, 1), "vF", "")]) is not None

    def test_window_stops_the_day_before_the_boundary(self, monkeypatch, tmp_path):
        """自证窗口止于边界**前一天**：边界当天那行（这里故意放一行缺输入的）不进窗口。"""
        self._scope_vf(monkeypatch)
        db = _proof_db(tmp_path)
        con = sqlite3.connect(db)
        con.execute("INSERT INTO predictions VALUES (?,?,?)", ("2026-10-20", "ZZZ", json.dumps({"sentiment": 5.0})))
        con.commit()
        con.close()
        res = B.self_proof(str(db), end="2026-10-25", history=[("2026-10-20", "vF", "")])
        assert res["ok"] and res["window"] == [B.ANCHOR_DATE, "2026-10-19"], res
        assert res["stopped_by"] == {"date": "2026-10-20", "version": "vF"}
        bad = B.self_proof(str(db), end="2026-10-25", history=[("2026-10-21", "vF", "")])
        assert not bad["ok"] and bad["n_missing"] == 1, "前提：那一行若进了窗口就会红"

    @pytest.mark.parametrize("stop", ["2026-10-05", "2026-10-12"])
    def test_frozen_boundary_before_the_h1_window_is_not_a_pass(self, monkeypatch, tmp_path, stop):
        """v0.45.562：窗口起点之前（含当天）的冻结层边界不截断 H1，H1 读的生产分自窗口起就不是 buzz_v1——
        自证窗口止于它之前，一天 H1 样本都担保不了，不许报通过（此前 4 行全等照样 ok）。"""
        self._scope_vf(monkeypatch)
        assert _protocol().FORWARD_START == "2026-10-12", "前提：参数化日期按窗口起点 10-12 取"
        res = B.self_proof(str(_proof_db(tmp_path)), end="2026-10-25", history=[(stop, "vF", "")])
        assert not res["ok"] and "H1 窗口起点" in res["error"] and res["stopped_by"]["date"] == stop

    @staticmethod
    def _with_bad_row(tmp_path, day):
        """`_proof_db` 再加一行：某天某票有生产分、却一个通道都没归档（自证 ② 缺输入）。"""
        db = _proof_db(tmp_path)
        con = sqlite3.connect(db)
        con.execute("INSERT INTO predictions VALUES (?,?,?)", (day, "ZZZ", json.dumps({"sentiment": 5.0})))
        con.commit()
        con.close()
        return db

    def test_protocol_excluded_day_is_skipped_and_reported(self, tmp_path):
        """v0.45.564（用户 10-20 定）：排除日不进 H1 检验，自证不替它担保——坏行落在排除日上不再让自证永远不过；
        跳过了哪天、几行照样报出来。判别：同一个库不给排除名单就红。"""
        db = self._with_bad_row(tmp_path, "2026-09-30")
        ok = B.self_proof(str(db), end="2026-10-02", history=[], excluded_days=["2026-09-30"])
        assert ok["ok"] and ok["excluded_days"] == ["2026-09-30"] and ok["n_excluded"] == 1, ok
        assert ok["n"] == 4, "排除日那一行不计入比对行数"
        bad = B.self_proof(str(db), end="2026-10-02", history=[], excluded_days=[])
        assert not bad["ok"] and bad["n_missing"] == 1 and bad["excluded_days"] == [] and bad["n_excluded"] == 0

    def test_default_exclusions_come_from_the_protocol_list(self, monkeypatch, tmp_path):
        db = self._with_bad_row(tmp_path, "2026-09-30")
        monkeypatch.setattr(signal_archive, "FORWARD_TEST_EXCLUDED_DAYS", {"2026-09-30": {"version": "vX"}})
        res = B.self_proof(str(db), end="2026-10-02", history=[])
        assert res["ok"] and res["excluded_days"] == ["2026-09-30"], res

    def test_excluded_day_channels_still_serve_as_references(self, tmp_path):
        """只跳过「比对哪些行」，不跳过「读哪些输入」：排除日的归档通道照样当 3 日参照（生产当时就是这么算的）。"""
        ch_ref, ch_now = _FLAT, _with(130.0)
        want = B.score(ch_now, None, int(B.composite(ch_ref)))["score"]
        rows = _chan_rows("2026-10-12", "T", ch_ref) + _chan_rows("2026-10-15", "T", ch_now)
        db = _db(tmp_path, rows, preds=[("2026-10-12", "T", 1.0), ("2026-10-15", "T", want)])
        res = B.self_proof(str(db), end="2026-10-16", history=[], excluded_days=["2026-10-12"])
        assert res["ok"] and res["n"] == 1 and res["n_excluded"] == 1, res
        assert B.momentum_adj(int(B.composite(ch_now)) - int(B.composite(ch_ref))) != 0, "前提：参照确实改变了分数"

    def test_default_history_goes_through_the_signal_archive_hook(self, monkeypatch):
        """边界表与执行器的截断判定同一个入口（`signal_archive._cohort_history`），替换它两边一起变。"""
        self._scope_vf(monkeypatch)
        monkeypatch.setattr(signal_archive, "_cohort_history", lambda: [("2026-10-20", "vF", "")])
        assert B.frozen_only_boundary() == {"date": "2026-10-20", "version": "vF"}
