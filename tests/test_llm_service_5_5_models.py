"""
tests/test_llm_service_5_5_models.py — v0.45.555：llm_service 为 LLM 影子 A/B 做的改动

全部用假客户端，**不调用任何 API**。覆盖：
  · 未登记模型一律拒绝（以前按 Haiku 价计费，换 5.5 代模型后预算闸少算 2~4 倍）
  · Sonnet 5.5 / Opus 5.5 不传 temperature（会 400）、走 extra_body、max_tokens 放宽
  · 成本公式：usage.input_tokens 是**未缓存**输入，总输入 = input + 缓存写 + 缓存读
  · 截断 / JSON 解析失败 / 预算拒绝 / 未登记拒绝 四个观测计数
  · 在途预留：并行蜂共用预算
  · 进程级模型覆盖
  · OracleBee 提示词精简：不改入参、丢矩阵、异动三口径取并集
"""
import copy
import json

import pytest

import llm_service


@pytest.fixture(autouse=True)
def _reset_llm_state(monkeypatch):
    monkeypatch.setattr(llm_service, "_disabled", False)
    monkeypatch.setattr(llm_service, "_client", None)
    monkeypatch.setattr(llm_service, "_api_key", None)
    monkeypatch.setattr(llm_service, "_model_override", None)
    monkeypatch.setattr(llm_service, "_inflight_cost", 0.0)
    monkeypatch.setattr(llm_service, "_token_usage", {
        "input_tokens": 0, "output_tokens": 0, "total_cost_usd": 0.0, "call_count": 0,
        "truncated_calls": 0, "json_parse_failures": 0, "budget_refusals": 0, "unpriced_refusals": 0,
        "failed_calls": 0,
    })
    from datetime import date
    monkeypatch.setattr(llm_service, "_budget_date", date.today())


class _Block:
    def __init__(self, text):
        self.text = text


class _Usage:
    def __init__(self, inp=10, out=20, cc=0, cr=0):
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_creation_input_tokens = cc
        self.cache_read_input_tokens = cr


class _Resp:
    def __init__(self, text="ok", usage=None, stop="end_turn"):
        self.content = [_Block(text)]
        self.usage = usage or _Usage()
        self.stop_reason = stop


class _FakeClient:
    """记录每次 messages.create 的 kwargs。"""

    def __init__(self, resp=None, raises=None):
        self.calls = []
        self._resp = resp
        self._raises = raises
        outer = self

        class _Messages:
            @staticmethod
            def create(**kw):
                outer.calls.append(kw)
                if outer._raises:
                    raise outer._raises
                return outer._resp or _Resp()

        self.messages = _Messages()


def _install(monkeypatch, client):
    monkeypatch.setattr(llm_service, "_get_client", lambda: client)
    return client


# ==================== 定价表 / 未登记模型 ====================

class TestPricingGuard:
    def test_unpriced_model_refused_and_counted(self, monkeypatch):
        c = _install(monkeypatch, _FakeClient())
        assert llm_service.call("hi", model="claude-mystery-9") is None
        assert c.calls == []                       # 根本没发请求
        assert llm_service.get_usage()["unpriced_refusals"] == 1

    @pytest.mark.parametrize("model,inp,out", [
        ("claude-sonnet-5-5", 2.0e-6, 10.0e-6),
        ("claude-opus-5-5", 4.0e-6, 20.0e-6),
    ])
    def test_5_5_models_priced_exactly(self, model, inp, out):
        assert llm_service._PRICING[model]["input"] == pytest.approx(inp)
        assert llm_service._PRICING[model]["output"] == pytest.approx(out)

    def test_every_profile_model_is_priced(self):
        assert set(llm_service._MODEL_PROFILES) <= set(llm_service._PRICING)


# ==================== 模型档案：采样参数 / extra_body / max_tokens ====================

