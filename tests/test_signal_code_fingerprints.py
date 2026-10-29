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
        # 三个行情字段的公式。v0.45.671：动量回落的窗口常数不提字段名，此前改它指纹不变（依赖闭包补上）
        ("price.fields", "price_history.py", "MOMENTUM_LOOKBACK = 4", "MOMENTUM_LOOKBACK = 5"),
        # v0.45.639：三个字段的公式收进 price_metrics（此前这里变异的是 cache.py 旧回落里的那份副本）
        ("price.fields", "price_metrics.py",
         'mom = float((hist["Close"].iloc[-1] / hist["Close"].iloc[-5] - 1) * 100)',
         'mom = float((hist["Close"].iloc[-1] / hist["Close"].iloc[-6] - 1) * 100)'),
        ("price.fields", "swarm_agents/cache.py",
         'data["volume_ratio"] = _m["volume_ratio"]',
         'data["volume_ratio"] = _m["volume_ratio"] or 1.0'),
    ], ids=["动量映射", "量比档位", "波动率档位", "量比阈值", "_safe_score", "动量回落窗口常数", "共用动量窗口",
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
        # v0.45.671：被调用的依赖改了名、调用方没跟——闭包定位不到它的定义 ⇒ 抛
        ("price_history.py", "def momentum_5d_detail(", "def momentum_5d_detail_x(", "price.fields"),
    ], ids=["被指名的函数改名", "被调用的依赖改名"])
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


# ── 3b. 依赖闭包（v0.45.671）：单元之外、单元代码却依赖的东西 ───────────────────

_UNITS6 = ("buzz.channels", "buzz.reddit", "buzz.news", "buzz.yahoo", "buzz.fear_greed", "price.fields")


_BASE_FPS: dict = {}


def _changed(read):
    if not _BASE_FPS:
        _BASE_FPS.update({u: _fp(u) for u in _UNITS6})
    return {u for u in _UNITS6 if _fp(u, read) != _BASE_FPS[u]}


