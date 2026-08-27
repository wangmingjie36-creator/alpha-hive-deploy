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


@pytest.fixture
def prose_only_cfg(tmp_path, monkeypatch):
    """只有人读散文、没有 {field,op,value} 的配置 —— 即 v0.45.50 迁移前的形态。

    v0.45.50 之后真实配置已迁移，所以这条不变式必须用**合成夹具**来测，
    不能再拿真实配置当反例 —— 否则测的是「当前配置长什么样」，
    不是「遇到不可求值的配置该怎么办」。
    """
    _prose = [{"id": "datacenter_revenue_decline", "metric": "DataCenter Revenue Growth",
               "trigger": "季度环比下降 > 5%", "data_source": "季度财报",
               "current_status": "✅ 正常（+8% QoQ）", "severity": "HIGH"}]
    cfg = {
        "ZZZ": {"level_1_warning": {"name": "预警", "conditions": list(_prose)}},
        # NVDA 是 check_thesis_breaks 的兜底键，夹具里必须有，
        # 否则无专属配置的标的会走「配置为空」而不是「套用兜底」分支
        "NVDA": {"level_1_warning": {"name": "预警", "conditions": list(_prose)}},
    }
    (tmp_path / "thesis_breaks_config.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(mi, "_BASE", tmp_path)
    return tmp_path


class TestUnevaluableIsNotClean:
    def test_prose_schema_reports_unevaluable(self, prose_only_cfg):
        r = _call("ZZZ")
        assert r["evaluable"] is False, \
            "散文 schema 无法机器求值时必须自报「未核对」"
        assert r["unevaluable_reason"], "必须说明为什么没核对"

    def test_level_none_alone_is_ambiguous(self, prose_only_cfg):
        """level=None 同时代表「没触发」与「没核对」—— 必须靠 evaluable 区分"""
        r = _call("ZZZ")
        assert r["level"] is None
        assert "evaluable" in r, "只给 level 会让下游把「没核对」读成「论点完好」"

    def test_fallback_ticker_flagged(self, prose_only_cfg):
        """无专属配置的标的套用兜底条件，必须在原因里点明"""
        r = _call("NOSUCHTICKER")
        assert "兜底" in r["unevaluable_reason"]

    @pytest.mark.parametrize("extreme", [
        {"current_price": 1.0}, {"current_price": 100000.0},
        {"iv_current": 200.0}, {"swarm_score": 0.0},
        {"bear_signals": ["a"] * 6},
    ])
    def test_extreme_inputs_still_unevaluable_not_clean(self, prose_only_cfg, extreme):
        """极端输入下仍是「未核对」——证明闸确实没在工作，而不是恰好没触发"""
        r = _call("ZZZ", **extreme)
        assert r["evaluable"] is False


class TestMigratedConfigActuallyFires:
    """v0.45.50 迁移后的真实配置：闸必须真的能工作。

    校准依据：全部 .swarm_results_*.json 共 1191 个标的-日观测的分位。
    初版阈值用单日 30 个样本定，实测回放触发率 44.7%（bear>=5 占 17.3%、
    iv>80 占 15.6%）—— 样本太薄导致阈值太松，已按全历史重定为 6.35%。
    """

    def test_real_config_is_evaluable(self):
        for tk in ("NVDA", "WMT", "AMC"):
            assert _call(tk)["evaluable"] is True, f"{tk} 迁移后应可求值"

    def test_normal_inputs_do_not_fire(self):
        """平时不触发 —— 一道 45% 触发的闸和从不触发一样没用"""
        r = _call("NVDA", current_price=209.82, iv_current=34.0,
                  put_call_ratio=0.80, swarm_score=5.4,
                  bear_signals=["a", "b", "c"])
        assert r["level"] is None and r["evaluable"] is True

    @pytest.mark.parametrize("over,expect", [
        ({"swarm_score": 3.5}, "stop_loss"),
        ({"swarm_score": 3.9}, "warning"),   # 落在 L1(4.00) 与 L2(3.65) 之间
        ({"bear_signals": ["a"] * 7}, "warning"),
        ({"put_call_ratio": 1.95}, "stop_loss"),
        ({"iv_current": 106.0}, "warning"),
    ])
    def test_extremes_fire_at_right_level(self, over, expect):
        base = dict(current_price=209.82, iv_current=34.0, put_call_ratio=0.80,
                    swarm_score=5.4, bear_signals=["a", "b", "c"])
        r = _call("NVDA", **{**base, **over})
        assert r["level"] == expect, f"{over} 应触发 {expect}，实得 {r['level']}"

    def test_no_price_conditions_in_config(self):
        """价格止损刻意不写进配置：绝对价位会随股价漂移衰减，
        有意义的止损应锚定建仓价，而求值器拿不到 entry price。"""
        cfg = json.load(open("thesis_breaks_config.json"))
        for tk, node in cfg.items():
            if tk.startswith("_") or not isinstance(node, dict):
                continue
            for lvl in ("level_1_warning", "level_2_stop_loss"):
                for c in (node.get(lvl) or {}).get("conditions", []) or []:
                    if isinstance(c, dict) and c.get("_machine"):
                        assert c.get("field") != "price", \
                            f"{tk}/{lvl} 出现绝对价格条件 —— 它会随股价漂移衰减"


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



class TestTwoSchemasCoexist:
    """配置同时承载两套 schema，每个求值器必须跳过看不懂的条目。

    v0.45.50 的集成事故：给配置补入 {field,op,value} 机器条目后，
    另一个消费者 `thesis_breaks.py._check_condition` 直接 `condition["id"]`
    → KeyError。两套 schema 并存的前提是**各自跳过对方的条目**。
    """

    def test_prose_evaluator_skips_machine_entries(self):
        from thesis_breaks import ThesisBreakMonitor
        m = ThesisBreakMonitor("NVDA", 5.0)
        machine = {"field": "score", "op": "<", "value": 4.0, "_machine": True}
        assert m._check_condition(machine, {}) is False, "机器条目应被跳过而非崩溃"
        assert m._check_condition({}, {}) is False
        assert m._check_condition({"id": "x"}, {}) is False          # 缺 trigger
        assert m._check_condition(None, {}) is False

    def test_prose_evaluator_still_handles_prose(self):
        """跳过机器条目不能削弱它处理散文条目的能力"""
        from thesis_breaks import ThesisBreakMonitor
        m = ThesisBreakMonitor("NVDA", 5.0)
        prose = {"id": "margin", "trigger": "下降 > 5%"}
        assert m._check_condition(prose, {"margin": 8.0}) is True
        assert m._check_condition(prose, {"margin": 2.0}) is False

    def test_machine_evaluator_skips_prose_entries(self):
        """反向：market_intelligence 侧遇到纯散文配置报「未核对」而非误判"""
        r = _call("NOSUCHTICKER_XYZ")
        assert r["level"] is None
