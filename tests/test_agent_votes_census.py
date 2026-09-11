"""ReportSnapshot.agent_votes 被信息素板截断 + 墙钟过期系统性删票（v0.45.164）

背景（全部为生产 `report_snapshots/*.json` 与 `analysis-*-ml-*.json` 实测，非推断）
--------------------------------------------------------------------------------
`alpha_hive_daily_report._post_scan_notify` 用 `ctx.board.snapshot()` 过滤 ticker
来填 `ReportSnapshot.agent_votes`。`snapshot()` 直接返回 `PheromoneBoard._entries`，
而 `_entries` 有**两道**减法，且这里两道全中：

  ① 溢出淘汰：`nlargest(MAX_ENTRIES=80, key=(self_score, support_count,
     pheromone_strength))` —— **先扔分最低的**。
  ② 墙钟过期：`publish` 里 `_age_s >= 3600` 就地丢弃。而本循环跑在**整轮扫描
     结束之后**（一轮 ~56 分钟），板的存活状态冻结在**最后一次 publish** 那一刻
     ⇒ 早批标的的条目已经躺过了大半个过期窗口。

实测（当前世代 = 30 只 watchlist，2026-08-24 起，n=300 份快照）
---------------------------------------------------------------
8 只蜂齐全的只有 **1 份 = 0.3%**，中位数 **3 只**。逐蜂缺席率：

    BearBeeContrarian 97.3% · BuzzBeeWhisper 91.0% · ChronosBeeHorizon 83.0%
    ScoutBeeNova 81.0% · RivalBeeVanguard 68.3% · GuardBeeSentinel 53.3%
    CodeExecutorAgent 50.0% · OracleBeeEcho 33.7%

**这不是随机缺失，是逐蜂的选择效应**：淘汰键就是 `self_score` 本身，于是每只蜂
**只在自己分数高于自身均值时才被记账**（配对 191 份同日 `agent_details`）：

    蜂                真均值   被记录时均值      Δ
    BearBeeContrarian   3.06      6.25       +3.19
    BuzzBeeWhisper      4.84      6.36       +1.52
    CodeExecutorAgent   5.30      6.52       +1.22
    ScoutBeeNova        5.42      6.53       +1.11
    GuardBeeSentinel    6.35      7.35       +1.00

条目层面：进了 agent_votes 的均分 **7.00**(n=503)，被丢掉的 **4.67**(n=1025)，
差 **+2.34**；逐份「幸存均值 − 真均值」为正 **177/178**。

下游后果（沙箱重建 622 份可配对快照，两侧同一批文件，差异只来自 agent_votes）
-----------------------------------------------------------------------------
· `weekly_optimizer.compute_new_weights_wls`：risk_adj **−4.43pp**、
  sentiment **+4.32pp**，Σ|Δ| **10.77pp**。两者都越过 `MIN_CHANGE_PP=3.0`
  ⇒ 在 v0.44.0 改为只读之前，这个偏差**足以真的写进 config.py**。
  方向尤其刺眼：被压低的正是 sentiment —— 干净口径下唯一有证据的维度。
· `self_analyst` 逐蜂 rank-IC：**每一只在截断下显得有预测力的蜂都失去了它**。
  BearBeeContrarian **+0.239 → −0.077**（n 92 → 431），GuardBee +0.092 → −0.006,
  BuzzBee +0.068 → −0.030。修复后每只蜂的 n 一律 431（截断下 92~431 参差，
  逐蜂 IC 本来就不可横向比较）。
  ⚠️ 上面这组 fixed 数字是**沙箱重建**出来的，不是「真值」：只有 57.5% 的历史
  快照有同日 `analysis-*.json` 可重建，故它证明的是**偏差的量级**，不是修好后的 IC。
· `paper_portfolio._infer_confidence`：dim_std 均值 1.067 → 1.539，
  置信 tier 与旧口径不同的占 **35.9%**（223/622）—— 板此前把蜂群的分歧藏了起来，
  于是什么都看着像 high confidence。**这是本版唯一改变生产行为的下游**
  （tier 降级 ⇒ 仓位减半），方向是保守的。

修法：不读板
------------
`swarm_results[ticker]["agent_details"]` 本来就有每只蜂的分数，**无截断、无过期**，
且就在同一行的 `_data` 里。生产实测 191/191 份恒有 8 只蜂。

它还**更正确**：`agent_details[蜂].score` vs `pheromone_compact.s` 逐份对照
1008 对上 / **16 对不上**，16 条**全部**是 CodeExecutorAgent —— 该蜂在 `analyze`
开头无条件发一条 `5.0/neutral` 占位（`code_executor_agent.py:75`），溢出淘汰按分
留下的恰恰是那个**桩**，把真实结论（3.0）挤掉了。

契约边界
--------
· `MAX_ENTRIES` 的值不动；`get_top_signals` 的排行榜语义不动。
· `QueenDistiller` 不在 `agent_details` 里（它是**聚合**不是蜂），但它今天确实
  会出现在 agent_votes 里（300 份中 82 份 = 27.3%），且 `self_analyst` /
  `paper_portfolio` / `backtest_engine` 三处会读到。故从 `final_score` 补回，
  **只改「在不在」，不改「是什么」**。
  （「把聚合值放进蜂群离散度」本身是另一个可疑设计，本版不碰。）
"""

