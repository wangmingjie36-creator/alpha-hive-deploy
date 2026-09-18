"""BearBee 把「排行榜」当「按身份取」用：get_top_signals(n=20) 被 MAX_ENTRIES 淘汰吃掉（v0.45.288）

这是同一个缺陷的第 5、6 处（v0.45.151 RivalBee → v0.45.156 共振 → v0.45.163 Guard
→ v0.45.279 Scout 拥挤度；后者的 `_COHORT_HISTORY` 条目与本仓 memory 均点名记过
「`bear_bee.py:39/512` 两处未迁」）。`BeeAgent._read_peer` 早在 v0.45.151 就是为
「读同一轮里另一只蜂的条目」修好的定点索引，`BearBeeContrarian` 就继承自 `BeeAgent`，
却一直用着自己的 `_read_board_entry`（`get_top_signals(ticker, n=20)` + 前缀匹配）。

生产实测（`.swarm_results_*.json`，2026-08-24 起、当日标的数 ≥25 的 18 天共 540 行）
--------------------------------------------------------------------------------
Bear 自记录了口径：`details.data_sources` 里 `ml`/`guard`/`catalyst` 三键**读到才写**
（缺席 = 没读到），`insider`/`options`/`news` 读到写 `"real"`、否则变成回落来源
（`sec_api`/`options_api`/`newsapi`）。所以——**与 v0.45.279（Scout）不同，这一处的
生产 miss 率是可以直接量的**。

  · 是容量效应，不是上游没发布。全期（03-10 起 1634 行）9~16 只标的的日子 miss
    0.7%~3.1%，满名单（≥25 只）日子 options 19.2% / ml 23.8% / guard 30.1% /
    catalyst 45.2%；限定到 08-24 起这 540 行则是 21.7% / 18.0% / 32.6% / 38.0%
    （下面所有数字与 `experiments/bear_read_miss_audit.py` 的默认口径）。
  · 偏向方向**因蜂而异**（低分先被挤掉，而各蜂「低分」的含义不同）：
      Oracle  读丢组「若读到会触发」53.8% vs 读到组 43.3%；方向 bearish 的 Oracle 有
              65.4%（34/52）必然没被读到；且回落路径不看 iv_skew / gex / Oracle 方向，
              只能**少算**——读丢行读板值更高 63 例、更低 0 例，均差 +5.67 分。
      Guard   30.7% vs 15.9%。
      Catalyst 反向（2.4% vs 22.7%）：读丢的恰是「无催化剂」（恒落 4.0 分），本就贡献 0，无害。
      ML      无偏（10.3% vs 11.1%）。 Scout / Buzz  下界 0% / 4.7%，无实质证据。
  · 汇总：22.4% 的行 `bear_score` 被低估，均值 +0.27、最大 +3.50；现行阈值下方向翻转
    14/540（2.6%），**全部翻向看空**。Bear 不计票、不进 `final_score`，波及的是
    `bear.*` 归档信号、报告看空版块与 dashboard。
  · 重放方法：`_compute_bear_score`（真方法）+ 现行阈值，baseline 与记录的
    `rule_bear_score` 吻合 99.4%（537/540）；Oracle 读板路径复刻在读到的行上 423/423
    吻合。（第一版重放只有 87.8%，原因是**重放自己**把催化剂维度的键写成 "catalyst"
    而 `dim_scores` 里是 "chronos"——先校验重放再信数字。）

副作用：读丢时 Bear 会重新去拉 OptionsAgent / SEC / newsapi（Oracle 回落占满名单日
21.7% 的行），正是它「读板以避免重复调用」的设计要躲的限流压力。

修复后复量：同一套口径（按当日标的数分层的 data_sources miss 率）应在满名单日降到
≈0；`experiments/bear_read_miss_audit.py` 是那把尺子。
"""

import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import swarm_agents.bear_bee as bear_mod
from pheromone_board import PheromoneBoard, PheromoneEntry
from swarm_agents import BearBeeContrarian

TICKER = "TEST"


def _entry(agent, ticker=TICKER, score=5.0, direction="neutral", details=None):
    return PheromoneEntry(
        agent_id=agent, ticker=ticker, discovery="测试发现", source="test",
        self_score=score, direction=direction, details=details or {},
    )