class TestModelProfiles:
    def test_haiku_default_unchanged(self, monkeypatch):
        c = _install(monkeypatch, _FakeClient())
        llm_service.call("hi", max_tokens=256, temperature=0.2)
        kw = c.calls[0]
        assert kw["model"] == "claude-haiku-4-5-20251001"
        assert kw["temperature"] == 0.2
        assert kw["max_tokens"] == 256
        assert "extra_body" not in kw

    @pytest.mark.parametrize("model,extra", [
        ("claude-sonnet-5-5", {"thinking": {"type": "between_tools"}}),
        ("claude-opus-5-5", {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}),
    ])
    def test_5_5_models_omit_temperature_and_use_extra_body(self, monkeypatch, model, extra):
        c = _install(monkeypatch, _FakeClient())
        llm_service.call("hi", model=model, max_tokens=256, temperature=0.2)
        kw = c.calls[0]
        assert "temperature" not in kw             # 传了会 400
        assert kw["extra_body"] == extra
        # Opus：max(256*3, 256+1024) = 1280；Sonnet：max(256*3, 256+2048) = 2304（v0.45.595 放宽）
        assert kw["max_tokens"] == {"claude-sonnet-5-5": 2304, "claude-opus-5-5": 1280}[model]

    @pytest.mark.parametrize("requested,expected", [(256, 1280), (400, 1424), (800, 2400)])
    def test_max_tokens_headroom(self, requested, expected):
        assert llm_service._effective_max_tokens("claude-opus-5-5", requested) == expected

    @pytest.mark.parametrize("requested,expected", [(150, 2198), (256, 2304), (300, 2348), (400, 2448), (800, 2848)])
    def test_sonnet_max_tokens_headroom(self, requested, expected):
        assert llm_service._effective_max_tokens("claude-sonnet-5-5", requested) == expected

    # 10-19 试跑实测的自然输出长度（Opus 放宽后的 text token）：蜂单次评估 ≤ 440、BearBee 空头论述最长 1215、Queen 蒸馏 1105
    _LONGEST_NATURAL_OUTPUT = 1215

    def test_sonnet_cap_is_at_least_1_5x_the_longest_measured_output_for_every_call_site(self):
        """Sonnet 11/18 次截断的真因是调用点上限（256~800）< 自然输出长度。每个调用点放宽后的上限都要 ≥ 1.5 × 实测最长。"""
        import ast
        from pathlib import Path
        tree = ast.parse((Path(llm_service.__file__)).read_text(encoding="utf-8"))
        sites = sorted({kw.value.value for n in ast.walk(tree) if isinstance(n, ast.Call)
                        for kw in n.keywords if kw.arg == "max_tokens" and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, int)})
        assert len(sites) >= 4, sites                      # 解析器没解析到调用点就不算数
        for req in sites:
            eff = llm_service._effective_max_tokens("claude-sonnet-5-5", req)
            assert eff >= 1.5 * self._LONGEST_NATURAL_OUTPUT, (req, eff)

    def test_the_guard_has_teeth_old_sonnet_profile_would_fail(self):
        """正对照：旧档案（+1024）下 BearBee 的 400 只给 1424 < 1.5 × 1215，这条判据会红。"""
        old = max(int(400 * 3), 400 + 1024)
        assert old < 1.5 * self._LONGEST_NATURAL_OUTPUT

    def test_default_profile_no_headroom(self):
        assert llm_service._effective_max_tokens("claude-haiku-4-5-20251001", 256) == 256

    def test_profile_extra_body_not_mutated_across_calls(self, monkeypatch):
        c = _install(monkeypatch, _FakeClient())
        llm_service.call("a", model="claude-opus-5-5")
        c.calls[0]["extra_body"]["output_config"]["effort"] = "max"   # 调用方乱改
        llm_service.call("b", model="claude-opus-5-5")
        # 共享同一个字典会被带坏：这里只断言档案本身仍是 low（若被改成 max，说明共享了可变对象）
        assert llm_service._MODEL_PROFILES["claude-opus-5-5"]["extra_body"]["output_config"]["effort"] == "low"


# ==================== 成本公式 ====================

class TestCostFormula:
    def test_cache_tokens_are_additive_not_subtracted(self, monkeypatch):
        """实测：count_tokens 总输入 2318 = input 1505 + 缓存写 812 + 缓存读 0。"""
        c = _install(monkeypatch, _FakeClient(_Resp(usage=_Usage(inp=1505, out=300, cc=812, cr=0))))
        llm_service.call("hi", model="claude-sonnet-5-5")
        u = llm_service.get_usage()
        p_in, p_out = 2.0e-6, 10.0e-6
        assert u["total_cost_usd"] == pytest.approx(1505 * p_in + 812 * p_in * 1.25 + 300 * p_out)
        assert u["input_tokens"] == 1505 + 812

    def test_cache_read_multiplier_per_model(self, monkeypatch):
        _install(monkeypatch, _FakeClient(_Resp(usage=_Usage(inp=100, out=0, cc=0, cr=1000))))
        llm_service.call("hi", model="claude-opus-5-5")
        assert llm_service.get_usage()["total_cost_usd"] == pytest.approx(100 * 4e-6 + 1000 * 4e-6 * 0.05)

    def test_cache_read_never_makes_negative_cost(self, monkeypatch):
        _install(monkeypatch, _FakeClient(_Resp(usage=_Usage(inp=0, out=10, cc=0, cr=5000))))
        llm_service.call("hi")
        assert llm_service.get_usage()["total_cost_usd"] > 0

    def test_no_cache_fields_still_works(self, monkeypatch):
        class _U:
            input_tokens = 10
            output_tokens = 20
        _install(monkeypatch, _FakeClient(_Resp(usage=_U())))
        assert llm_service.call("hi") == "ok"
        assert llm_service.get_usage()["input_tokens"] == 10


