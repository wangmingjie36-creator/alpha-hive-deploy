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

class TestRegisteredConstants:
    def test_dates(self):
        assert proto.REGISTERED_ON == "2026-09-23"
        assert proto.FORWARD_START == "2026-09-28"

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
        assert proto.HYPOTHESES[0][1] == "sentiment"
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
    # H1 的截断信号全名写在 §6；H2 在 §6 按蜂的短名列（「Chronos / Buzz / Oracle 三只蜂」）
    need["H1 截断信号"] = p.HYPOTHESES[0][2][0]
    for s in p.HYPOTHESES[1][2]:
        need[f"H2 截断信号 {s}"] = s.split(".")[1].split("Bee")[0]
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
        {"MIN_WIDTH": 10},
        {"NW_LAG": 2},
        {"LOOKS": ((26, 0.02, "中检"), (52, 0.03, "终检"))},
        {"LOOKS": ((20, 0.01, "中检"), (52, 0.04, "终检"))},
        {"FROZEN_WEIGHTS": {**proto.FROZEN_WEIGHTS, "catalyst": 0.4}},
        {"OUTCOME_COLUMN": "price_t7"},
        {"HYPOTHESES": (("H1", "sentiment", ("agent.ScoutBeeNova.score",)), proto.HYPOTHESES[1])},
        {"HYPOTHESES": (proto.HYPOTHESES[0], ("H2", "weighted_composite",
                                              proto.HYPOTHESES[1][2] + ("agent.GuardBeeSentinel.score",)))},
    ], ids=["窗口起点", "宽度", "NW滞后", "α拆分", "中检周数", "冻结权重", "结果变量",
            "H1截断信号", "H2截断信号"])
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

    def test_window_starts_monday_of_the_iso_week_after_registration(self):
        """窗口必须晚于登记——否则「看结果之前写死」不成立。"""
        reg = dt.date.fromisoformat(proto.REGISTERED_ON)
        start = dt.date.fromisoformat(proto.FORWARD_START)
        assert start.weekday() == 0
        assert start == reg - dt.timedelta(days=reg.weekday()) + dt.timedelta(weeks=1)

    def test_frozen_weights_cover_the_five_dims_and_sum_to_one(self):
        assert set(proto.FROZEN_WEIGHTS) == set(proto.DIM_TO_AGENT)
        assert sum(proto.FROZEN_WEIGHTS.values()) == pytest.approx(1.0, abs=1e-6)

    def test_descriptive_dims_are_the_rest(self):
        assert set(proto.DESCRIPTIVE_DIMS) | {"sentiment"} == set(proto.DIM_TO_AGENT)
        assert "sentiment" not in proto.DESCRIPTIVE_DIMS

    def test_h1_truncation_signal_is_the_sentiment_bee(self):
        assert proto.HYPOTHESES[0][2] == (f"agent.{proto.DIM_TO_AGENT['sentiment']}.score",)

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