class TestDependencyClosure:
    """根因：单元盯哪些代码曾是手写清单，没人核对它全不全。这一组在 v0.45.671 之前的代码上全部红（缺口本身）。"""

    def test_the_gap_news_primary_switch(self):
        """新闻主源开关（注释写着「回滚用」）改成 alpha_vantage——此前指纹不红、桥接照过，H1 新闻通道却换了量纲。"""
        read = _mutated("config.py", '"primary": "massive",', '"primary": "alpha_vantage",')
        assert _changed(read) == {"buzz.news"}

    def test_config_is_tracked_down_to_the_key_actually_read(self):
        """Reddit 缓存时长改了 ⇒ 只有 buzz.reddit 变；改一个 H1 谁都不读的缓存时长 ⇒ 谁都不变（不制造噪音）。"""
        assert _changed(_mutated("config.py", '"reddit": 600,', '"reddit": 900,')) == {"buzz.reddit"}
        assert _changed(_mutated("config.py", '"google_trends": 86400,', '"google_trends": 3600,')) == set()

    def test_config_key_renamed_is_recorded_as_absent(self):
        """代码按 `.get("volume_thresholds", {})` 读、键没了 ⇒ 记成「不存在」（这也是一个值，指纹变）。
        以后代码改读新键，闭包跟着代码走——不会「确认一次就永远不再跟踪」。"""
        read = _mutated("config.py", '"volume_thresholds": {', '"volume_thresholds_x": {')
        assert _fp("buzz.channels", read) != _fp("buzz.channels")
        deps = F.unit_dependencies("buzz.channels", read)
        assert "volume_thresholds = <absent>" in deps["config.py::AGENT_SCORING[volatility_thresholds,volume_thresholds]"]

    @pytest.mark.parametrize("rel,old,new", [
        # 同文件、不提字段名的辅助函数：决定 Twelve Data 量比收不收
        ("data_pipeline.py", "s = _expected_session((today - timedelta(days=1)).isoformat())",
         "s = _expected_session((today - timedelta(days=2)).isoformat())"),
        # 别的模块：CBOE 收盘核实
        ("cboe_options.py", "session_end = datetime.combine(last_trade.date(), close_t)",
         "session_end = datetime.combine(last_trade.date(), close_t) + timedelta(minutes=1)"),
    ], ids=["同文件辅助函数 _latest_closed_session", "跨模块 close_verdict"])
    def test_helpers_outside_the_located_code_are_tracked(self, rel, old, new):
        src = _read(rel)
        if src.count(old) != 1:     # CBOE 那句出现两次（close_verdict 与另一处）：只改 close_verdict 里的
            start = src.index("def close_verdict(")
            end = src.index("\ndef ", start + 1)
            body = src[start:end]
            assert body.count(old) == 1, "锚点漂了，更新本测试"
            read = _transformed(rel, lambda t: t[:start] + body.replace(old, new) + t[end:])
        else:
            read = _mutated(rel, old, new)
        assert "price.fields" in _changed(read)

    @pytest.mark.parametrize("rel,old,new", [
        ("observation_archive.py", '_event(kind, "ok", sha=sha, request=request)',
         '_event(kind, "ok!", sha=sha, request=request)'),
        ("llm_service.py", "def analyze_news_sentiment(", "def analyze_news_sentiment2("),
        ("config.py", '"momentum": 0.20,\n        "volume": 0.10,', '"momentum": 0.30,\n        "volume": 0.10,'),
    ], ids=["只存不改值的观测档案", "只在 LLM 模式生效", "合成权重（冻结层）"])
    def test_exempted_dependencies_do_not(self, rel, old, new):
        assert _changed(_mutated(rel, old, new)) == set()

    def test_stops_at_another_unit_that_guards_the_same_signal(self):
        """buzz.channels 调 get_ticker_news ⇒ 新闻模块的改动归 buzz.news 认领，buzz.channels 不重复要求确认。"""
        read = _mutated("newsapi_client.py", '_MASSIVE_LABELS = {"positive": "bullish"',
                        '_MASSIVE_LABELS = {"positive": "neutral"')
        assert _changed(read) == {"buzz.news"}

    def test_does_not_stop_when_the_owner_guards_other_signals(self):
        """观测层缓存函数在 price.fields 的整份定位里，但 price.fields 不守新闻 / F&G ⇒ 两个情绪单元照样算进去。"""
        read = _mutated("observation_layer.py", "if not _cache_doc_ok(doc):", "if not _cache_doc_ok(doc) or False:")
        assert {"buzz.news", "buzz.fear_greed", "price.fields"} <= _changed(read)

    @pytest.mark.parametrize("rel,old,new", [
        # BuzzBee 经 self._get_stock_data() 取行情（基类 BeeAgent）：补跑时不再透传目标日期
        ("swarm_agents/base.py", 'data = _cache._fetch_stock_data(ticker, getattr(self, "_target_date", None))',
         "data = _cache._fetch_stock_data(ticker)"),
        # 经 self._validate_ticker() 判代码合不合法：正则收窄 ⇒ 某些标的整只不出通道
        ("swarm_agents/_config.py", "_RE_TICKER = _re.compile(r'^[A-Z]{1,5}(?:[.-][A-Z])?$')",
         "_RE_TICKER = _re.compile(r'^[A-Z]{1,4}(?:[.-][A-Z])?$')"),
    ], ids=["基类方法 _get_stock_data", "基类方法用到的 _RE_TICKER"])
    def test_methods_reached_through_self_are_tracked(self, rel, old, new):
        """v0.45.676（671 二次检查）：`self.方法` 此前不解析，基类里的这两处改了指纹不红。"""
        assert _changed(_mutated(rel, old, new)) == {"buzz.channels"}

    def test_only_the_member_not_the_whole_base_class(self):
        """精度：基类里 Buzz 不调的方法（v0.45.674 刚改过的宏观快照访问器）改了 ⇒ 谁都不变。"""
        read = _mutated("swarm_agents/base.py", 'return getattr(self, "_prefetched_macro", None)',
                        'return getattr(self, "_prefetched_macro", {})')
        assert _changed(read) == set()

    def test_instance_attribute_set_in_init_is_tracked(self):
        """v0.45.679（676 二次检查）：`_get_stock_data` 读的 `self._prefetched_stock` 在 `__init__` 里赋缺省值——
        类内赋值静态可解，676 把它当「推不出来」放过；改了只让 buzz.channels 变。"""
        read = _mutated("swarm_agents/base.py", "self._prefetched_stock: Dict[str, Dict] = {}",
                        "self._prefetched_stock: Dict[str, Dict] = dict(x={})")
        assert _changed(read) == {"buzz.channels"}

    def test_unused_instance_attribute_is_not(self):
        """精度：`__init__` 里 Buzz 不读的属性（674 加的宏观快照）改了 ⇒ 谁都不变。"""
        read = _mutated("swarm_agents/base.py", "self._prefetched_macro: Optional[Dict] = None",
                        "self._prefetched_macro: Optional[Dict] = {}")
        assert _changed(read) == set()

    @pytest.mark.parametrize("old,new,expect", [
        # 值改成引用另一个名字（基线），再改那个名字——两个变体之间只差 600 → 900
        ('"reddit": 600,', '"reddit": _REDDIT_TTL,', {"buzz.reddit"}),
        # 键是变量 ⇒ ttl 下哪个键都可能被改 ⇒ 读 ttl 下任何键的单元都算（保守）
        ("def get_cache_ttl(", "for _k in ('reddit',):\n    CACHE_CONFIG['ttl'][_k] = 60\n\ndef get_cache_ttl(",
         {"buzz.reddit", "buzz.yahoo"}),
        # 常量键 ⇒ 只算给读这个键的单元（buzz.yahoo 读的 ttl.yahoo_trending 不受影响）
        ("def get_cache_ttl(", "def _tune():\n    CACHE_CONFIG['ttl']['reddit'] = 60\n_tune()\n\ndef get_cache_ttl(",
         {"buzz.reddit"}),
    ], ids=["值里引用的名字", "模块层循环里就地改（变量键）", "函数里就地改、模块层调用（常量键）"])
    def test_config_values_reached_indirectly_are_tracked(self, old, new, expect):
        """v0.45.676：config 值里再引用的名字此前停在字面文本上；就地修改此前只认模块层直接语句。"""
        if "_REDDIT_TTL" in new:
            def variant(ttl):
                return _transformed("config.py", lambda t: t.replace(old, new).replace(
                    "CACHE_CONFIG = {", f"_REDDIT_TTL = {ttl}\nCACHE_CONFIG = {{", 1))
            assert {u for u in _UNITS6 if _fp(u, variant(600)) != _fp(u, variant(900))} == expect
        else:
            assert _changed(_mutated("config.py", old, new)) == expect

    def test_expected_dependencies_are_present(self):
        """正对照：缺口里点过名的依赖确实在。"""
        assert "config.py::NEWS_SOURCE_CONFIG" in F.unit_dependencies("buzz.news")
        assert "config.py::CACHE_CONFIG[ttl.reddit,ttl.reddit_memory]" in F.unit_dependencies("buzz.reddit")
        ch = F.unit_dependencies("buzz.channels")
        assert "config.py::AGENT_SCORING[volatility_thresholds,volume_thresholds]" in ch, list(ch)
        for label in ("swarm_agents/base.py::BeeAgent._get_stock_data", "swarm_agents/base.py::BeeAgent._validate_ticker",
                      "swarm_agents/_config.py::_RE_TICKER"):
            assert label in ch, label
        assert not any(k.startswith("swarm_agents/base.py::BeeAgent._scan_macro") for k in ch), "只取用到的成员"
        assert "swarm_agents/base.py::BeeAgent.__init__[self._prefetched_stock]" in ch
        assert not any("_prefetched_macro" in k for k in ch), "只取用到的实例属性"
        pf = F.unit_dependencies("price.fields")
        for label in ("data_pipeline.py::_latest_closed_session", "cboe_options.py::close_verdict",
                      "price_history.py::MOMENTUM_LOOKBACK", "is_trading_day.py::session_close_et"):
            assert label in pf, label

    def test_exemption_registry_is_clean(self):
        assert F.exemption_problems() == []

    @pytest.mark.parametrize("key,reason,needle", [
        (("nonexistent_module.py", "*"), "x" * F.MIN_NOTE_CHARS, "过期条目"),
        (("observation_archive.py", "*"), "太短", "没写凭什么"),
    ], ids=["过期", "理由太短"])
    def test_exemption_registry_problems_are_reported(self, monkeypatch, key, reason, needle):
        monkeypatch.setitem(F.EXEMPT_DEPS, key, reason)
        assert any(needle in p for p in F.exemption_problems())


