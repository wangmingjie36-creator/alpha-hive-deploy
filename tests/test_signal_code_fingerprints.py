"""输入层代码指纹守卫（v0.45.554，buzz_v1 阶段 2 第 1 块）。

维度 IC 协议修订 1 的 H1 只在**登记过**的输入层边界上截断。v0.45.330 守住了「点名通道必连带点名分」，
本文件守反方向：**改了决定 H1 输入的代码，就必须有一条确认**（边界 / 测量误差修正 / 产出值不变）。

逐条守，且每条配一个「违反它就会红」的用例（全绿本身证明不了什么）：

1. 当前代码与登记一致——这是守卫本体；失配时报错信息直接告诉人怎么登记。
2. 指纹灵敏：通道公式里改一个数 ⇒ 变；只改注释 / docstring ⇒ 不变；**改冻结层（合成权重）⇒ 不变**
   （否则每次合法的冻结层改动都会被误要求输入层确认）；同文件里不相干的函数 ⇒ 不变。
3. 结构变了（定位不到）⇒ 抛 / 报 error，不当作「没变」。
4. 登记表自洽：baseline 规则、说明长度、边界确认必须真在边界表里、声明过影响面、且触及该单元的信号。
5. 覆盖：各单元信号的并集恰好等于协议的 `H1_INPUT_SIGNALS`。

全部用本仓源码 + 内存里改过的副本，零外部依赖、不需要 skip。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import signal_code_fingerprints as F

_ROOT = Path(__file__).resolve().parent.parent


def _protocol():
    spec = importlib.util.spec_from_file_location("dim_ic_protocol", _ROOT / "experiments" / "dim_ic_protocol.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _mutated(rel: str, old: str, new: str):
    """只改 `rel` 里唯一一处 `old` 的读函数。锚点不唯一 / 不存在 ⇒ 直接红（代码漂了，测试要跟着改）。"""
    src = _read(rel)
    assert src.count(old) == 1, f"变异锚点在 {rel} 里出现 {src.count(old)} 次（应为 1）：{old!r}——代码改了，请更新本测试的锚点"
    patched = src.replace(old, new)
    return lambda r: patched if r == rel else _read(r)


def _transformed(rel: str, fn):
    """`rel` 的源码经 `fn` 变换后的读函数（其余文件原样）。"""
    patched = fn(_read(rel))

    def read(r):
        return patched if r == rel else _read(r)
    return read


def _fp(unit, read=None):
    return F.unit_fingerprint(unit, read)


# ── 1. 守卫本体 ─────────────────────────────────────────────────────────────

class TestCurrentCodeIsAcknowledged:
    def test_every_unit_matches_its_last_acknowledgement(self):
        res = F.check()
        msgs = []
        for unit, u in res["units"].items():
            if u["state"] == "unacknowledged":
                msgs.append(F.how_to_acknowledge(unit, u["current"]))
            elif u["state"] == "error":
                msgs.append(f"{unit} 算不出指纹：{u['error']}")
        assert res["ok"], "\n\n".join(msgs)

    def test_registry_is_consistent(self):
        assert F.registry_problems() == []


# ── 2. 指纹灵敏度：该变的变、不该变的不变 ────────────────────────────────────

class TestSensitivity:
    @pytest.mark.parametrize("unit,rel,old,new", [
        # Buzz 自己的通道公式（动量映射 / 量比档位 / 波动率档位）
        ("buzz.channels", "swarm_agents/buzz_bee.py", "/ 20 * 100", "/ 25 * 100"),
        ("buzz.channels", "swarm_agents/buzz_bee.py", "volume_signal = 80", "volume_signal = 85"),
        ("buzz.channels", "swarm_agents/buzz_bee.py", "vol_sentiment = 25", "vol_sentiment = 30"),
        # 通道读的阈值（config）与缩放函数
        ("buzz.channels", "config.py", '"very_high": 2.0', '"very_high": 2.5'),
        ("buzz.channels", "swarm_agents/cache.py", "return max(lo, min(hi, f))", "return max(lo, min(hi, f * 1.0001))"),
        # 三个行情字段的公式
        ("price.fields", "price_history.py", "def momentum_5d_detail(", "def momentum_5d_detail_x("),
        # v0.45.639：三个字段的公式收进 price_metrics（此前这里变异的是 cache.py 旧回落里的那份副本）
        ("price.fields", "price_metrics.py",
         'mom = float((hist["Close"].iloc[-1] / hist["Close"].iloc[-5] - 1) * 100)',
         'mom = float((hist["Close"].iloc[-1] / hist["Close"].iloc[-6] - 1) * 100)'),
        ("price.fields", "swarm_agents/cache.py",
         'data["volume_ratio"] = _m["volume_ratio"]',
         'data["volume_ratio"] = _m["volume_ratio"] or 1.0'),
    ], ids=["动量映射", "量比档位", "波动率档位", "量比阈值", "_safe_score", "动量回落函数改名", "共用动量窗口",
            "旧回落量比接线"])
    def test_formula_change_changes_the_fingerprint(self, unit, rel, old, new):
        assert _fp(unit, _mutated(rel, old, new)) != _fp(unit)

    def test_new_function_computing_a_price_field_is_picked_up(self):
        """自动识别：有人新加一条算 volume_ratio 的路径，不必记得去改 UNITS，指纹自己就变。"""
        rel = "data_pipeline.py"
        read = _transformed(rel, lambda s: s + "\n\ndef _another_volume_path(x):\n    x.volume_ratio = 1.0\n")
        assert _fp("price.fields", read) != _fp("price.fields")

    @pytest.mark.parametrize("unit,rel", [
        ("buzz.reddit", "reddit_sentiment.py"), ("buzz.news", "newsapi_client.py"),
        ("buzz.yahoo", "yahoo_trending.py"), ("buzz.fear_greed", "fear_greed.py"),
    ])
    def test_any_code_change_in_a_channel_module_changes_it(self, unit, rel):
        read = _transformed(rel, lambda s: s + "\n_GUARD_PROBE = 1\n")
        assert _fp(unit, read) != _fp(unit)

    @pytest.mark.parametrize("unit,rel", [
        ("buzz.channels", "swarm_agents/buzz_bee.py"), ("buzz.reddit", "reddit_sentiment.py"),
        ("price.fields", "data_pipeline.py"),
    ])
    def test_comments_and_blank_lines_do_not(self, unit, rel):
        read = _transformed(rel, lambda s: s.replace("\n", "\n# guard-probe comment\n\n", 3))
        assert _fp(unit, read) == _fp(unit)

    def test_docstring_edit_does_not(self):
        rel = "reddit_sentiment.py"
        src = _read(rel)
        first = src.index('"""')
        read = _transformed(rel, lambda s: s[:first + 3] + "守卫探针：只改 docstring。" + s[first + 3:])
        assert _fp("buzz.reddit", read) == _fp("buzz.reddit")

    def test_frozen_layer_change_does_not_touch_the_input_layer(self):
        """判别：改合成权重 / 合成之后的调整是冻结层改动，**不许**被要求输入层确认。"""
        rel = "swarm_agents/buzz_bee.py"
        for old, new in (('_bw.get("momentum", 0.20)', '_bw.get("momentum", 0.30)'),
                         ("score = sentiment_composite / 10.0", "score = sentiment_composite / 11.0")):
            assert _fp("buzz.channels", _mutated(rel, old, new)) == _fp("buzz.channels"), old

    def test_unrelated_function_in_a_shared_module_does_not(self):
        """`cache.py` / `data_pipeline.py` 是共享大模块：只取相关定义，别处改动不该要求确认。"""
        rel = "swarm_agents/cache.py"
        read = _transformed(rel, lambda s: s + "\n\ndef _guard_probe_unrelated():\n    return 42\n")
        assert _fp("buzz.channels", read) == _fp("buzz.channels")
        assert _fp("price.fields", read) == _fp("price.fields")