from datetime import datetime, timedelta

import pytest

from pheromone_board import PheromoneBoard, PheromoneEntry


# 一轮生产发布序列（按 _analyze_single_ticker 的真实顺序）。
# 分数取自 tests/test_resonance_eviction.py 的同一份形状。
ROUND = [
    ("CodeExecutorAgent", "neutral", 5.0),      # code_executor_agent.py:75 无条件占位
    ("ScoutBeeNova", "bullish", 6.26),
    ("OracleBeeEcho", "bullish", 8.48),
    ("BuzzBeeWhisper", "bearish", 4.25),
    ("ChronosBeeHorizon", "bearish", 4.00),
    ("CodeExecutorAgent", "bearish", 2.00),     # 真实结论，占位分更高 ⇒ 淘汰时它先走
    ("RivalBeeVanguard", "bearish", 3.10),
    ("GuardBeeSentinel", "bullish", 7.30),
    ("BearBeeContrarian", "bearish", 3.50),
]

# 同一轮的 swarm_results[ticker] 形状：agent_details 每蜂一条，取**最终**结论
# （CodeExecutor 是 2.0 的真实结论，不是 5.0 的占位）。
AGENT_DETAILS = {
    "ScoutBeeNova":      {"score": 6.26, "direction": "bullish"},
    "OracleBeeEcho":     {"score": 8.48, "direction": "bullish"},
    "BuzzBeeWhisper":    {"score": 4.25, "direction": "bearish"},
    "ChronosBeeHorizon": {"score": 4.00, "direction": "bearish"},
    "CodeExecutorAgent": {"score": 2.00, "direction": "bearish"},
    "RivalBeeVanguard":  {"score": 3.10, "direction": "bearish"},
    "GuardBeeSentinel":  {"score": 7.30, "direction": "bullish"},
    "BearBeeContrarian": {"score": 3.50, "direction": "bearish"},
}

BEES = frozenset(AGENT_DETAILS)

FINAL_SCORE = 6.64


def _swarm_row(details=None, final_score=FINAL_SCORE):
    row = {"final_score": final_score, "direction": "bearish"}
    if details is not None:
        row["agent_details"] = details
    return row


def _entry(agent, ticker="TEST", score=5.0, direction="neutral", ts=None):
    e = PheromoneEntry(
        agent_id=agent, ticker=ticker, discovery="x", source="test",
        self_score=score, direction=direction,
    )
    if ts is not None:
        e.timestamp = ts
    return e


@pytest.fixture
def board():
    return PheromoneBoard()


def _publish_round(board, ticker="TEST", seq=ROUND):
    for agent, direction, score in seq:
        board.publish(_entry(agent, ticker=ticker, score=score, direction=direction))
    # QueenDistiller 在 distill 末尾发布（queen_distiller.py:1230）
    board.publish(_entry("QueenDistiller", ticker=ticker,
                         score=FINAL_SCORE, direction="bearish"))


