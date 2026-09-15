"""蜂 `details` 的读取契约：读者读的键，生产者必须真的产出（v0.45.247）

缺陷形状：**有读者、没写者**
----------------------------
`QueenDistiller.distill()` 自 2026-03-30（caa432d2）起从 BuzzBee 的
`details["fear_greed_value"]` 读 F&G，驱动「F&G 政体分数调整」与「F&G>75 收紧看多门槛」。
但 BuzzBee 只把这个键放进了**发信息素板**的 `_pub_details`，返回的 `AgentResult.details`
里从来没有 ⇒ 仓库根 851 份 JSON 的 `swarm_results.fear_greed_value` 无一有值 ⇒
两段逻辑**从未执行过一次**。同一步里 Queen 读 Oracle `details["gex"]`
（794/794 份非 error 结果里不存在，同样只在 `_pub_details`）与 Guard
`market_regime.dealer_gex`（`detect_market_regime` 从不产出，794/794 为空）也是死的，
生产 GEX 一直落在第三条兜底（`distill` 内现场 `DealerGEXAnalyzer`）。
`deep_analysis` 与 `generate_ml_report` 读 Buzz 的 `components.fear_greed_signal` /
`fear_greed_index` / `components.fear_greed`，这三个键也都不存在。

为什么全绿：`dict.get` 把「键不存在」（接口断了）与「值为 None」（数据暂时拿不到）
渲染成同一个 None；所有 Queen 测试都往里喂手写的 details，**没有一条把真实蜂的输出接到读者上**。
这是 MEMORY `alpha-hive-dead-field`（算了没人读）的镜像。

本文件的三层
------------
1. **静态层（任何环境都跑，不依赖生产数据）**：AST 抽各蜂 `AgentResult(details={...})`
   字面量的键树作为「生产者真相」——由**当前代码**算出，不是快照夹具（快照会过期、恒绿）。
   · Queen 读 `details` 必须经 `_read_bee_detail`，登记表 `DETAIL_READS` 与调用点逐项一致，
     且每条都能在生产者键树里找到；
   · `signal_archive` 的全部 `agent_details.<蜂>.details.<路径>` 同样核对。
   生产者用 `**展开` 或变量填值时静态不可证 ⇒ 列入显式清单，**双向**断言
   （清单变大 = 新增了守卫看不见的读取；变小 = 清单过期）。
2. **运行期层**：`_read_bee_detail` 区分「键不存在」与「值为 None」，前者进
   `swarm_results.details_contract_misses` 并打 error —— 生产每次扫描都在真实输出上核一遍，
   静态不可证的那几条（Oracle `iv_rank`）靠这一层。
3. **集成层**：离线跑**真实** `BuzzBeeWhisper.analyze()`，把输出喂给 Queen、
   `signal_archive.extract`、两个展示函数。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

# 指向**代码**（被 AST 解析的源文件），`__file__` 是正确锚点（CLAUDE.md「代码还是数据」表）。
ROOT = Path(__file__).resolve().parent.parent

_OPAQUE = "<opaque>"
_PATH_RE = re.compile(r"^agent_details\.(\w+)\.details\.([\w.]+)$")


# ══════════════════════════════════════════════════════════════════════════
# 生产者键树（AST）
# ══════════════════════════════════════════════════════════════════════════

def _schema(node: ast.AST):
    """字面量 dict → {键: 子树}。非字面量的值 → `_OPAQUE`（再往下的路径不可证）；
    `**展开` → 在该层记一个 `_OPAQUE` 键（该层任何未显式出现的键不可证）。"""
    if not isinstance(node, ast.Dict):
        return _OPAQUE
    out: Dict = {}
    for k, v in zip(node.keys, node.values):
        if k is None:
            out[_OPAQUE] = True
        elif isinstance(k, ast.Constant) and isinstance(k.value, str):
            out[k.value] = _schema(v)
    return out


def _producers() -> Dict[str, list]:
    """source → [details 键树, ...]。只收 `source=` 为字面量的构造（error 结果的
    `make_error_result` 用变量 source，天然排除）。"""
    found: Dict[str, list] = {}
    for py in sorted((ROOT / "swarm_agents").glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and getattr(n.func, "id", None) == "AgentResult"):
                continue
            kw = {k.arg: k.value for k in n.keywords}
            src = kw.get("source")
            if not (isinstance(src, ast.Constant) and isinstance(src.value, str)):
                continue
            det = kw.get("details")
            found.setdefault(src.value, []).append(_schema(det) if det is not None else {})
    return found


def _resolve(schema, dotted: str) -> str:
    """'ok' | 'missing' | 'unprovable'"""
    cur = schema
    for part in dotted.split("."):
        if cur is _OPAQUE:
            return "unprovable"
        if part in cur:
            cur = cur[part]
        elif _OPAQUE in cur:
            return "unprovable"
        else:
            return "missing"
    return "ok"


@pytest.fixture(scope="module")
def producers():
    return _producers()


def _check(producers, source: str, dotted: str) -> str:
    schemas = producers.get(source)
    if not schemas:
        return "no_static_producer"
    return _resolve(schemas[0], dotted)


class TestProducerScanner:
    """先证明尺子本身可信——扫描器看错了，下面每条「ok」都不算数。"""

    BEES = ("BuzzBeeWhisper", "GuardBeeSentinel", "ScoutBeeNova", "OracleBeeEcho",
            "BearBeeContrarian", "ChronosBeeHorizon", "RivalBeeVanguard")

    def test_each_bee_has_exactly_one_non_error_construction(self, producers):
        """多出一处构造（比如新加一条降级分支）时，键树只取第一处会漏看 ⇒ 必须显式处理。"""
        for bee in self.BEES:
            assert len(producers.get(bee, [])) == 1, (
                f"{bee} 的 AgentResult(source=...) 构造有 {len(producers.get(bee, []))} 处；"
                "本守卫假定恰好 1 处。新增分支请让键树取各处**交集**再核。")

    def test_scanner_sees_known_keys_and_misses_invented_ones(self, producers):
        assert _check(producers, "BuzzBeeWhisper", "sentiment_pct") == "ok"
        assert _check(producers, "BuzzBeeWhisper", "components.reddit_signal") == "ok"
        assert _check(producers, "BuzzBeeWhisper", "no_such_key_xyz") == "missing"
        # Oracle 的 details 是 {**result, ...} ⇒ 展开层不可证，显式键仍可证
        assert _check(producers, "OracleBeeEcho", "term_structure") == "ok"
        assert _check(producers, "OracleBeeEcho", "iv_rank") == "unprovable"
        # Scout 的 components 值是变量 ⇒ 其下不可证
        assert _check(producers, "ScoutBeeNova", "components.social_volume") == "unprovable"

    def test_the_original_bug_would_have_been_red(self, producers):
        """正对照：原缺陷那两个键，扫描器必须判 missing（Buzz）与 unprovable-但不在显式键里（Oracle）。"""
        assert _check(producers, "BuzzBeeWhisper", "fear_greed_value") == "missing"
        assert _check(producers, "GuardBeeSentinel", "market_regime.dealer_gex") == "unprovable"


# ══════════════════════════════════════════════════════════════════════════
# 读者 1：QueenDistiller
# ══════════════════════════════════════════════════════════════════════════

QUEEN_SRC = ROOT / "swarm_agents" / "queen_distiller.py"

#: 静态不可证、只靠运行期层核对的 Queen 读取。理由逐条写明。
QUEEN_STATICALLY_UNPROVABLE = {
    # Oracle details = {**options_result, ...}：iv_rank 来自 OptionsAgent 的结果展开。
    # 运行期层在每次扫描核它（2026-09 生产 108/108 份非 error 结果都有此键）。
    ("OracleBeeEcho", "iv_rank"),
}


def _parents(tree: ast.AST) -> Dict[ast.AST, ast.AST]:
    return {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}


def _is_details_access(n: ast.AST) -> bool:
    if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
            and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "details"):
        return True
    return (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
            and n.slice.value == "details")


def _raw_details_reads(src_path: Path, allowed_func: str):
    """函数 `allowed_func` 之外、且不是「整份 details 原样抄进 dict 字面量的 "details" 键」
    的 details 访问 → [(行号, 所在函数)]。"""
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    par = _parents(tree)
    bad = []
    for n in ast.walk(tree):
        if not _is_details_access(n):
            continue
        func, cur, copied = None, n, False
        while cur in par:
            p = par[cur]
            if isinstance(p, ast.Dict) and not copied and func is None:
                for k, v in zip(p.keys, p.values):
                    if v is cur and isinstance(k, ast.Constant) and k.value == "details":
                        copied = True
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = p.name
                break
            cur = p
        if func == allowed_func or copied:
            continue
        bad.append((n.lineno, func))
    return bad


def _read_calls(src_path: Path) -> Tuple[set, list]:
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    pairs, non_literal = set(), []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "_read_bee_detail"):
            a = n.args
            if (len(a) >= 3 and isinstance(a[1], ast.Constant) and isinstance(a[2], ast.Constant)):
                pairs.add((a[1].value, a[2].value))
            else:
                non_literal.append(n.lineno)
    return pairs, non_literal


class TestQueenReadsAreDeclaredAndProduced:

    def test_no_raw_details_reads_outside_the_reader(self):
        """原缺陷就住在这种裸读里（`(_r.get("details") or {}).get("fear_greed_value")`）。"""
        bad = _raw_details_reads(QUEEN_SRC, allowed_func="_read_bee_detail")
        assert not bad, (
            f"queen_distiller.py 里有绕过 `_read_bee_detail` 的 details 读取：{bad}。"
            "裸 `.get` 把「键不存在」与「值为 None」渲染成同一个 None——原缺陷正是这样藏了半年。")

    def test_call_sites_match_registry_exactly(self):
        from swarm_agents.queen_distiller import QueenDistiller
        pairs, non_literal = _read_calls(QUEEN_SRC)
        assert not non_literal, f"`_read_bee_detail` 的 source/key 必须是字面量（行 {non_literal}），否则静态核不了"
        assert pairs == set(QueenDistiller.DETAIL_READS), (
            f"调用点 − 登记表 = {pairs - set(QueenDistiller.DETAIL_READS)}；"
            f"登记表 − 调用点 = {set(QueenDistiller.DETAIL_READS) - pairs}")

    def test_every_registered_read_is_produced(self, producers):
        from swarm_agents.queen_distiller import QueenDistiller
        missing, unprovable = [], set()
        for src, key in sorted(QueenDistiller.DETAIL_READS):
            r = _check(producers, src, key)
            if r in ("missing", "no_static_producer"):
                missing.append((src, key, r))
            elif r == "unprovable":
                unprovable.add((src, key))
        assert not missing, f"Queen 读了生产者不产出的键：{missing}"
        assert unprovable == QUEEN_STATICALLY_UNPROVABLE, (
            f"新增的静态不可证读取：{unprovable - QUEEN_STATICALLY_UNPROVABLE}；"
            f"清单里已可证/已删除（清单过期）：{QUEEN_STATICALLY_UNPROVABLE - unprovable}")


# ══════════════════════════════════════════════════════════════════════════
# 读者 2：signal_archive
# ══════════════════════════════════════════════════════════════════════════

ARCHIVE_SRC = ROOT / "signal_archive.py"

#: 静态不可证的归档路径。**不是「已验证活着」**，是「本守卫看不见」。
ARCHIVE_STATICALLY_UNPROVABLE = {
    # Oracle details = {**options_result, ...}
    *{("OracleBeeEcho", k) for k in ("gamma_exposure", "iv_current", "iv_percentile", "iv_rank",
                                      "iv_rank_source", "put_call_ratio", "total_oi")},
    # Rival details = {**prediction, ...}
    *{("RivalBeeVanguard", k) for k in ("expected_30d", "expected_7d", "probability")},
    # Scout components 的值是变量 component_scores
    *{("ScoutBeeNova", "components." + k) for k in ("consensus_strength", "google_trends",
                                                     "seeking_alpha_views", "short_squeeze_risk",
                                                     "social_volume")},
}

#: 生产者不走 AgentResult（返回裸 dict），静态扫描器够不到。
#: v0.45.250：原先列着 `(CodeExecutorAgent, market_cap/pe_ratio)`（2026-09-14 实测基本已死：
#: 成功路径的 details 把它们放在 `fetch_data` 下，归档只读顶层）。已修为按形状分派的
#: `signal_archive._code_exec_fetch`，其路径常量 `agent_details.CodeExecutorAgent.details`
#: 不带 `.details.<键>` 尾巴 ⇒ 本扫描器不再看到 CE 路径，清单清空。
#: CE 返回的是裸 dict，静态层本来就证明不了；现在由运行期测试兜底：
#: `tests/test_signal_archive_code_executor_shapes.py`（驱动真实 `analyze()` 的两条路径）。
#: 保留空集与双向断言：以后再有新的无静态生产者路径会红。
ARCHIVE_NO_STATIC_PRODUCER: set = set()


def _archive_paths() -> set:
    tree = ast.parse(ARCHIVE_SRC.read_text(encoding="utf-8"))
    paths = set()
    prefix_helpers: Dict[str, Tuple[str, str]] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            m = _PATH_RE.match(n.value)
            if m:
                paths.add((m.group(1), m.group(2)))
        if isinstance(n, ast.FunctionDef):
            consts = [c.value for c in ast.walk(n)
                      if isinstance(c, ast.Constant) and isinstance(c.value, str) and _PATH_RE.match(c.value)]
            params = [a.arg for a in n.args.args]
            if len(consts) == 1 and params == ["key"]:
                m = _PATH_RE.match(consts[0])
                prefix_helpers[n.name] = (m.group(1), m.group(2))
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in prefix_helpers
                and n.args and isinstance(n.args[0], ast.Constant)):
            src, prefix = prefix_helpers[n.func.id]
            paths.add((src, f"{prefix}.{n.args[0].value}"))
    return paths, prefix_helpers


class TestSignalArchivePathsAreProduced:

    def test_prefix_helpers_are_discovered(self):
        """`_buzz_comp` / `_crowding_comp` 从函数体里读前缀——测试不抄第二份映射。"""
        _, helpers = _archive_paths()
        assert helpers.get("_buzz_comp") == ("BuzzBeeWhisper", "components")
        assert helpers.get("_crowding_comp") == ("ScoutBeeNova", "components")

    def test_every_archive_path_is_produced(self, producers):
        paths, _ = _archive_paths()
        missing, unprovable, no_prod = [], set(), set()
        for src, dotted in sorted(paths):
            r = _check(producers, src, dotted)
            if r == "missing":
                missing.append((src, dotted))
            elif r == "unprovable":
                unprovable.add((src, dotted))
            elif r == "no_static_producer":
                no_prod.add((src, dotted))
        assert not missing, f"signal_archive 读了生产者不产出的路径（该信号恒不入档）：{missing}"
        assert unprovable == ARCHIVE_STATICALLY_UNPROVABLE, (
            f"新增静态不可证：{unprovable - ARCHIVE_STATICALLY_UNPROVABLE}；"
            f"清单过期：{ARCHIVE_STATICALLY_UNPROVABLE - unprovable}")
        assert no_prod == ARCHIVE_NO_STATIC_PRODUCER, (
            f"新增无静态生产者：{no_prod - ARCHIVE_NO_STATIC_PRODUCER}；"
            f"清单过期：{ARCHIVE_NO_STATIC_PRODUCER - no_prod}")

    def test_new_buzz_channels_are_archived(self):
        """v0.45.247 补的通道——没有它们，sentiment 维度做不了维度计算层重放。"""
        paths, _ = _archive_paths()
        for p in ("components.news_signal", "components.yahoo_signal",
                  "components.fear_greed_signal", "fear_greed.value"):
            assert ("BuzzBeeWhisper", p) in paths, f"signal_archive 未归档 Buzz {p}"


# ══════════════════════════════════════════════════════════════════════════
# 运行期层 + 集成层：真实 BuzzBee 输出
# ══════════════════════════════════════════════════════════════════════════

_CNN_FG = {"value": 33, "classification": "Fear", "sentiment_score": 3.3,
           "is_real_data": True, "source": "cnn", "timestamp": "2026-09-11T14:16:09"}


@pytest.fixture
def buzz_sources(monkeypatch, stub_reddit):
    """Buzz 的五个外部依赖全部钉在源头。F&G 默认给 CNN 形态；测试可覆盖。
    其余源用**各自模块的不可得契约函数**，不随手编值。"""
    import fear_greed
    import newsapi_client
    import yahoo_trending
    from swarm_agents import cache as _swarm_cache

    stock = {"price": 142.5, "momentum_5d": 3.2, "avg_volume": 45_000_000,
             "volume_ratio": 1.4, "volatility_20d": 38.5}
    monkeypatch.setattr(_swarm_cache, "_fetch_stock_data",
                        lambda ticker, target_date=None: dict(stock))
    monkeypatch.setattr(newsapi_client, "get_ticker_news",
                        lambda ticker, max_articles=10: newsapi_client._fallback(ticker))
    monkeypatch.setattr(yahoo_trending, "get_ticker_attention",
                        lambda ticker: yahoo_trending._default_result(ticker))
    state = {"fg": dict(_CNN_FG)}
    monkeypatch.setattr(fear_greed, "get_fear_greed", lambda: dict(state["fg"]))
    return state


def _run_buzz(ticker="NVDA"):
    from pheromone_board import PheromoneBoard
    from swarm_agents import BuzzBeeWhisper
    r = BuzzBeeWhisper(PheromoneBoard()).analyze(ticker)
    assert "error" not in r, f"真实 BuzzBee 离线跑出了 error，后续断言无意义：{r.get('error')}"
    return r


class TestRealBuzzOutput:

    def test_fear_greed_is_in_agent_result_details(self, buzz_sources):
        det = _run_buzz()["details"]
        assert det["fear_greed"]["value"] == 33
        assert det["fear_greed"]["source"] == "cnn"
        assert det["fear_greed"]["is_real_data"] is True
        for k in ("news_signal", "yahoo_signal", "fear_greed_signal"):
            assert k in det["components"], f"components 缺 {k}"

    def test_fallback_fear_greed_is_not_a_fake_50(self, buzz_sources):
        """`fear_greed._default_result()` 是 value=50 / is_real_data=False。
        照旧写法 `int(fg.get("value"))` 会把兜底喂成一个看起来真实的 50。"""
        import fear_greed
        buzz_sources["fg"] = fear_greed._default_result()
        fg = _run_buzz()["details"]["fear_greed"]
        assert fg["value"] is None
        assert fg["is_real_data"] is False

    def test_crypto_fallback_is_labelled(self, buzz_sources):
        """Alternative.me 是**加密**市场 F&G、同样标 is_real_data=True —— 必须把源带下去。"""
        buzz_sources["fg"] = {**_CNN_FG, "value": 13, "source": "alternative_me"}
        assert _run_buzz()["details"]["fear_greed"]["source"] == "alternative_me"


def _res(source, dimension, details, **over):
    r = {"source": source, "dimension": dimension, "score": 6.0, "direction": "neutral",
         "confidence": 0.6, "discovery": "t", "data_quality": {"x": "real"}, "details": details}
    r.update(over)
    return r


@pytest.fixture
def queen_inputs(buzz_sources, monkeypatch):
    """真实 Buzz + 其余蜂各自带齐 Queen 登记表里的键。
    Scout 带 price ⇒ 会走 distill 内现场 GEX，钉成空（不出网、不改分支结构）。"""
    import advanced_analyzer
    monkeypatch.setattr(advanced_analyzer.DealerGEXAnalyzer, "analyze",
                        lambda self, ticker, price, *a, **k: {})
    buzz = _run_buzz("NVDA")
    others = [
        _res("GuardBeeSentinel", "risk_adj", {"macro_regime": "neutral"}),
        _res("OracleBeeEcho", "odds", {"iv_rank": 40.0}),
        _res("ScoutBeeNova", "signal", {"price": 142.5}),
    ]
    return buzz, others


def _distill(results):
    from pheromone_board import PheromoneBoard
    from swarm_agents.queen_distiller import QueenDistiller
    return QueenDistiller(board=PheromoneBoard(), enable_llm=False).distill("NVDA", results)


class TestQueenRuntimeContract:

    def test_real_buzz_plus_well_formed_peers_has_no_misses(self, queen_inputs):
        buzz, others = queen_inputs
        out = _distill([buzz, *others])
        assert out["details_contract_misses"] == []

    def test_missing_key_is_reported(self, queen_inputs):
        """有牙：生产者丢了一个 Queen 在规则模式下必读的键 ⇒ 必须记账。"""
        buzz, others = queen_inputs
        others[0] = _res("GuardBeeSentinel", "risk_adj", {})
        out = _distill([buzz, *others])
        assert out["details_contract_misses"] == ["GuardBeeSentinel.macro_regime"]

    def test_none_value_is_not_a_contract_break(self, queen_inputs):
        """键在、值为 None = 数据暂时拿不到，合法。"""
        buzz, others = queen_inputs
        others[1] = _res("OracleBeeEcho", "odds", {"iv_rank": None})
        assert _distill([buzz, *others])["details_contract_misses"] == []

    def test_error_result_is_not_a_contract_break(self, queen_inputs):
        """蜂自己报了 error ⇒ details 为空是已声明的失败，不是接口断裂。"""
        buzz, others = queen_inputs
        others[0] = _res("GuardBeeSentinel", "risk_adj", {}, error="boom")
        assert _distill([buzz, *others])["details_contract_misses"] == []

    def test_dead_fear_greed_output_field_is_gone(self, queen_inputs):
        """恒为 None 的输出字段看起来像「接上了、只是今天没值」——删掉比留着诚实。"""
        buzz, others = queen_inputs
        assert "fear_greed_value" not in _distill([buzz, *others])


class TestDownstreamReadersOfRealBuzz:
    """把真实 Buzz 输出接到下游读者上——原缺陷里没有任何一条测试这样接。"""

    def test_signal_archive_extracts_fear_greed_and_channels(self, buzz_sources):
        import signal_archive
        sig = signal_archive.extract({"agent_details": {"BuzzBeeWhisper": _run_buzz()}})
        assert sig["market.fear_greed"] == 33.0
        assert sig["market.fear_greed_is_cnn"] == 1.0
        for k in ("buzz.comp.news_signal", "buzz.comp.yahoo_signal", "buzz.comp.fear_greed_signal"):
            assert k in sig, f"{k} 未入档"

    def test_signal_archive_omits_fallback_fear_greed(self, buzz_sources):
        import fear_greed
        import signal_archive
        buzz_sources["fg"] = fear_greed._default_result()
        sig = signal_archive.extract({"agent_details": {"BuzzBeeWhisper": _run_buzz()}})
        assert "market.fear_greed" not in sig, "兜底 50 不得作为观测值入档"
        assert "market.fear_greed_is_cnn" not in sig

    def test_signal_archive_labels_crypto_source(self, buzz_sources):
        import signal_archive
        buzz_sources["fg"] = {**_CNN_FG, "value": 13, "source": "alternative_me"}
        sig = signal_archive.extract({"agent_details": {"BuzzBeeWhisper": _run_buzz()}})
        assert sig["market.fear_greed_is_cnn"] == 0.0

    def test_deep_analysis_renders_fear_greed(self, buzz_sources):
        import deep_analysis
        r = _run_buzz()
        assert "33" in deep_analysis._render_details("buzz", r).split("恐惧贪婪", 1)[-1][:80]
        assert "恐惧贪婪指数 33" in deep_analysis._phase1_block("buzz", r)

    def test_deep_analysis_does_not_render_fallback(self, buzz_sources):
        import deep_analysis
        import fear_greed
        buzz_sources["fg"] = fear_greed._default_result()
        r = _run_buzz()
        assert "恐惧贪婪" not in deep_analysis._render_details("buzz", r)
        assert "恐惧贪婪" not in deep_analysis._phase1_block("buzz", r)

    def test_ml_report_renders_fear_greed(self, buzz_sources):
        from generate_ml_report import MLEnhancedReportGenerator as G
        html = G._ch3_buzz(G.__new__(G), {"BuzzBeeWhisper": _run_buzz()})
        assert "恐贪指数" in html and ">33<" in html

    def test_ml_report_does_not_render_fallback(self, buzz_sources):
        import fear_greed
        from generate_ml_report import MLEnhancedReportGenerator as G
        buzz_sources["fg"] = fear_greed._default_result()
        html = G._ch3_buzz(G.__new__(G), {"BuzzBeeWhisper": _run_buzz()})
        assert "恐贪指数" not in html