# ── 3. 定位不到 ⇒ 不当作「没变」────────────────────────────────────────────

class TestStructureChangeIsLoud:
    def test_missing_composite_assignment_raises(self):
        read = _mutated("swarm_agents/buzz_bee.py", "sentiment_composite = (", "sentiment_total = (")
        with pytest.raises(F.FingerprintError):
            _fp("buzz.channels", read)

    @pytest.mark.parametrize("rel,old,new,unit", [
        ("swarm_agents/cache.py", "def _safe_score(", "def _safe_score_renamed(", "buzz.channels"),
        ("config.py", '"volume_thresholds": {', '"volume_thresholds_x": {', "buzz.channels"),
    ], ids=["被指名的函数改名", "阈值键改名"])
    def test_named_target_gone_raises(self, rel, old, new, unit):
        """判别：定位不到时若静默给空串，第一次会因指纹变化而红，可一旦有人确认了，这个单元就**永远**
        不再跟踪那段代码——之后再怎么改都不红。所以必须抛。"""
        with pytest.raises(F.FingerprintError):
            _fp(unit, _mutated(rel, old, new))

    def test_file_with_no_price_field_definitions_raises(self):
        read = _transformed("twelve_data.py", lambda s: "X = 1\n")
        with pytest.raises(F.FingerprintError):
            _fp("price.fields", read)

    def test_check_reports_error_not_ok(self):
        read = _mutated("swarm_agents/buzz_bee.py", "sentiment_composite = (", "sentiment_total = (")
        res = F.check(read=read)
        assert res["ok"] is False and res["units"]["buzz.channels"]["state"] == "error"

    def test_unacknowledged_change_is_not_ok(self):
        read = _mutated("swarm_agents/buzz_bee.py", "/ 20 * 100", "/ 25 * 100")
        res = F.check(read=read)
        assert res["ok"] is False and res["units"]["buzz.channels"]["state"] == "unacknowledged"
        assert "buzz.comp.momentum_signal" in F.how_to_acknowledge("buzz.channels", res["units"]["buzz.channels"]["current"])

    def test_missing_file_is_an_error(self):
        def read(rel):
            if rel == "fear_greed.py":
                raise F.FingerprintError("模拟：文件不见了")
            return _read(rel)
        assert F.check(read=read)["units"]["buzz.fear_greed"]["state"] == "error"