def _flood(board, score=7.0):
    """把板灌到 MAX_ENTRIES 溢出，复刻生产形状（30 只标的 × 9 条 ≈ 270 条）。

    ⚠️ 每条填充必须用**不同的 ticker**。`publish` 的衰减是 ticker-scoped
    （`if not self._ticker_scoped or e.ticker == entry.ticker`）—— 若填充全用
    同一个 ticker，它们会把彼此衰减到 `MIN_STRENGTH` 以下先行消失，板根本涨不到
    MAX_ENTRIES，溢出淘汰不会触发，这一整组测试会**假绿**。
    v0.45.151 的第一版就栽在这里，故本文件同样配反向自证（见下）。

    默认分 7.0 刻意**居中**：ROUND 里 8.48/7.30 应当存活、其余被挤掉，
    复刻生产形状（中位只剩 3 只蜂）。
    """
    for i in range(PheromoneBoard.MAX_ENTRIES + 5):
        board.publish(_entry("Filler", ticker=f"FLOOD{i}", score=score))


def _votes_via_board(board, ticker="TEST"):
    """被替换掉的旧读法，逐字复刻 alpha_hive_daily_report.py:1169-1173。"""
    return {
        e.get("agent_id", ""): e.get("self_score", 5.0)
        for e in board.snapshot()
        if e.get("ticker") == ticker
    }


# ══════════════════════════════════════════════════════════════════════════
# 夹具反向自证：若 _flood 没真的挤掉条目，下面每一条溢出测试都是假绿
# ══════════════════════════════════════════════════════════════════════════

def test_flood_helper_actually_evicts(board):
    _publish_round(board)
    before = len([e for e in board._entries if e.ticker == "TEST"])
    assert before == len(ROUND) + 1, f"未灌洪时本应 {len(ROUND) + 1} 条全在，实为 {before}"
    _flood(board)
    after = len([e for e in board._entries if e.ticker == "TEST"])
    assert after < before, (
        "_flood 没有触发 MAX_ENTRIES 溢出淘汰 —— 本文件所有溢出测试都失去意义"
    )


def test_old_board_read_loses_bees(board):
    """自证之二：旧读法在本夹具下确实丢蜂，且丢的是低分那些（淘汰键的方向）。

    这条**在修复前后都应为绿**——它钉的是被替换掉的旧行为，
    用来证明夹具复现了生产形状，而不是证明新代码。
    """
    _publish_round(board)
    _flood(board)
    old = _votes_via_board(board)
    assert BEES - set(old), "旧读法一只蜂都没丢 —— 夹具没复现生产的截断形状"
    assert "OracleBeeEcho" in old, "8.48 分的最高分条目不该被挤掉"
    assert "ChronosBeeHorizon" not in old, "4.00 分的最低分条目本应先被挤掉"


def test_old_board_read_keeps_placeholder_over_real_verdict(board):
    """自证之三：CodeExecutor 的 5.0 占位顶掉 2.0 真实结论（生产 16 例的形状）。

    这里刻意用 4.5 而非默认 7.0 的填充分：7.0 会把 CodeExec 的**两条**一起挤掉，
    那测到的是「整只蜂消失」而不是「占位顶掉真实结论」。4.5 正好卡在 2.0 与 5.0
    之间，复刻生产那 16 例的形状。填充分选多少决定测的是哪一种截断。
    """
    _publish_round(board)
    _flood(board, score=4.5)
    old = _votes_via_board(board)
    assert old.get("CodeExecutorAgent") == 5.0, (
        "本夹具本应复现「占位存活、真实结论被挤掉」，实为 "
        f"{old.get('CodeExecutorAgent')!r}"
    )
    assert "BearBeeContrarian" not in old, "3.50 分的低分蜂本应被 4.5 的填充挤掉"


# ══════════════════════════════════════════════════════════════════════════
# 主张：agent_votes 不再受板的截断与过期影响
# ══════════════════════════════════════════════════════════════════════════