@pytest.fixture
def board():
    b = PheromoneBoard()
    yield b
    b.clear()


@pytest.fixture
def bear(board):
    return BearBeeContrarian(board)


@pytest.fixture
def fallbacks(monkeypatch):
    """三条会去网络的回落全部换成记录桩：既保证离线，又能数「回落被触发了几次」。

    ⚠️ 桩本身要被证明接得上——`TestFallbackStillWorksWhenNothingPublished` 里
    「没人发布 ⇒ 每个计数器都 +1」就是这个反向自证；否则下面所有
    「回落计数 == 0」的断言都可能只是桩没接上。
    """
    import newsapi_client
    import options_analyzer
    import sec_edgar

    calls = {"sec": 0, "options": 0, "news": 0}

    def _sec(ticker, days=90):
        calls["sec"] += 1
        return None

    class _Opt:
        def analyze(self, ticker, stock_price=None):
            calls["options"] += 1
            return {"put_call_ratio": 0.9, "iv_rank": 40.0}

    def _news(ticker, max_articles=8):
        calls["news"] += 1
        return {"is_real_data": False}

    monkeypatch.setattr(sec_edgar, "get_insider_trades", _sec)
    monkeypatch.setattr(options_analyzer, "OptionsAgent", _Opt)
    monkeypatch.setattr(newsapi_client, "get_ticker_news", _news)
    return calls


def _flood(board, score=9.0):
    """灌到 MAX_ENTRIES 溢出，复刻生产形状（30 只标的 × 7 蜂 ≈ 210 条）。

    ⚠️ 每条填充必须用**不同的 ticker**：publish 的衰减是 ticker-scoped，同一个
    ticker 的填充会互相衰减到消失、板涨不到 80 条，溢出淘汰不触发，测试会假绿
    （test_rival_bee_catalyst_missing.py 第一版栽过）。
    分数 9.0 高于本文件所有同伴条目 ⇒ 淘汰一定先挑同伴。
    """
    for i in range(PheromoneBoard.MAX_ENTRIES + 5):
        board.publish(_entry("Filler", ticker=f"FLOOD{i}", score=score))


def _assess(bear, which, ticker=TICKER):
    """走真实的六个 `_assess_*`，返回 (分量值, data_sources)。"""
    sigs, ds = [], {}
    if which == "insider":
        val = bear._assess_insider_selling(ticker, sigs, ds)[0]
    elif which == "options":
        val = bear._assess_options_puts(ticker, 100.0, sigs, ds)[0]
    elif which == "news":
        val = bear._assess_news_sentiment(ticker, sigs, ds)[0]
    elif which == "catalyst":
        val = bear._assess_catalyst_risk(ticker, sigs, ds)
    elif which == "ml":
        val = bear._assess_ml_prediction(ticker, sigs, ds)
    elif which == "guard":
        val = bear._assess_signal_consistency(ticker, sigs, ds)
    else:
        raise AssertionError(which)
    return val, ds


# 每行 = 一个读点。分数都刻意取低分（生产里被先挤掉的那一档），值取「该读点会触发」的形状。
#   which, agent_id, direction, score, details, source_key, expected, fallback_counter
CASES = [
    ("insider", "ScoutBeeNova", "bearish", 3.6,
     {"insider_sold_usd": 2_000_000, "insider_bought_usd": 100_000}, "insider", 8.0, "sec"),
    # 方向 bearish + 数值都温和：读板路径靠「Oracle 方向」规则给 5.5，回落路径给 0 —— 生产里 +5.67 的那种
    ("options", "OracleBeeEcho", "bearish", 3.8,
     {"pc_ratio": 0.9, "iv_rank": 40.0, "iv_skew": 1.0, "gex": 100.0}, "options", 5.5, "options"),
    ("news", "BuzzBeeWhisper", "bearish", 3.9,
     {"sentiment_score": 25}, "news", 7.5, "news"),
    ("catalyst", "ChronosBeeHorizon", "neutral", 4.5,
     {"nearest_days": 3}, "catalyst", 5.0, None),
    ("ml", "RivalBeeVanguard", "bearish", 3.0,
     {"ml_probability": 0.35, "expected_7d": -4.0}, "ml", 7.0, None),
    ("guard", "GuardBeeSentinel", "bearish", 3.5,
     {"consistency": 0.3, "conflict_type": ""}, "guard", 5.5, None),
]
_IDS = [c[0] for c in CASES]