# ── 4. 登记表自洽（合成登记表证明检查有牙）─────────────────────────────────

_FP = "0123456789abcdef"
_NOTE = "x" * F.MIN_NOTE_CHARS
_HIST = [("2026-10-20", "v9.9.1", "改新闻通道"), ("2026-10-20", "v9.9.2", "只改合成层"),
         ("2026-10-20", "v9.9.3", "未声明")]
_SCOPE = {"v9.9.1": ("buzz.comp.news_signal", "agent.BuzzBeeWhisper.*"),
          "v9.9.2": ("agent.BuzzBeeWhisper.*",)}


def _base(unit_overrides=()):
    rows = [(u, _FP, "baseline", _NOTE) for u in F.UNITS]
    return rows + list(unit_overrides)


class TestRegistryProblems:
    def test_clean_synthetic_registry(self):
        acks = _base([("buzz.news", "1" * 16, "v9.9.1", "")])
        assert F.registry_problems(acks, _HIST, _SCOPE) == []

    @pytest.mark.parametrize("extra,needle", [
        (("buzz.news", "1" * 16, "v9.9.2", ""), "没触及"),            # 只点名冻结层，却拿来确认通道改动
        (("buzz.news", "1" * 16, "v9.9.3", ""), "没在 COHORT_SIGNAL_SCOPE"),
        (("buzz.news", "1" * 16, "v9.9.9", ""), "不在 _COHORT_HISTORY"),
        (("buzz.news", "1" * 16, "no_value_change", "短"), "凭什么"),
        (("buzz.news", "1" * 16, "baseline", _NOTE), "baseline 只能是"),
        (("buzz.news", "xyz", "no_value_change", _NOTE), "16 位"),
        (("buzz.nope", "1" * 16, "no_value_change", _NOTE), "未知单元"),
        (("buzz.news", "1" * 16, "whatever", _NOTE), "未知确认方式"),
    ], ids=["边界只点名冻结层", "边界未声明影响面", "边界不在表里", "说明太短", "第二条baseline", "指纹格式",
            "未知单元", "未知确认方式"])
    def test_each_problem_is_reported(self, extra, needle):
        probs = F.registry_problems(_base([extra]), _HIST, _SCOPE)
        assert any(needle in p for p in probs), probs

    def test_first_ack_must_be_baseline_and_every_unit_needs_one(self):
        acks = [(u, _FP, "baseline", _NOTE) for u in F.UNITS if u != "buzz.yahoo"]
        acks.append(("buzz.yahoo", _FP, "no_value_change", _NOTE))
        probs = F.registry_problems(acks, _HIST, _SCOPE)
        assert any("第一条必须是 baseline" in p for p in probs)
        assert any("没有任何确认" in p for p in F.registry_problems(acks[:-1], _HIST, _SCOPE))


# ── 5. 覆盖 ─────────────────────────────────────────────────────────────────

class TestCoverage:
    def test_units_cover_exactly_the_h1_input_signals(self):
        """多一个信号 ⇒ 不相干的改动也要求确认；少一个 ⇒ 那个输入没人守。"""
        P = _protocol()
        covered = {s for u in F.UNITS.values() for s in u["signals"]}
        assert covered == set(P.H1_INPUT_SIGNALS)

    def test_frozen_layer_signals_are_not_guarded_here(self):
        """冻结层由冻结评分器吸收，不该出现在输入层守卫里。"""
        P = _protocol()
        covered = {s for u in F.UNITS.values() for s in u["signals"]}
        assert not covered & set(P.H1_FROZEN_LAYER_SIGNALS)