def test_records_every_bee_despite_eviction(board):
    """核心不变式：满编一轮的 8 只蜂全部入账。"""
    from alpha_hive_daily_report import build_agent_votes

    _publish_round(board)
    _flood(board)
    votes, _src = build_agent_votes(_swarm_row(AGENT_DETAILS))
    assert BEES <= set(votes), (
        f"缺 {sorted(BEES - set(votes))} —— 生产 300 份里 8 只齐全的只有 1 份"
    )
    for bee, det in AGENT_DETAILS.items():
        assert votes[bee] == pytest.approx(det["score"]), f"{bee} 分值不对"


def test_survives_wall_clock_expiry(board):
    """第二道减法：本循环跑在全扫描之后，早批条目已躺过 3600s 过期窗口。

    只堵溢出淘汰不够 —— 板空了同样交白卷。这里让整轮条目都超龄，
    板上一条不剩，而 agent_votes 必须照常齐全。
    """
    from alpha_hive_daily_report import build_agent_votes

    stale = (datetime.now() - timedelta(seconds=3700)).isoformat()
    for agent, direction, score in ROUND:
        board.publish(_entry(agent, score=score, direction=direction, ts=stale))
    board.publish(_entry("Filler", ticker="OTHER", score=5.0))   # 触发存活过滤
    assert not _votes_via_board(board), "夹具没复现过期清空 —— 板上还有 TEST 条目"

    votes, _src = build_agent_votes(_swarm_row(AGENT_DETAILS))
    assert BEES <= set(votes), f"板过期清空后 agent_votes 也空了: {sorted(votes)}"


def test_prefers_real_verdict_over_placeholder(board):
    """CodeExecutorAgent 记真实结论 2.0，不记 5.0 占位（生产 16/1024 例）。"""
    from alpha_hive_daily_report import build_agent_votes

    _publish_round(board)
    _flood(board)
    votes, _src = build_agent_votes(_swarm_row(AGENT_DETAILS))
    assert votes["CodeExecutorAgent"] == pytest.approx(2.00), (
        "记成了板上留下的 5.0 占位 —— agent_details 才是真实结论"
    )


def test_result_is_independent_of_max_entries(monkeypatch, board):
    """同一轮下，agent_votes 与 MAX_ENTRIES 取值**无关**。

    这条同时挡住「把 MAX_ENTRIES 调大就算修好了」——调大只是让当前 watchlist
    恰好不溢出，标的数再涨一次就复发。
    """
    from alpha_hive_daily_report import build_agent_votes

    def verdict(max_entries):
        monkeypatch.setattr(PheromoneBoard, "MAX_ENTRIES", max_entries)
        b = PheromoneBoard()
        _publish_round(b)
        _flood(b)
        return build_agent_votes(_swarm_row(AGENT_DETAILS))[0]

    assert verdict(80) == verdict(400), "结论随 MAX_ENTRIES 改变 —— 还在读板"


def test_keeps_queen_distiller_from_final_score(board):
    """QueenDistiller 不在 agent_details 里，但它是今天 agent_votes 的合法键
    （生产 82/300 = 27.3%），且被 self_analyst / paper_portfolio /
    backtest_engine 读到。必须从 final_score 补回，不得静默消失。
    """
    from alpha_hive_daily_report import build_agent_votes

    votes, _src = build_agent_votes(_swarm_row(AGENT_DETAILS))
    assert votes.get("QueenDistiller") == pytest.approx(FINAL_SCORE)


def test_no_extra_keys_beyond_bees_and_queen():
    """键域必须恰好是 8 蜂 + QueenDistiller —— 不许把 Filler 之类混进来。"""
    from alpha_hive_daily_report import build_agent_votes

    votes, _src = build_agent_votes(_swarm_row(AGENT_DETAILS))
    assert set(votes) == BEES | {"QueenDistiller"}, f"键域异常: {sorted(votes)}"


# ══════════════════════════════════════════════════════════════════════════
# 「这个失败，下游怎么知道？」—— 缺 agent_details 时必须留下可观测的痕迹
# ══════════════════════════════════════════════════════════════════════════