def _publish_case(board, case):
    _which, agent, direction, score, details, *_ = case
    board.publish(_entry(agent, score=score, direction=direction, details=details))


# ══════════════════════════════════════════════════════════════════════════
# 夹具反向自证：洪水没真把同伴挤掉的话，下面的溢出测试全部假绿
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_flood_evicts_the_peer_from_the_leaderboard_but_not_the_census(board, case):
    _publish_case(board, case)
    agent = case[1]
    assert any(e.agent_id == agent for e in board.get_top_signals(TICKER, n=20)), \
        "洪水前排行榜里应该有它（否则发布没成功）"
    _flood(board)
    assert not any(e.agent_id == agent for e in board.get_top_signals(TICKER, n=20)), (
        "_flood 未把低分同伴挤出排行榜 —— 本文件溢出测试全部失去意义")
    assert any(e.agent_id == agent for e in board.get_live_signals(TICKER)), \
        "普查视图不该丢它——这是修复方向成立的前提"


# ══════════════════════════════════════════════════════════════════════════
# 站点 1：六个读点在溢出后仍读得到（修复前红）
# ══════════════════════════════════════════════════════════════════════════

class TestSixReadSitesSurviveEviction:

    @pytest.mark.parametrize("case", CASES, ids=_IDS)
    def test_evicted_peer_is_still_read(self, bear, board, fallbacks, case):
        which, _agent, _d, _s, _det, source_key, expected, fb = case
        _publish_case(board, case)
        _flood(board)

        val, ds = _assess(bear, which)

        assert val == pytest.approx(expected), (
            f"{which}：同伴条目被 MAX_ENTRIES 挤出排行榜后应仍能读到（期望 {expected}，"
            f"实得 {val}）—— 读丢会让该分量静默变 0 或掉到回落路径的值")
        assert ds.get(source_key) == "real", (
            f"{which}：data_sources[{source_key!r}] 应为 'real'，实得 {ds.get(source_key)!r}")
        if fb:
            assert fallbacks[fb] == 0, (
                f"{which}：读到了就不该再去拉 {fb} 回落（那是它「读板避免重复调用」要躲的限流压力）")

    @pytest.mark.parametrize("case", CASES, ids=_IDS)
    def test_control_without_flood_gives_the_same_value(self, bear, board, fallbacks, case):
        """对照：没有洪水时值就是 expected —— 证明上面的期望值本身是读板路径的真值，
        红/绿之差只来自淘汰，不来自期望值写错。修复前后都应绿。"""
        which, _agent, _d, _s, _det, source_key, expected, fb = case
        _publish_case(board, case)

        val, ds = _assess(bear, which)

        assert val == pytest.approx(expected)
        assert ds.get(source_key) == "real"
        if fb:
            assert fallbacks[fb] == 0

    def test_flooded_full_round_equals_unflooded_full_round(self, fallbacks):
        """结构性不变式：同一轮发布，板灌没灌满，六个分量与口径标记必须完全一样。"""
        def run(flood):
            b = PheromoneBoard()
            try:
                for c in CASES:
                    _publish_case(b, c)
                if flood:
                    _flood(b)
                bee = BearBeeContrarian(b)
                out = {}
                for c in CASES:
                    val, ds = _assess(bee, c[0])
                    out[c[0]] = (round(val, 6), ds.get(c[5]))
                return out
            finally:
                b.clear()

        assert run(flood=True) == run(flood=False)
        assert fallbacks == {"sec": 0, "options": 0, "news": 0}


class TestFallbackStillWorksWhenNothingPublished:
    """反向自证 + 保留合法回落：同伴**从没发布过**时，回落照旧触发（桩确实接得上）。"""

    def test_empty_board_hits_every_network_fallback_once(self, bear, fallbacks):
        _assess(bear, "insider")
        _assess(bear, "options")
        _assess(bear, "news")
        assert fallbacks == {"sec": 1, "options": 1, "news": 1}

    def test_no_fallback_components_are_zero_and_unmarked_when_unpublished(self, bear, fallbacks):
        for which, key in (("catalyst", "catalyst"), ("ml", "ml"), ("guard", "guard")):
            val, ds = _assess(bear, which)
            assert val == 0.0
            assert key not in ds


