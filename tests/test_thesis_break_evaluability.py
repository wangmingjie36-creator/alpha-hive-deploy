"""论点失效闸必须能区分「核对过、没触发」与「根本没核对」（v0.45.44）

实测（2026-08-26）：`check_thesis_breaks` **从未触发过一次**。
极端输入——price $1 vs $100,000 / IV 200% / P/C 5.0 / score 0 / 6 条看空——
在 NVDA 与 WMT 上一律返回 `level=None`。

根因是 schema 对不上：
  配置存的是人读散文  {id, metric, trigger, data_source, current_status, severity}
  求值器要的是机器可比 {field, op, value}
→ `cond.get("value")` 恒为 None → `_eval_condition` 第一行就 return False。

而 `level=None` 在下游读作「论点完好」。CLAUDE.md 把「任何结论必须附失效条件」
列为硬约束——这条硬约束长期是靠一个永不触发的闸在"满足"。

另有 17/30 只标的没有专属配置，`cfg.get(ticker, cfg.get("NVDA", {}))` 让它们
套用 NVIDIA 的条件（数据中心营收环比 / AMD 竞品 / 中国芯片禁令）。

本次不迁移配置 schema（那是独立决定），只保证**失败可见**。
"""

import json

import pytest

import market_intelligence as mi


def _call(ticker="NVDA", **over):
    kw = dict(current_price=100.0, iv_current=50.0, put_call_ratio=1.0,
              swarm_score=5.0, bear_signals=[])
    kw.update(over)
    return mi.check_thesis_breaks(ticker, **kw)


class TestUnevaluableIsNotClean:
    def test_prose_schema_reports_unevaluable(self):
        r = _call("NVDA")
        assert r["evaluable"] is False, \
            "散文 schema 无法机器求值时必须自报「未核对」"
        assert r["unevaluable_reason"], "必须说明为什么没核对"

    def test_level_none_alone_is_ambiguous(self):
        """level=None 同时代表「没触发」与「没核对」—— 必须靠 evaluable 区分"""
        r = _call("NVDA")
        assert r["level"] is None
        assert "evaluable" in r, "只给 level 会让下游把「没核对」读成「论点完好」"

    def test_fallback_ticker_flagged(self):
        """无专属配置的标的套用 NVDA 条件，必须在原因里点明"""
        cfg = json.load(open("thesis_breaks_config.json"))
        missing = [t for t in ("WMT", "COST", "T") if t not in cfg]
        if not missing:
            pytest.skip("这些标的现在都有专属配置了")
        r = _call(missing[0])
        assert "NVDA 兜底" in r["unevaluable_reason"]

    @pytest.mark.parametrize("extreme", [
        {"current_price": 1.0}, {"current_price": 100000.0},
        {"iv_current": 200.0}, {"swarm_score": 0.0},
        {"bear_signals": ["a"] * 6},
    ])
    def test_extreme_inputs_still_unevaluable_not_clean(self, extreme):
        """极端输入下仍是「未核对」——证明闸确实没在工作，而不是恰好没触发"""
        r = _call("NVDA", **extreme)
        assert r["evaluable"] is False


class TestEvaluableConfigWorks:
    """配置一旦补成机器可比 schema，闸必须真的能触发 —— 锁住迁移后的行为"""

    @pytest.fixture
    def machine_cfg(self, tmp_path, monkeypatch):
        cfg = {"ZZZ": {"level_1_warning": {"name": "预警", "conditions": [
            {"field": "score", "op": "<", "value": 3.0}]}}}
        p = tmp_path / "thesis_breaks_config.json"
        p.write_text(json.dumps(cfg))
        monkeypatch.setattr(mi, "_BASE", tmp_path)
        return p

    def test_fires_when_condition_met(self, machine_cfg):
        r = _call("ZZZ", swarm_score=1.0)
        assert r["level"] == "warning", "机器可比条件满足时必须触发"

    def test_evaluable_true_when_not_fired(self, machine_cfg):
        r = _call("ZZZ", swarm_score=9.0)
        assert r["level"] is None
        assert r["evaluable"] is True, "核对过但没触发 ⇒ evaluable=True"