def test_source_is_recorded_on_success():
    from alpha_hive_daily_report import build_agent_votes

    _votes, src = build_agent_votes(_swarm_row(AGENT_DETAILS))
    assert src == "agent_details"


@pytest.mark.parametrize("row", [
    _swarm_row(None),        # 整个键缺失
    _swarm_row({}),          # 空字典
])
def test_missing_agent_details_is_visible_not_silent(row):
    """agent_details 缺失时不能悄悄交一份「只有 Queen」的票就算完事。

    与 CLAUDE.md「这个失败，下游怎么知道？」同源：容错不得把失败改写成
    「没发生过」。此处的观测点是落盘的 `agent_votes_source`。
    """
    from alpha_hive_daily_report import build_agent_votes

    votes, src = build_agent_votes(row)
    assert src == "unavailable", f"缺 agent_details 却报 source={src!r}"
    assert not (BEES & set(votes)), "没有 agent_details 却凭空产出了蜂票"


@pytest.mark.parametrize("bad_score", [
    None,                       # 上游没给
    "n/a",                      # 字符串
    True,                       # bool 是 int 的子类，`isinstance(x, int)` 会放它过
    float("nan"),               # 算出来是 NaN
    float("inf"),
])
def test_bad_scores_are_dropped_not_coerced(bad_score):
    """坏值不许被 `or 5.0` 之类兜成一个看起来正常的中位分。

    5.0 在 `feedback_loop.agent_vote_correct` 里是**弃权票**（不计入分母），
    而「这只蜂没给分」应该是**根本没有这一票**。两者进的分母不同，
    兜成 5.0 会把缺失伪装成一次有效的弃权。

    ⚠️ 拒绝路径有**三条**（非数字 / bool / NaN-Inf），必须各断一次：
    变异检查实测，只测 None 与 "n/a" 时，把 NaN 分支改成 `score = 5.0`
    是一个**全绿的变异**（M3 存活）—— 那条 `continue` 从没被求值过。
    """
    from alpha_hive_daily_report import build_agent_votes

    bad = {k: dict(v) for k, v in AGENT_DETAILS.items()}
    bad["BuzzBeeWhisper"] = {"score": bad_score}
    votes, _src = build_agent_votes(_swarm_row(bad))
    assert "BuzzBeeWhisper" not in votes, (
        f"score={bad_score!r} 被记成了 {votes.get('BuzzBeeWhisper')!r}"
    )
    assert len(BEES & set(votes)) == len(BEES) - 1


def test_zero_final_score_does_not_fabricate_queen_vote():
    """final_score 为 0 / 缺失时不得凭空造一个 QueenDistiller 票。

    `or` 兜底在这里是错的：0.0 是「算出来就是 0」，不是「没算」。
    但快照循环本身只在 final_score > 0 时才建快照，故此处取「不造票」。
    """
    from alpha_hive_daily_report import build_agent_votes

    votes, _src = build_agent_votes(_swarm_row(AGENT_DETAILS, final_score=0.0))
    assert "QueenDistiller" not in votes


# ══════════════════════════════════════════════════════════════════════════
# 接线：证明生产循环真的改用了它（测 helper ≠ 测接线）
# ══════════════════════════════════════════════════════════════════════════

def test_snapshot_loop_actually_calls_build_agent_votes():
    """`_post_scan_notify` 里赋值 `agent_votes` 的那一段必须走 build_agent_votes，
    且不得再出现 `board.snapshot()`。

    纯 helper 测试挡不住「函数写对了但调用点没换」——v0.45.149 的 mutation M7
    实测过这种全绿。这里锚在源码上，是因为该循环嵌在 `_post_scan_notify` 里，
    其后紧跟 paper_portfolio / Slack 等有副作用的段落，整方法调用不适合入测。
    """
    import inspect

    import alpha_hive_daily_report as m

    src = inspect.getsource(m.AlphaHiveDailyReporter._post_scan_notify)
    assert "_snap.agent_votes" in src, "锚点失效：找不到 agent_votes 赋值"
    head, _, tail = src.partition("_snap.agent_votes")
    region = tail[:400]
    assert "build_agent_votes" in region, (
        "agent_votes 赋值处没调用 build_agent_votes —— 函数换了、调用点没换"
    )
    assert "board.snapshot()" not in region, (
        "agent_votes 赋值处仍在读板 —— 截断与过期都还在"
    )