class TestStaleEntryIsStillRejected:
    """普查视图保留墙钟过期（与 get_agent_entry / get_live_signals 同口径 3600s）：
    「被容量挤掉」不该拦，「上一轮的陈货」该拦。成对断言，防把两件事一起放开。"""

    def test_two_hour_old_entry_is_not_read(self, bear, board, fallbacks):
        board.publish(_entry("OracleBeeEcho", score=3.8, direction="bearish",
                             details={"pc_ratio": 0.9, "iv_rank": 40.0}))
        stale = (datetime.now() - timedelta(hours=2)).isoformat()
        board._latest_by_agent[(TICKER, "OracleBeeEcho")].timestamp = stale

        val, ds = _assess(bear, "options")

        assert ds.get("options") != "real"
        assert fallbacks["options"] == 1, "陈货不该被当成本轮数据读到，应走回落"
        assert val == 0.0


# ══════════════════════════════════════════════════════════════════════════
# 站点 2：LLM 论点里的「看多信号」列表（只在 LLM 模式生效，但同一个缺陷）
# ══════════════════════════════════════════════════════════════════════════

class TestLlmBullSignalsUseTheCensus:

    @pytest.fixture
    def captured(self, monkeypatch):
        """桩掉 LLM：is_available→True 让代码走进该分支，generate_bear_thesis 只记参数、
        返回 None ⇒ 全程零 API 费用（项目铁律：LLM 调用须用户批准）。"""
        import llm_service
        got = {}
        monkeypatch.setattr(llm_service, "is_available", lambda: True)

        def _gen(**kw):
            got.update(kw)
            return None

        monkeypatch.setattr(llm_service, "generate_bear_thesis", _gen)
        return got

    def _round(self, board):
        board.publish(_entry("OracleBeeEcho", score=4.0, direction="bullish"))
        board.publish(_entry("ScoutBeeNova", score=4.2, direction="bullish"))
        board.publish(_entry("BuzzBeeWhisper", score=4.1, direction="bearish"))
        board.publish(_entry("CodeExecutorAgent", score=5.0, direction="neutral"))
        board.publish(_entry("BearBeeContrarian", score=6.0, direction="bullish"))

    def test_bullish_signals_survive_eviction(self, bear, board, captured):
        self._round(board)
        _flood(board)

        bear._generate_llm_bear_thesis(TICKER, 5.0, [], None, None, None, {})

        agents = {s["agent"] for s in captured["bull_signals"]}
        assert agents == {"OracleBeeEcho", "ScoutBeeNova"}, (
            f"洪水后 LLM 该看到全部看多蜂（且不含 Bear 自己/非看多），实得 {agents}")

    def test_control_without_flood_same_answer(self, bear, board, captured):
        """对照：不灌洪水时同样是这两只 —— 修复前后都应绿。"""
        self._round(board)

        bear._generate_llm_bear_thesis(TICKER, 5.0, [], None, None, None, {})

        assert {s["agent"] for s in captured["bull_signals"]} == {"OracleBeeEcho", "ScoutBeeNova"}


# ══════════════════════════════════════════════════════════════════════════
# 守卫：bear_bee.py 里不许再有「排行榜当普查/定点查」
# ══════════════════════════════════════════════════════════════════════════

def test_bear_bee_never_reads_the_board_as_a_leaderboard():
    """这个缺陷已经在 5 个消费方各修了一次、每次都是**同一个**形状。Bear 要的是
    「某只蜂的条目」（`_read_peer`）或「每只蜂最新一条」（`get_live_signals`），
    没有一处是真需要「按强度排名前 N」的。真要加排行榜读法，先读
    `PheromoneBoard.get_agent_entry` / `get_live_signals` 的 docstring 再来改这条。"""
    tree = ast.parse(Path(bear_mod.__file__).read_text(encoding="utf-8"))
    hits = [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get_top_signals"]
    assert hits == [], f"bear_bee.py 仍在用 get_top_signals（行 {hits}）"