# ==================== 观测计数 ====================

class TestCounters:
    def test_truncation_counted(self, monkeypatch):
        _install(monkeypatch, _FakeClient(_Resp(text='{"a": 1, "b":', stop="max_tokens")))
        assert llm_service.call("hi") == '{"a": 1, "b":'
        assert llm_service.get_usage()["truncated_calls"] == 1

    def test_end_turn_not_counted_as_truncated(self, monkeypatch):
        _install(monkeypatch, _FakeClient(_Resp(stop="end_turn")))
        llm_service.call("hi")
        assert llm_service.get_usage()["truncated_calls"] == 0

    def test_json_failure_counted_and_returns_none(self, monkeypatch):
        _install(monkeypatch, _FakeClient(_Resp(text='{"score": 7.57,', stop="max_tokens")))
        assert llm_service.call_json("hi") is None
        u = llm_service.get_usage()
        assert u["json_parse_failures"] == 1 and u["truncated_calls"] == 1

    def test_valid_json_not_counted(self, monkeypatch):
        _install(monkeypatch, _FakeClient(_Resp(text='{"x": 1}')))
        assert llm_service.call_json("hi") == {"x": 1}
        assert llm_service.get_usage()["json_parse_failures"] == 0

    def test_call_returning_none_is_not_a_json_failure(self, monkeypatch):
        monkeypatch.setattr(llm_service, "_get_client", lambda: None)
        assert llm_service.call_json("hi") is None
        assert llm_service.get_usage()["json_parse_failures"] == 0

    def test_budget_refusal_counted(self, monkeypatch):
        monkeypatch.setattr(llm_service, "_token_usage", {
            "input_tokens": 0, "output_tokens": 0, "total_cost_usd": 999.0, "call_count": 0})
        c = _install(monkeypatch, _FakeClient())
        assert llm_service.call("hi") is None
        assert c.calls == []
        assert llm_service.get_usage()["budget_refusals"] == 1     # 字典里原先没有这个键也不能 KeyError

    def test_connection_error_counted_as_failed_call(self, monkeypatch):
        """call_count 只数成功的：请求发了却没拿到响应，以前完全看不见（v0.45.560）。"""
        _install(monkeypatch, _FakeClient(raises=ConnectionError("x")))
        assert llm_service.call("hi") is None
        u = llm_service.get_usage()
        assert u["failed_calls"] == 1 and u["call_count"] == 0

    def test_unexpected_api_error_counted_then_propagates(self, monkeypatch):
        _install(monkeypatch, _FakeClient(raises=RuntimeError("400 bad request")))
        with pytest.raises(RuntimeError):
            llm_service.call("hi")
        assert llm_service.get_usage()["failed_calls"] == 1

    def test_rate_limit_exhausted_counted(self, monkeypatch):
        import anthropic
        monkeypatch.setattr(llm_service, "_RATE_LIMIT_RETRY_DELAYS", ())
        monkeypatch.setattr(llm_service.time, "sleep", lambda s: None)
        err = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
        _install(monkeypatch, _FakeClient(raises=err))
        assert llm_service.call("hi") is None
        assert llm_service.get_usage()["failed_calls"] == 1

    def test_success_and_refusals_are_not_failed_calls(self, monkeypatch):
        _install(monkeypatch, _FakeClient())
        assert llm_service.call("hi") == "ok"
        assert llm_service.call("hi", model="claude-mystery-9") is None
        assert llm_service.get_usage()["failed_calls"] == 0

    def test_daily_reset_clears_extra_counters(self, monkeypatch):
        from datetime import date, timedelta
        monkeypatch.setattr(llm_service, "_budget_date", date.today() - timedelta(days=1))
        llm_service._token_usage["truncated_calls"] = 7
        llm_service._token_usage["json_parse_failures"] = 3
        llm_service._token_usage["failed_calls"] = 2
        llm_service._maybe_reset_daily_budget()
        u = llm_service.get_usage()
        assert u["truncated_calls"] == 0 and u["json_parse_failures"] == 0 and u["failed_calls"] == 0


