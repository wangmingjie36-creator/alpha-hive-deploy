"""OracleBeeEcho 的方向来自「在中文摘要里数关键词」，而那张词表是单边的（v0.45.201）

背景（全部为 `pheromone.db::agent_memory` 实测，1789 行、2026-04-06~09-10，非推断）
--------------------------------------------------------------------------------
Oracle 的方向是三级级联，分数带（唯一有中性区、唯一均衡的那条）排在最后：

    if unusual_flow["unusual_direction"] in ("bullish","bearish"): ...   ← 分支1
    elif _bull_count > _bear_count: bullish                              ← 分支2
    elif _bear_count > _bull_count: bearish
    elif score < 4.0: bearish / elif score > 6.5: bullish / else neutral ← 分支3

闭式反解（`discovery` 原样存着 `signal_summary`，无需插桩；复现率 99.8%，
去掉分支1 掉到 93.3%、关键词换成无关词掉到 81.9% ⇒ 复现率有鉴别力）：

    分支2  982 行 (55.0%)  —— **982 行全部 bullish，五个月零次 bearish**
    分支1  662 行 (37.1%)  —— 567 bullish
    分支3  141 行 ( 7.9%)  ——  22 bullish   ← 均衡的那条只占 7.9%

分支2 恒为看多，因为词表在本语料里是单边的：

  · 五个看多词里只有「看涨」出现过，1542 行，且**永远来自同一句**
    `检测到 N 个看涨异动`（options_analyzer.py:1397，`bullish_unusual > 0`
    时无条件拼上）。`bearish_unusual` 这个量**全仓不存在**，没有对应项。
  · 五个看空关键词**一次都没出现过** ⇒ `_bear_count` 恒为 0 ⇒
    `elif _bear_count > _bull_count` 是结构上不可达的分支。
    （与 ChronosBee v0.43.0 的 `elif score <= 4.5: bearish` 同族。）
  · 真正有方向含义的那句 `做多气氛浓厚（P/C低）`（811 行）**不匹配任何关键词**，
    从不投票。会投票的反倒是那句不含方向的计数。

而这 982 行的 `unusual_direction` **全部**是 neutral/absent —— 专职方向探测器
说「无方向」，被一个子串计数改判成看多；其中能看到 Call/Put 明细的 467 行里
有 127 行（27.2%）实际是 **Put > Call**。

同 CodeExecutor 兜底（v0.45.191）：把「这类数据存在」当成「方向看多」的同一物种。

修复：删掉分支2。方向只走 专职方向探测器 → 分数带，并抽成
`_decide_direction()` 以便直接断言（照 ChronosBee `_apply_pead_direction` 的先例）。

位移（台账实测）：bullish 88.0%→69.9%，neutral 6.3%→23.7%，81.9% 的行逐字节不变。
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from swarm_agents.oracle_bee import OracleBeeEcho

# 生产语料里真实出现过的摘要形状（取自 agent_memory.discovery）
SUMMARY_WITH_BULL_COUNT = (
    "做多气氛浓厚（P/C低） | 检测到 23 个看涨异动 | TermStr:contango(26/29) | "
    "Skew25d:1.10 | MaxPain:$450(+9.4%) | 异常流:混合期权流 Call:47 Put:34"
)
SUMMARY_PUT_HEAVY = (
    "检测到 8 个看涨异动 | TermStr:flat(49/50) | MaxPain:$170(+4.0%) | "
    "异常流:混合期权流 Call:34 Put:47"
)
SUMMARY_PLAIN = "信号平衡 | 异常流:无异常期权流信号"

MID_SCORE = 5.0      # 落在 [4.0, 6.5] 中性带内
HIGH_SCORE = 7.2     # > 6.5
LOW_SCORE = 3.1      # < 4.0

NEUTRAL_FLOW = {"unusual_direction": "neutral", "summary": "混合期权流 Call:47 Put:34"}
ABSENT_FLOW = {}
BULL_FLOW = {"unusual_direction": "bullish", "summary": "异常Call流 34个信号 溢价$43.9M"}
BEAR_FLOW = {"unusual_direction": "bearish", "summary": "异常Put流 40个信号 溢价$51.0M"}


@pytest.fixture
def bee():
    return OracleBeeEcho.__new__(OracleBeeEcho)


class TestKeywordVoteRemoved:
    """核心回归：修复前这三条全部返回 bullish。"""

    def test_bullish_unusual_phrase_does_not_force_bullish(self, bee):
        """`检测到 N 个看涨异动` 是一个单边计数，不是方向证据。

        生产实测：正是这一句在 982 行里把方向钉成 bullish。
        """
        d = bee._decide_direction(NEUTRAL_FLOW, MID_SCORE, SUMMARY_WITH_BULL_COUNT)
        assert d == "neutral", (
            f"分数 {MID_SCORE} 落在中性带、专职探测器说 neutral，"
            f"却因摘要里有「看涨异动」判成 {d}"
        )

    def test_put_heavy_mixed_flow_is_not_bullish(self, bee):
        """Call:34 Put:47（实际偏空）不得因为「8 个看涨异动」被判看多。

        生产实测：关键词分支判多的 467 行有 Call/Put 明细，其中 127 行 Put > Call。
        """
        d = bee._decide_direction(NEUTRAL_FLOW, MID_SCORE, SUMMARY_PUT_HEAVY)
        assert d != "bullish", "Put 多于 Call 的混合流被判成了看多"

    @pytest.mark.parametrize("summary", [
        SUMMARY_WITH_BULL_COUNT,
        SUMMARY_PUT_HEAVY,
        SUMMARY_PLAIN,
        "",
        None,
        # 即便有朝一日上游真的开始输出看空措辞，方向也不该由文本决定
        "检测到 12 个看跌异动 | 空头占优 | 下行风险",
        "看多 看涨 多头 走高 上行",
        "看空 看跌 空头 下行 走低",
    ])
    def test_direction_is_independent_of_summary_text(self, bee, summary):
        """真正的契约：固定 (专职探测器, 分数) 后，摘要文本不得改变方向。

        这条是本文件的主守卫 —— 谁再往方向里接一个文本投票，它就红。
        """
        d = bee._decide_direction(NEUTRAL_FLOW, MID_SCORE, summary)
        assert d == "neutral", f"摘要文本改变了方向: {summary!r} → {d}"


class TestScoreBandStillDecides:
    """正对照：删掉分支2 之后，剩下的路径必须真的还能判出三种方向。

    没有这一组，上面「恒为 neutral」的断言可以被一个恒返回 neutral 的实现满足。
    """

    def test_high_score_is_bullish(self, bee):
        assert bee._decide_direction(NEUTRAL_FLOW, HIGH_SCORE, SUMMARY_PLAIN) == "bullish"

    def test_low_score_is_bearish(self, bee):
        assert bee._decide_direction(NEUTRAL_FLOW, LOW_SCORE, SUMMARY_PLAIN) == "bearish"

    def test_mid_score_is_neutral(self, bee):
        assert bee._decide_direction(NEUTRAL_FLOW, MID_SCORE, SUMMARY_PLAIN) == "neutral"

    @pytest.mark.parametrize("flow", [NEUTRAL_FLOW, ABSENT_FLOW])
    def test_absent_flow_falls_through_to_score_band(self, bee, flow):
        assert bee._decide_direction(flow, HIGH_SCORE, SUMMARY_PLAIN) == "bullish"


class TestUnusualFlowStillOverrides:
    """分支1 是唯一的专职方向探测器，不能一并删掉。"""

    def test_bullish_flow_beats_low_score(self, bee):
        assert bee._decide_direction(BULL_FLOW, LOW_SCORE, SUMMARY_PLAIN) == "bullish"

    def test_bearish_flow_beats_high_score(self, bee):
        assert bee._decide_direction(BEAR_FLOW, HIGH_SCORE, SUMMARY_PLAIN) == "bearish"

    @pytest.mark.parametrize("bad", ["neutral", "unknown", None, "", 1, True])
    def test_only_directional_values_override(self, bee, bad):
        """非 bullish/bearish 的取值必须落回分数带，不能当成方向。"""
        assert bee._decide_direction(
            {"unusual_direction": bad}, MID_SCORE, SUMMARY_PLAIN) == "neutral"


def _code_only(src: str) -> str:
    """只留可执行代码：注释与 docstring 一律剥掉。

    必须剥 —— 修复后的 `_decide_direction` docstring 里**引用**了这些关键词
    来解释为什么删掉它们。按原始源码断言会把「解释」误判成「重新引入」。
    """
    import ast
    import textwrap
    tree = ast.parse(textwrap.dedent(src))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _body_only(func) -> str:
    """只要函数体（剥签名、注释、docstring）。"""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    fn = tree.body[0]
    body = fn.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return "\n".join(ast.unparse(n) for n in body)


class TestSourceLevel:
    """结构守卫：关键词投票不得以任何形式回到方向决策里。"""

    KEYWORDS = ("看多", "看涨", "多头", "走高", "上行",
                "看空", "看跌", "空头", "下行", "走低")

    def test_no_direction_keyword_anywhere_in_class(self):
        """全类扫描，不是只看 analyze —— 把计数搬进另一个方法不该让守卫转绿。

        （STEP1 实测：仅断言 analyze 时，把同一段代码抽成 helper 就无声变绿。）
        """
        import inspect
        offenders = {}
        for name, fn in vars(OracleBeeEcho).items():
            fn = getattr(fn, "__func__", fn)
            if not callable(fn) or not hasattr(fn, "__code__"):
                continue
            try:
                code = _code_only(inspect.getsource(fn))
            except (OSError, TypeError, SyntaxError):
                continue
            hit = [k for k in self.KEYWORDS if k in code]
            if hit:
                offenders[name] = hit
        assert not offenders, f"方向关键词投票出现在: {offenders}"

    def test_decider_does_not_read_summary_for_direction(self):
        """signal_summary 只允许作为**入参**存在，函数体内不得读它。

        只能看函数体：签名里当然有 signal_summary，按整段源码断言会恒红。
        """
        assert "signal_summary" not in _body_only(OracleBeeEcho._decide_direction), (
            "signal_summary 又被方向决策读了 —— 文本不是方向证据"
        )

    def test_body_only_sees_the_signature_is_excluded(self):
        """自证：_body_only 确实把签名排除了，否则上一条永远红/永远绿都说不清。"""
        import inspect
        full = _code_only(inspect.getsource(OracleBeeEcho._decide_direction))
        assert "signal_summary" in full, "签名里本就该有 signal_summary"
        assert "unusual_flow" in _body_only(OracleBeeEcho._decide_direction), (
            "_body_only 把函数体也吃掉了 —— 那它对任何东西都不会红"
        )

    def test_counting_helpers_are_gone(self):
        import inspect
        src = inspect.getsource(OracleBeeEcho)
        assert "_bull_count" not in src and "_bear_count" not in src, (
            "关键词计数还在类里"
        )


class TestGuardsHaveTeeth:
    """没有这一组，上面的结构守卫证明不了任何事。"""

    def test_upstream_still_emits_the_phrase(self):
        """正对照：上游确实还在输出「看涨异动」。

        若哪天这句没了，`test_direction_is_independent_of_summary_text`
        的夹具就变成了一段无关文本，守卫会静默失效。
        """
        import options_analyzer
        src = open(options_analyzer.__file__, encoding="utf-8").read()
        assert "个看涨异动" in src, (
            "上游不再输出「看涨异动」—— 本文件的夹具已与生产脱节，先核对再改断言"
        )

    def test_bearish_counterpart_still_absent_upstream(self):
        """登记现状：上游只数看多异动，没有 bearish_unusual。

        不是在固化缺陷，是在标记「单边」这个前提仍然成立；
        哪天补上了对应项，这条会红，提醒回来重评方向逻辑。
        """
        import options_analyzer
        src = open(options_analyzer.__file__, encoding="utf-8").read()
        assert "bearish_unusual" not in src, (
            "上游新增了 bearish_unusual —— 单边前提已变，回来重评方向逻辑"
        )

    def test_reintroduction_would_be_caught(self):
        """反向自证：把关键词投票塞回一段源码，走**同一条** _code_only 判据必须抓到。"""
        fake = ('def _decide_direction(f, s, ss):\n'
                '    """解释里提到 看跌 不算重新引入。"""\n'
                '    if "看涨" in ss:  # 注释里的 看空 也不算\n'
                '        return "bullish"\n'
                '    return "neutral"\n')
        code = _code_only(fake)
        hit = [k for k in TestSourceLevel.KEYWORDS if k in code]
        assert hit == ["看涨"], (
            f"判据失灵：应只抓到代码里的「看涨」，实际 {hit}"
            "（抓多了说明没剥注释/docstring，抓不到说明形同虚设）"
        )

    def test_docstring_mention_is_not_flagged(self):
        """互补的另一半：只出现在 docstring/注释里的关键词不得被误报。"""
        fake = ('def _decide_direction(f, s, ss):\n'
                '    """这里解释为什么删掉 看涨/看空 投票。"""\n'
                '    # 看跌 也只在注释里\n'
                '    return "neutral"\n')
        code = _code_only(fake)
        assert not [k for k in TestSourceLevel.KEYWORDS if k in code], (
            "把解释误判成了重新引入"
        )


class TestCohortBoundary:
    """方向直通 final_score ⇒ 必须登记世代边界（否则新旧口径样本被静默混算）。"""

    def _history(self):
        import ic_rerun_readiness
        return ic_rerun_readiness._COHORT_HISTORY

    def test_entry_appended(self):
        assert any(v == "v0.45.201" for _, v, _ in self._history()), (
            "Oracle 方向口径变了却没追加世代边界"
        )

    def test_entry_opens_a_new_partition(self):
        """本条必须**新开**一个分区：2026-09-10 那 30 条里有 7 条方向会变，不可比。

        判据是「相对**前一条**严格更晚」，不是「本条在队尾」——
        绑队尾会让下一个 session 的任何合法边界把这条改红
        （v0.45.191 的 test_it_extends_the_same_label 正是这么红的，同版已修）。
        """
        h = self._history()
        idx = [i for i, (_, v, _) in enumerate(h) if v == "v0.45.201"]
        assert len(idx) == 1, "v0.45.201 应恰好登记一条"
        i = idx[0]
        assert i > 0 and h[i][0] > h[i - 1][0], (
            f"本条日期 {h[i][0]} 未严格晚于前一条 {h[i-1][0]} —— 那就不是新分区"
        )

    def test_history_is_append_only(self):
        """既有条目不得被改写 —— 审计轨迹。"""
        h = self._history()
        assert len(h) >= 11, f"世代历史条数倒退到 {len(h)}"
        assert h[0][1] == "v0.44.1~0.44.3", "第一条被改写了"
        assert [d for d, _, _ in h] == sorted(d for d, _, _ in h), "日期不是单调的"