# ══════════════════════════════════════════════════════════════════════════
# 口径标签必须真的落到盘上 —— 只在内存里对不算数
# ══════════════════════════════════════════════════════════════════════════

def test_source_survives_json_roundtrip(tmp_path):
    """`agent_votes_source` 是本版唯一的观测点，它必须 save→load 往返不丢。

    只断言 `build_agent_votes` 返回值对，挡不住「字段没进 save_to_json」——
    那样标签只活在内存里，而所有下游读的都是盘上的 JSON。
    """
    from feedback_loop import ReportSnapshot

    snap = ReportSnapshot("TEST", "2026-09-08")
    snap.agent_votes = dict(AGENT_DETAILS and {k: v["score"] for k, v in AGENT_DETAILS.items()})
    snap.agent_votes_source = "agent_details"
    fname = snap.save_to_json(str(tmp_path))

    import json
    on_disk = json.load(open(fname, encoding="utf-8"))
    assert on_disk.get("agent_votes_source") == "agent_details", (
        "口径标签没进 JSON —— 落盘的快照与旧口径无法区分"
    )
    assert ReportSnapshot.load_from_json(fname).agent_votes_source == "agent_details"


def test_legacy_snapshot_without_source_key_is_not_backfilled(tmp_path):
    """历史快照没有这个键，回读时**不得**兜成 "agent_details"。

    那批快照恰恰是被板截断过的那一批。把缺键兜成「完整口径」，等于把
    「这一份没验过」渲染成「这一份没问题」—— 与 CLAUDE.md 那条 skip 守卫同源。
    """
    import json

    from feedback_loop import ReportSnapshot

    legacy = tmp_path / "OLD_2026-08-24.json"
    legacy.write_text(json.dumps({
        "ticker": "OLD", "date": "2026-08-24", "composite_score": 6.6,
        "direction": "Long", "entry_price": 10.0,
        "agent_votes": {"OracleBeeEcho": 8.48},      # 板截断后的残余
        "actual_prices": {"t1": None, "t7": None, "t30": None},
    }), encoding="utf-8")

    loaded = ReportSnapshot.load_from_json(str(legacy))
    assert loaded.agent_votes_source == "", (
        f"旧快照被兜成 {loaded.agent_votes_source!r} —— 旧口径伪装成了新口径"
    )


def test_deep_report_producer_labels_its_own_caliber(tmp_path):
    """第二个快照生产者也必须自报口径，不能留空。

    `generate_deep_v2._save_report_snapshot` 本来就不读板（直接取 7 只蜂的 ctx），
    所以它没有截断问题；但它的**键域**与日报不同（无 CodeExecutorAgent、
    无 QueenDistiller）。若它留空，`agent_votes_source == ""` 就同时意味着
    「深度报告口径」和「v0.45.164 之前被板截断的历史快照」—— 而区分这两件事
    正是这个字段存在的理由。
    """
    import json

    from generate_deep_v2 import _save_report_snapshot

    ctx = {"final_score": 7.1, "direction": "bullish", "price": 12.5}
    for key in ("scout", "buzz", "oracle", "chronos", "rival", "guard", "bear"):
        ctx[key] = {"score": 6.0}

    _save_report_snapshot(ctx, "TEST", "2026-09-08", tmp_path)

    out = tmp_path / "report_snapshots" / "TEST_2026-09-08.json"
    assert out.exists(), "深度路径没写出快照"
    src = json.load(open(out, encoding="utf-8")).get("agent_votes_source")
    assert src == "deep_report_ctx", f"深度路径口径标签为 {src!r}"
    assert src not in ("", "agent_details"), "深度口径与另外两种撞在了一起"