# ==================== 在途预留 ====================

class TestInflightReservation:
    def test_inflight_counts_against_budget(self, monkeypatch):
        c = _install(monkeypatch, _FakeClient())
        monkeypatch.setattr(llm_service, "_inflight_cost", 5.0)    # 别的蜂的在途调用已占满预算
        assert llm_service.call("hi") is None
        assert c.calls == []

    def test_inflight_released_after_success(self, monkeypatch):
        _install(monkeypatch, _FakeClient())
        llm_service.call("hi")
        assert llm_service._inflight_cost == pytest.approx(0.0)

    def test_inflight_released_after_exception(self, monkeypatch):
        _install(monkeypatch, _FakeClient(raises=ConnectionError("x")))
        assert llm_service.call("hi") is None
        assert llm_service._inflight_cost == pytest.approx(0.0)

    def test_inflight_released_when_unexpected_exception_propagates(self, monkeypatch):
        _install(monkeypatch, _FakeClient(raises=RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            llm_service.call("hi")
        assert llm_service._inflight_cost == pytest.approx(0.0)    # finally 必须释放，否则预算被在途幽灵占死

    def test_estimate_includes_input_not_only_output(self, monkeypatch):
        """以前只按 max_tokens×输出单价估：输入巨大的提示词也能通过预检。"""
        from config import LLM_CONFIG
        monkeypatch.setitem(LLM_CONFIG, "daily_budget_usd", 0.05)
        c = _install(monkeypatch, _FakeClient())
        # 5 万字符 ≈ $0.05（Haiku $1/MTok）+ 输出，必超 $0.05
        assert llm_service.call("字" * 50_000, max_tokens=16) is None
        assert c.calls == []


# ==================== 进程级模型覆盖 ====================

class TestModelOverride:
    def test_override_applies_to_all_callers(self, monkeypatch):
        c = _install(monkeypatch, _FakeClient())
        llm_service.set_model_override("claude-opus-5-5")
        llm_service.call("hi")                       # 调用点用的是缺省 Haiku
        assert c.calls[0]["model"] == "claude-opus-5-5"
        assert "temperature" not in c.calls[0]

    def test_override_none_restores(self, monkeypatch):
        c = _install(monkeypatch, _FakeClient())
        llm_service.set_model_override("claude-opus-5-5")
        llm_service.set_model_override(None)
        llm_service.call("hi")
        assert c.calls[0]["model"] == "claude-haiku-4-5-20251001"

    def test_unpriced_override_rejected(self):
        with pytest.raises(ValueError):
            llm_service.set_model_override("claude-mystery-9")

    def test_no_override_by_default(self):
        assert llm_service._model_override is None


# ==================== OracleBee 提示词精简 ====================

def _options_fixture():
    unusual = [
        {"type": "large_call_volume", "strike": 365.0, "volume": 1664, "oi": 1633, "ratio": 1.02,
         "bullish": True, "dollar_premium": 2_660_736, "expiry": "2026-10-30"},
        {"type": "large_put_volume", "strike": 362.5, "volume": 466, "oi": 330, "ratio": 1.42,
         "bullish": False, "dollar_premium": 900_000, "expiry": "2026-10-30"},
        # 金额小、但成交量与 vol/OI 极端的新仓——只按溢价取前 N 会漏掉它
        {"type": "call_sweep", "strike": 230.0, "volume": 869, "oi": 1, "ratio": 869.0,
         "bullish": True, "dollar_premium": 12_000, "expiry": "2026-11-02"},
    ] + [
        {"type": "large_call_volume", "strike": 300.0 + i, "volume": 100 + i, "oi": 5000, "ratio": 0.02,
         "bullish": True, "dollar_premium": 100_000 + i * 1000, "expiry": "2026-10-30"}
        for i in range(30)
    ]
    return {
        "ticker": "NVDA", "iv_rank": 8.2, "put_call_ratio": 0.81, "options_score": 6.0,
        "raw_chain": {"huge": list(range(1000))},
        "full_chain_oi": {
            "total_call_oi": 7_000_000, "total_put_oi": 5_000_000, "full_pc_ratio": 0.71,
            "max_pain": 200.0, "expiry_count": 24,
            "top_call_oi": [{"strike": 400.0 + i, "oi": 1000 - i} for i in range(10)],
            "top_put_oi": [{"strike": 300.0 - i, "oi": 900 - i} for i in range(10)],
            "expiry_breakdown": [{"expiry": "2026-10-30", "call_oi": 1, "put_oi": 2}],
            "oi_by_strike_call": {str(k): k for k in range(90)},
            "oi_by_strike_put": {str(k): k for k in range(90)},
            "call_exp_oi": {str(k): {"2026-10-30": k} for k in range(90)},
            "put_exp_oi": {str(k): {"2026-10-30": k} for k in range(90)},
        },
        "unusual_activity": unusual,
    }


class TestCompactOptions:
    def test_does_not_mutate_input(self):
        src = _options_fixture()
        before = copy.deepcopy(src)
        llm_service._compact_options_for_llm(src)
        assert src == before                         # OracleBee 的下游还要用这份结果

    def test_drops_raw_chain_and_matrices(self):
        out = llm_service._compact_options_for_llm(_options_fixture())
        assert "raw_chain" not in out
        for k in ("call_exp_oi", "put_exp_oi", "oi_by_strike_call", "oi_by_strike_put"):
            assert k not in out["full_chain_oi"]

    def test_keeps_scalars_and_term_structure(self):
        out = llm_service._compact_options_for_llm(_options_fixture())
        assert out["iv_rank"] == 8.2 and out["put_call_ratio"] == 0.81
        fc = out["full_chain_oi"]
        assert fc["total_call_oi"] == 7_000_000 and fc["max_pain"] == 200.0
        assert fc["expiry_breakdown"]               # 到期日分布保留

    def test_top_lists_sliced(self):
        fc = llm_service._compact_options_for_llm(_options_fixture())["full_chain_oi"]
        assert len(fc["top_call_oi"]) == 5 and len(fc["top_put_oi"]) == 5
        assert fc["top_call_oi"][0]["strike"] == 400.0      # 保留的是前 5（已按 OI 降序），不是后 5

    def test_unusual_union_keeps_extreme_volume_small_premium(self):
        ua = llm_service._compact_options_for_llm(_options_fixture())["unusual_activity"]
        strikes = {(x["type"], x["strike"]) for x in ua["selected"]}
        assert ("call_sweep", 230.0) in strikes      # 溢价排不进前 8，靠成交量 / vol/OI 口径入选
        assert ua["total_count"] == 33
        assert ua["bullish_count"] == 32 and ua["bearish_count"] == 1
        assert len(ua["selected"]) <= 18

    def test_unusual_sorted_by_premium_desc(self):
        sel = llm_service._compact_options_for_llm(_options_fixture())["unusual_activity"]["selected"]
        prem = [x["dollar_premium"] for x in sel]
        assert prem == sorted(prem, reverse=True)

    def test_unknown_direction_is_not_counted_as_bearish(self):
        r = {"unusual_activity": [{"strike": 1, "volume": 1}, {"strike": 2, "bullish": False}]}
        ua = llm_service._compact_options_for_llm(r)["unusual_activity"]
        assert ua["bullish_count"] == 0 and ua["bearish_count"] == 1 and ua["total_count"] == 2

    def test_tolerates_missing_and_malformed_fields(self):
        r = {"unusual_activity": [None, "x", {"volume": None, "ratio": "abc", "dollar_premium": None}],
             "full_chain_oi": "not-a-dict"}
        out = llm_service._compact_options_for_llm(r)
        assert out["full_chain_oi"] == "not-a-dict"
        assert out["unusual_activity"]["total_count"] == 3

    def test_empty_result_ok(self):
        assert llm_service._compact_options_for_llm({}) == {}

    def test_output_is_much_smaller(self):
        src = _options_fixture()
        big = len(json.dumps({k: v for k, v in src.items() if k != "raw_chain"}, ensure_ascii=False))
        small = len(json.dumps(llm_service._compact_options_for_llm(src), ensure_ascii=False))
        assert small < big * 0.5


class TestInterpretOptionsFlowUsesCompact:
    def test_prompt_has_no_matrices(self, monkeypatch):
        seen = {}

        def fake_call_json(prompt, **kw):
            seen["prompt"] = prompt
            return {"smart_money_score": 5}
        monkeypatch.setattr(llm_service, "call_json", fake_call_json)
        llm_service.interpret_options_flow("NVDA", _options_fixture(), {"price": 200.0})
        p = seen["prompt"]
        assert "call_exp_oi" not in p and "oi_by_strike_call" not in p and "raw_chain" not in p
        assert "iv_rank" in p and "put_call_ratio" in p
        assert "call_sweep" in p                      # 极端成交量的小额新仓在提示词里
