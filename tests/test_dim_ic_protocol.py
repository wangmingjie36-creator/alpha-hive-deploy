"""维度 IC 证据协议（P1，v0.45.320）登记常量的守卫。

`experiments/dim_ic_protocol.py` 与 `experiments/dim_ic_preregistration.md` 是**同一份预注册**的
两种写法。预注册的价值全在「看结果之前写死」，所以这里守四件事：

1. 常量没被悄悄改掉（逐值钉住）。
2. 常量与协议文档一致——只改一处就红。一致性检查写成函数，并用篡改过的副本证明它会红
   （「新增不变式必配喂退化数据看它红」）。
3. 常量之间自洽：α 合计、窗口在登记之后、H2 的截断信号与冻结权重对得上。
4. 截断规则依赖的外部事实成立：维度↔蜂的对应与各蜂源码里的 `dimension=` 声明一致；
   截断信号是 `signal_archive` 认识的名字（不认识会被 `generation_boundaries()` 当成
   「受全部边界约束」——截断规则静默退化成「任何边界都截断」，没人会红）。
5. 修订 1（v0.45.330）：H1 分输入层 / 冻结层；输入层的通道集合与归档、生产合成两处对齐；
   「窗口开始前」的修订真的早于窗口；边界声明「点名通道必连带点名分」（协议 §6 按层声明）。
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_DOC = _ROOT / "experiments" / "dim_ic_preregistration.md"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "dim_ic_protocol", _ROOT / "experiments" / "dim_ic_protocol.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


proto = _load_module()


# ── 1. 常量钉住 ──────────────────────────────────────────────────────────────

_BUZZ_CHANNELS = ("buzz.comp.momentum_signal", "buzz.comp.volume_signal", "buzz.comp.volatility_signal",
                  "buzz.comp.reddit_signal", "buzz.comp.news_signal", "buzz.comp.yahoo_signal",
                  "buzz.comp.fear_greed_signal")


class TestRegisteredConstants:
    def test_dates(self):
        assert proto.REGISTERED_ON == "2026-09-23"
        assert proto.FORWARD_START == "2026-10-12"          # 修订 1（原 2026-09-28）

    def test_amendments(self):
        assert proto.AMENDMENTS == (
            ("2026-09-23", "事后·窗口开始前、零数据",
             "修订 1：窗口推迟到 2026-10-12；H1 对象改为冻结版 Buzz 评分 v1（buzz_v1）"),)

    def test_h1_layers(self):
        """修订 1：H1 截断信号分输入层 / 冻结层；锚点在阶段 1 登记前为 None。"""
        assert proto.H1_INPUT_SIGNALS == _BUZZ_CHANNELS + (
            "price.momentum_5d", "price.volume_ratio", "price.volatility_20d", "market.fear_greed")
        assert proto.H1_FROZEN_LAYER_SIGNALS == ("agent.BuzzBeeWhisper.score", "sentiment.pct")
        assert proto.H1_PRODUCTION_DIM == "sentiment"
        assert proto.H1_ANCHOR_VERSION is None

    def test_looks_and_test_shape(self):
        assert proto.LOOKS == ((26, 0.01, "中检"), (52, 0.04, "终检"))
        assert proto.TWO_SIDED is True
        assert proto.MIN_WIDTH == 20
        assert proto.NW_LAG == 1

    def test_outcome(self):
        assert proto.OUTCOME_COLUMN == "close_t7"
        assert set(proto.FORBIDDEN_OUTCOME_COLUMNS) == {"price_t7", "return_t7"}

    def test_hypotheses_order_and_objects(self):
        assert [h[0] for h in proto.HYPOTHESES] == ["H1", "H2"]
        assert proto.HYPOTHESES[0][1] == "buzz_v1"               # 修订 1（原 "sentiment"）
        assert proto.HYPOTHESES[1][1] == "weighted_composite"

    def test_frozen_weights(self):
        assert proto.FROZEN_WEIGHTS == {
            "signal": 0.0, "catalyst": 0.332, "sentiment": 0.325, "odds": 0.343, "risk_adj": 0.0}

    def test_descriptive(self):
        assert proto.DESCRIPTIVE_DIMS == ("signal", "catalyst", "odds", "risk_adj")
        assert proto.DESCRIPTIVE_CORRECTION == "holm"


# ── 2. 常量 ↔ 协议文档 ───────────────────────────────────────────────────────

def _doc_mismatches(p, doc: str) -> list:
    """返回常量在文档里找不到的项。空列表 = 一致。"""
    need = {
        "REGISTERED_ON": p.REGISTERED_ON,
        "FORWARD_START": f"FORWARD_START = {p.FORWARD_START}",
        "MIN_WIDTH": f"≥ **{p.MIN_WIDTH}**",
        "NW_LAG": f"lag = {p.NW_LAG}",
        "OUTCOME_COLUMN": f"`{p.OUTCOME_COLUMN} / price_at_predict − 1`",
    }
    for g, a, label in p.LOOKS:
        need[f"LOOKS[{label}] 周数"] = f"G ≥ {g}"
        need[f"LOOKS[{label}] α"] = f"| {a:g} |"
    for dim, w in p.FROZEN_WEIGHTS.items():
        if w:
            need[f"FROZEN_WEIGHTS[{dim}]"] = f"{dim} {w:g}"
    # H1（修订 1）：对象名 + 每个截断信号的全名都写在 §6 / §13.2（带反引号）；
    # H2 在 §6 按蜂的短名列（「Chronos / Buzz / Oracle 三只蜂」）
    need["H1 对象"] = f"**{p.HYPOTHESES[0][1]}**"
    for s in p.HYPOTHESES[0][2]:
        need[f"H1 截断信号 {s}"] = f"`{s}`"
    for s in p.HYPOTHESES[1][2]:
        need[f"H2 截断信号 {s}"] = s.split(".")[1].split("Bee")[0]
    need["H1 生产维度"] = f"`dimension_scores.{p.H1_PRODUCTION_DIM}`"
    need["H1 锚点"] = ("`H1_ANCHOR_VERSION`" if p.H1_ANCHOR_VERSION is None
                     else f"`H1_ANCHOR_VERSION = {p.H1_ANCHOR_VERSION}`")
    for i, (date, kind, _summary) in enumerate(p.AMENDMENTS, 1):
        need[f"修订 {i}"] = f"### 修订 {i}（{date}"
        need[f"修订 {i} 性质"] = kind
    return [k for k, v in need.items() if v not in doc]


class _Mutated:
    """拿真实常量造一个只改一处的副本。"""

    def __init__(self, **over):
        for k in dir(proto):
            if k.isupper():
                setattr(self, k, getattr(proto, k))
        for k, v in over.items():
            setattr(self, k, v)


class TestConstantsMatchDocument:
    def test_real_constants_match_real_document(self):
        doc = _DOC.read_text(encoding="utf-8")
        assert _doc_mismatches(proto, doc) == []

    @pytest.mark.parametrize("over", [
        {"FORWARD_START": "2026-10-05"},
        {"FORWARD_START": "2026-09-28"},
        {"MIN_WIDTH": 10},
        {"NW_LAG": 2},
        {"LOOKS": ((26, 0.02, "中检"), (52, 0.03, "终检"))},
        {"LOOKS": ((20, 0.01, "中检"), (52, 0.04, "终检"))},
        {"FROZEN_WEIGHTS": {**proto.FROZEN_WEIGHTS, "catalyst": 0.4}},
        {"OUTCOME_COLUMN": "price_t7"},
        {"HYPOTHESES": (("H1", "buzz_v1", ("agent.ScoutBeeNova.score",)), proto.HYPOTHESES[1])},
        {"HYPOTHESES": (("H1", "buzz_v1", proto.HYPOTHESES[0][2] + ("options.iv_rank",)),
                        proto.HYPOTHESES[1])},
        {"HYPOTHESES": (("H1", "sentiment", proto.HYPOTHESES[0][2]), proto.HYPOTHESES[1])},
        {"HYPOTHESES": (proto.HYPOTHESES[0], ("H2", "weighted_composite",
                                              proto.HYPOTHESES[1][2] + ("agent.GuardBeeSentinel.score",)))},
        {"H1_PRODUCTION_DIM": "odds"},
        {"H1_ANCHOR_VERSION": "v0.45.999"},
        {"AMENDMENTS": proto.AMENDMENTS + (("2026-10-01", "事后·窗口开始前、零数据", "修订 2"),)},
    ], ids=["窗口起点", "窗口起点回到原登记", "宽度", "NW滞后", "α拆分", "中检周数", "冻结权重", "结果变量",
            "H1截断信号换蜂", "H1截断信号多一个", "H1对象回到原登记", "H2截断信号", "H1生产维度",
            "H1锚点", "多一条修订"])
    def test_checker_has_teeth(self, over):
        """只改常量、不改文档 ⇒ 检查必须报出不一致。"""
        doc = _DOC.read_text(encoding="utf-8")
        assert _doc_mismatches(_Mutated(**over), doc), f"篡改 {over} 后检查仍判一致——守卫没牙"


# ── 3. 常量自洽 ──────────────────────────────────────────────────────────────

class TestInternalConsistency:
    def test_alpha_spends_exactly_five_percent(self):
        assert sum(a for _g, a, _l in proto.LOOKS) == pytest.approx(0.05)

    def test_looks_strictly_increasing(self):
        gs = [g for g, _a, _l in proto.LOOKS]
        assert gs == sorted(gs) and len(set(gs)) == len(gs)

    def test_window_starts_on_a_monday_after_registration(self):
        """窗口必须晚于登记——否则「看结果之前写死」不成立。"""
        reg = dt.date.fromisoformat(proto.REGISTERED_ON)
        start = dt.date.fromisoformat(proto.FORWARD_START)
        assert start.weekday() == 0
        assert start > reg

    def test_pre_window_amendments_really_precede_the_window(self):
        """自称「窗口开始前」的修订，日期必须早于窗口起点——这是它不算看过数据的全部依据。
        修订只许追加、按日期排序、不早于登记。"""
        dates = [d for d, _k, _s in proto.AMENDMENTS]
        assert dates == sorted(dates) and all(d >= proto.REGISTERED_ON for d in dates)
        for d, kind, _s in proto.AMENDMENTS:
            if "窗口开始前" in kind:
                assert d < proto.FORWARD_START, f"修订 {d} 自称窗口开始前，却不早于 {proto.FORWARD_START}"

    def test_frozen_weights_cover_the_five_dims_and_sum_to_one(self):
        assert set(proto.FROZEN_WEIGHTS) == set(proto.DIM_TO_AGENT)
        assert sum(proto.FROZEN_WEIGHTS.values()) == pytest.approx(1.0, abs=1e-6)

    def test_descriptive_dims_are_the_rest(self):
        assert set(proto.DESCRIPTIVE_DIMS) | {"sentiment"} == set(proto.DIM_TO_AGENT)
        assert "sentiment" not in proto.DESCRIPTIVE_DIMS

    def test_h1_truncation_signals_are_the_two_layers(self):
        """执行器只读 HYPOTHESES；分层常量是它的来源，两处不许各写各的。"""
        assert proto.HYPOTHESES[0][2] == proto.H1_INPUT_SIGNALS + proto.H1_FROZEN_LAYER_SIGNALS
        assert not set(proto.H1_INPUT_SIGNALS) & set(proto.H1_FROZEN_LAYER_SIGNALS)
        assert len(set(proto.HYPOTHESES[0][2])) == len(proto.HYPOTHESES[0][2])

    def test_h1_frozen_layer_is_the_sentiment_bee(self):
        assert proto.H1_PRODUCTION_DIM == "sentiment"
        assert f"agent.{proto.DIM_TO_AGENT[proto.H1_PRODUCTION_DIM]}.score" in proto.H1_FROZEN_LAYER_SIGNALS

    def test_h2_truncation_signals_are_exactly_the_weighted_dims(self):
        """H2 截断信号 = 冻结权重非零的那几维的蜂。多了会误截断，少了会漏截断。"""
        want = {f"agent.{proto.DIM_TO_AGENT[d]}.score"
                for d, w in proto.FROZEN_WEIGHTS.items() if w}
        assert set(proto.HYPOTHESES[1][2]) == want


# ── 4. 外部事实 ──────────────────────────────────────────────────────────────

class TestExternalFacts:
    @pytest.mark.parametrize("dim", ["signal", "catalyst", "sentiment", "odds", "risk_adj"])
    def test_dim_to_agent_matches_bee_source(self, dim):
        agent = proto.DIM_TO_AGENT[dim]
        files = [f for f in (_ROOT / "swarm_agents").glob("*.py")
                 if re.search(rf"^class {agent}\b", f.read_text(encoding="utf-8"), re.M)]
        assert len(files) == 1, f"找不到（或不止一处）定义 {agent} 的文件：{files}"
        src = files[0].read_text(encoding="utf-8")
        assert re.search(rf'dimension\s*=\s*"{dim}"', src), \
            f"{files[0].name} 里没有 dimension=\"{dim}\"——维度↔蜂的对应变了，H1/H2 的截断信号跟着错"

    def test_truncation_signals_are_known_to_signal_archive(self):
        import signal_archive as sa
        known = set(sa.SIGNAL_EXTRACTORS) | set(sa.UNARCHIVED_NODES)
        for _hid, _obj, sigs in proto.HYPOTHESES:
            for s in sigs:
                assert s in known, (f"{s} 不在 signal_archive 的已知信号里 ⇒ generation_boundaries() "
                                    f"会让它受全部边界约束，截断规则静默退化")

    def test_protocol_dims_are_the_config_dims(self):
        import config
        assert set(proto.DIM_TO_AGENT) == set(config.EVALUATION_WEIGHTS)

    def test_h1_channels_are_every_archived_buzz_channel(self):
        """修订 1：H1 输入层的通道必须是全部通道。Buzz 加一个通道而这里没跟 ⇒ 改它不会截断 H1。"""
        import signal_archive as sa
        archived = {s for s in sa.SIGNAL_EXTRACTORS if s.startswith("buzz.comp.")}
        assert {s for s in proto.H1_INPUT_SIGNALS if s.startswith("buzz.comp.")} == archived

    def test_h1_channels_are_the_config_buzz_weights(self):
        """通道集合的第二个来源：生产合成读的 `AGENT_SCORING["buzz_weights"]`。"""
        import config
        want = {f"buzz.comp.{k}_signal" for k in config.AGENT_SCORING["buzz_weights"]}
        assert {s for s in proto.H1_INPUT_SIGNALS if s.startswith("buzz.comp.")} == want


def _buzz_scope_gaps(scope: dict) -> list:
    """协议 §6「按层声明」：点名了任一 Buzz 通道的边界，必须同时点名 Buzz 的分——通道值变了，分一定跟着变。
    否则 H2（读生产 Buzz 分）会漏截断，`analyze()` 也会把 Buzz 分的新旧两代池化。返回违规的 version。"""
    from fnmatch import fnmatchcase
    out = []
    for version, pats in scope.items():
        names_channel = any(fnmatchcase(c, p) for c in _BUZZ_CHANNELS for p in pats)
        names_score = any(fnmatchcase("agent.BuzzBeeWhisper.score", p) for p in pats)
        if names_channel and not names_score:
            out.append(version)
    return out


class TestBuzzLayerDeclarations:
    def test_real_scope_declares_channel_changes_with_the_score(self):
        import signal_archive as sa
        assert _buzz_scope_gaps(sa.COHORT_SIGNAL_SCOPE) == []

    @pytest.mark.parametrize("pats,bad", [
        (("buzz.comp.news_signal",), True),
        (("buzz.comp.*",), True),
        (("buzz.comp.news_signal", "agent.BuzzBeeWhisper.*"), False),
        (("agent.BuzzBeeWhisper.*",), False),          # 只改合成 / 调整层：合法
        (("price.volatility_20d",), False),            # 不点名通道的输入层改动不归这条管
    ], ids=["单通道", "通配通道", "通道连带分", "只改冻结层", "只改行情量"])
    def test_checker_has_teeth(self, pats, bad):
        assert bool(_buzz_scope_gaps({"vX": pats})) is bad