# ── 3c. 依赖解析器本身（合成源码，逐个规则有牙）────────────────────────────────

def _synthetic(monkeypatch, files: dict, units: dict):
    monkeypatch.setattr(F, "UNITS", units)
    monkeypatch.setattr(F, "EXEMPT_DEPS", {})

    def read(rel):
        if rel not in files:
            raise F.FingerprintError(f"没有 {rel}")
        return files[rel]
    return read


_CFG = 'TTL = {"a": 1, "b": {"x": 2, "y": 3}}\nFLAG = True\nTTL["b"]["z"] = 9\n'


class TestResolver:
    def _unit(self, code):
        return {"u": {"code": (("m.py", "def:f"),), "signals": ("s",)}}

    def test_keypath_alias_reexport_and_relative_import(self, monkeypatch):
        files = {"config.py": _CFG,
                 "pkg/__init__.py": "",
                 "pkg/conf.py": "from config import TTL as _T\n",
                 "pkg/m.py": "from .conf import _T\n\ndef f():\n    return _T['b'].get('x', 0)\n"}
        read = _synthetic(monkeypatch, files, {"u": {"code": (("pkg/m.py", "def:f"),), "signals": ("s",)}})
        deps = F.unit_dependencies("u", read)
        assert list(deps) == ["config.py::TTL[b.x]"]
        assert deps["config.py::TTL[b.x]"].splitlines()[0] == "TTL.b.x = 2"
        assert "TTL['b']['z']" not in deps["config.py::TTL[b.x]"], "改的是兄弟键 b.z，读的 b.x 不受影响（v0.45.676 起按键分）"
        files["config.py"] = _CFG + 'TTL["b"]["x"] = 7\nTTL["b"].update(q=1)\n'
        read = _synthetic(monkeypatch, files, {"u": {"code": (("pkg/m.py", "def:f"),), "signals": ("s",)}})
        text = F.unit_dependencies("u", read)["config.py::TTL[b.x]"]
        assert "TTL['b']['x'] = 7" in text and "TTL['b'].update(q=1)" in text, "改到读过的键（或其上层）的就地修改一并算进去"

    def test_local_names_and_main_block_are_not_dependencies(self, monkeypatch):
        files = {"m.py": "r = 1\nHELPER = 2\n\ndef f(x):\n    r = x + HELPER\n    return r\n\n"
                         "if __name__ == '__main__':\n    HELPER = 3\n    tk = 'NVDA'\n",
                 "config.py": _CFG}
        read = _synthetic(monkeypatch, files, self._unit("m.py"))
        deps = F.unit_dependencies("u", read)
        assert list(deps) == ["m.py::HELPER"], "局部变量 r 不是对模块层 r 的引用"
        assert deps["m.py::HELPER"] == "HELPER = 2", "脚本入口里的赋值不是可被 import 的定义"

    def test_transitive_and_third_party(self, monkeypatch):
        files = {"m.py": "import json\nfrom n import g\n\ndef f():\n    return g() + json.dumps(1).count('1')\n",
                 "n.py": "import config\n\ndef g():\n    return h() if config.FLAG else 0\n\ndef h():\n    return 1\n",
                 "config.py": _CFG}
        read = _synthetic(monkeypatch, files, self._unit("m.py"))
        assert list(F.unit_dependencies("u", read)) == ["config.py::FLAG", "n.py::g", "n.py::h"]

    def test_missing_definition_is_loud(self, monkeypatch):
        files = {"m.py": "from n import gone\n\ndef f():\n    return gone()\n", "n.py": "X = 1\n", "config.py": _CFG}
        read = _synthetic(monkeypatch, files, self._unit("m.py"))
        with pytest.raises(F.FingerprintError):
            F.unit_dependencies("u", read)

    def test_whole_object_when_not_read_by_constant_key(self, monkeypatch):
        files = {"m.py": "from config import TTL\n\ndef f(k):\n    return TTL[k]\n", "config.py": _CFG}
        read = _synthetic(monkeypatch, files, self._unit("m.py"))
        assert list(F.unit_dependencies("u", read)) == ["config.py::TTL"]

    # ── v0.45.676：类成员 / 子模块链 / config 间接引用 ──

    _BASE = ("import config\n\nclass Base:\n    K = config.FLAG\n\n    def get(self):\n        return self.norm(1)\n\n"
             "    def norm(self, x):\n        return x\n\n    def unused(self):\n        return 0\n")

    def test_self_member_resolves_through_the_base_class_and_only_that_member(self, monkeypatch):
        files = {"base.py": self._BASE, "config.py": _CFG,
                 "m.py": "from base import Base\n\nclass Sub(Base):\n    def run(self):\n        return self.get() + self.K\n"}
        # 定位 Sub.run 的方法体（不含类本身）——与 buzz_channel_block 同形：类没被整个定位，基类不会经 bases 进来
        monkeypatch.setitem(F._LOCATORS, "run_body", lambda tree, rel, _a: (
            "x", list(tree.body[1].body[0].body), set()))
        read = _synthetic(monkeypatch, files, {"u": {"code": (("m.py", "run_body"),), "signals": ("s",)}})
        assert list(F.unit_dependencies("u", read)) == [
            "base.py::Base.K", "base.py::Base.get", "base.py::Base.norm", "config.py::FLAG"], \
            "get 里的 self.norm 接着追；unused 不进来；类属性 K 引用的 config 也追"

    def test_subclass_override_wins_and_super_goes_to_the_base(self, monkeypatch):
        files = {"base.py": self._BASE, "config.py": _CFG,
                 "m.py": "from base import Base\n\nclass Sub(Base):\n    def norm(self, x):\n        return super().norm(x) + 1\n\n"
                         "    def run(self):\n        return self.get()\n"}
        monkeypatch.setitem(F._LOCATORS, "run_body", lambda tree, rel, _a: (
            "x", list(tree.body[1].body[1].body), set()))
        read = _synthetic(monkeypatch, files, {"u": {"code": (("m.py", "run_body"),), "signals": ("s",)}})
        deps = F.unit_dependencies("u", read)
        assert "m.py::Sub.norm" in deps, "基类 get 里的 self.norm 指的是子类覆盖的那个（实例是 Sub）"
        assert "base.py::Base.norm" in deps, "super().norm 指基类的"

    def test_injected_attributes_and_external_bases_are_not_errors(self, monkeypatch):
        files = {"m.py": "import abc\n\nclass C(abc.ABC):\n    def __init__(self):\n        self.data = {}\n\n"
                         "    def run(self):\n        return self.data.get('x'), self.injected, self.__class__.__name__\n",
                 "config.py": _CFG}
        monkeypatch.setitem(F._LOCATORS, "run_body", lambda tree, rel, _a: (
            "x", list(tree.body[1].body[1].body), set()))
        read = _synthetic(monkeypatch, files, {"u": {"code": (("m.py", "run_body"),), "signals": ("s",)}})
        # v0.45.679 起类内赋值算进去（676 时这里断言为空）；从类外塞进来的 injected、外部基类 ABC 推不出来，不当错误
        assert F.unit_dependencies("u", read) == {"m.py::C.__init__[self.data]": "self.data = {}"}

    # ── v0.45.679：实例属性的类内赋值 / 泛型基类 / getattr(self, 常量) / 与处理顺序无关 ──

    _ATTRS = ("import config\nlimit = config.FLAG\n\nclass Base:\n    def __init__(self, limit):\n"
              "        self.limit = limit\n        if config.FLAG:\n            self.mode = 'a'\n"
              "        setattr(self, 'tag', TAG)\n        self.other = 1\n\nTAG = 'x'\n")

    def _run_unit(self, monkeypatch, files, method_index=0):
        monkeypatch.setitem(F._LOCATORS, "run_body", lambda tree, rel, _a: (
            "x", list(tree.body[1].body[method_index].body), set()))
        read = _synthetic(monkeypatch, {"config.py": _CFG, **files},
                          {"u": {"code": (("m.py", "run_body"),), "signals": ("s",)}})
        return F.unit_dependencies("u", read)

    def test_instance_attribute_sources_in_the_class(self, monkeypatch):
        deps = self._run_unit(monkeypatch, {
            "base.py": self._ATTRS,
            "m.py": "from base import Base\n\nclass Sub(Base):\n    def run(self):\n"
                    "        return self.limit, self.mode, self.tag\n"})
        assert list(deps) == ["base.py::Base.__init__[self.limit]", "base.py::Base.__init__[self.mode]",
                              "base.py::Base.__init__[self.tag]", "base.py::TAG", "config.py::FLAG"], list(deps)
        assert "if config.FLAG" in deps["base.py::Base.__init__[self.mode]"], "取方法体里的顶层语句：条件也在里面"
        assert deps["base.py::Base.__init__[self.limit]"] == "self.limit = limit", \
            "参数 limit 不是模块层的 limit（只把语句拿出来追，没有方法的作用域）"

    def test_generic_base_and_getattr_on_self(self, monkeypatch):
        deps = self._run_unit(monkeypatch, {
            "base.py": self._BASE,
            "m.py": "from base import Base\n\nclass Sub(Base[int]):\n    def run(self):\n"
                    "        return getattr(self, 'norm')(1)\n"})
        assert list(deps) == ["base.py::Base.norm"]

    def test_result_does_not_depend_on_which_is_reached_first(self, monkeypatch):
        """先碰到成员 Base.get、后碰到整个 Base（get 里引用了类名）⇒ 只留整类。"""
        base = self._BASE.replace("        return self.norm(1)\n", "        return Base.K\n")
        deps = self._run_unit(monkeypatch, {
            "base.py": base,
            "m.py": "from base import Base\n\nclass Sub(Base):\n    def run(self):\n        return self.get()\n"})
        assert "base.py::Base" in deps and "base.py::Base.get" not in deps, list(deps)

    def test_submodule_attribute_chain(self, monkeypatch):
        files = {"pkg/__init__.py": "", "pkg/sub.py": "def g():\n    return 1\n", "config.py": _CFG,
                 "m.py": "import pkg.sub\n\ndef f():\n    return pkg.sub.g(), id(pkg.sub)\n"}
        read = _synthetic(monkeypatch, files, self._unit("m.py"))
        assert list(F.unit_dependencies("u", read)) == ["pkg/sub.py::g"], \
            "顺着属性链走到子模块（此前报「pkg 里找不到 sub」）；子模块对象整个被拿去用不当错误"

    @pytest.mark.parametrize("cfg,expect", [
        ("T0 = 5\nTTL = {'a': T0}\n", ["config.py::T0", "config.py::TTL[a]"]),
        ("TTL = {'a': 1}\nfor _k in ('a',):\n    TTL[_k] = 2\n", ["config.py::TTL[a]"]),
        ("TTL = {'a': 1}\ndef _tune():\n    TTL['a'] = _V\n_V = 3\n_tune()\n", ["config.py::TTL[a]", "config.py::_V"]),
        ("TTL = {'a': 1}\ndef _reset():\n    global TTL\n    TTL = {'a': 2}\n_reset()\n", ["config.py::TTL[a]"]),
        # 变量键在前、常量键在后：改的是「任意键下的 x」，读的 a 也可能被改到（前缀到变量键为止，不能跳过它）
        ("TTL = {'a': {'x': 1}}\nfor _k in ('a',):\n    TTL[_k]['x'] = 0\n", ["config.py::TTL[a]"]),
    ], ids=["值里引用的名字", "循环里就地改", "函数里就地改（改成的值接着追）", "函数里 global 重新赋值",
            "变量键之后的常量键"])
    def test_config_indirection(self, monkeypatch, cfg, expect):
        files = {"m.py": "from config import TTL\n\ndef f():\n    return TTL['a']\n", "config.py": cfg}
        read = _synthetic(monkeypatch, files, self._unit("m.py"))
        deps = F.unit_dependencies("u", read)
        assert list(deps) == expect
        if "for _k" in cfg or "_tune" in cfg or "_reset" in cfg:
            assert "TTL" in deps["config.py::TTL[a]"].split("\n", 1)[1], "改动它的语句在文本里"


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
