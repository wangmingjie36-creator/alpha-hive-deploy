"""`cboe_options._select_expiries` 的 DTE 口径守卫（v0.45.190）。

**背景。** 这个函数是全部 30 只标的取期权链的唯一闸门 —— IV rank / 25Δ skew /
期限结构 / Dealer GEX 全部吃它的输出 —— 而在 v0.45.190 之前它**一条测试都没有**。

它有一处差一天：`today` 是 `_pdt_now()`（**带时分秒**的 datetime），而
`datetime.strptime(到期日)` 是当天零点，`timedelta.days` 向下取整 ⇒ 只要不在
00:00:00 整跑，每个到期日都少算一天。于是写着 `DTE >= 7` 的闸门实际要求
「≥8 个日历日」。2026-09-11（周五）实测：7 个日历日之外的 09-18 标准周权被判成
DTE=6 而落选，**30/30 只标的的选中集合都因此改变**；只修这一处，
`DealerGEXAnalyzer.total_gex` 有 7/30 只符号翻转、NVDA 量级差 42×。

**本文件不修那个 bug，只把它钉住。** 修它要改 `odds` 维度的输入口径，按
`ic_rerun_readiness._COHORT_HISTORY` 的规则得付一条世代边界；那是单独的决定。
这里要防的是**另一件事**：有人「照字面把它修好」却没登记世代边界 —— 那样新旧
两套口径的分会被静默混算。`test_selection_must_not_depend_on_time_of_day` 就是
为此而设的 strict xfail：今天它是 xfail（绿），**修好的那一刻它会变红**，
并在 reason 里说清下一步该干什么。

**纪律。** 每条断言都在自己的 docstring 里写出「什么变异会让它变红」。
举不出变异的断言不许加（见 auto-memory `alpha-hive-test-writes-production`）。

**本文件零外部依赖** —— 夹具全是内存里造的 dict，没有任何文件/网络/数据库，
故结构上不存在写 `pytest.skip` 的余地（见 `alpha-hive-failure-propagation`：
`skip` 会把「这条没验」渲染成「这条没问题」）。
"""

from datetime import datetime

import pytest

import cboe_options as C
from options_analyzer import OptionsAgent

# 2026-09-11 是**周五**。差一天只在「恰好有到期日落在 3 或 7 个日历日」时才改变
# 选集，而周五那天 7 日历日之外正是下周五的标准周权（全周 OI 最大）——
# 实测周五 30/30、周一 6/30、周三 5/30、周二与周四 0/30。故夹具锚在周五。
FRIDAY_1330 = datetime(2026, 9, 11, 13, 30)   # 编排器时间闸就在这个钟点
FRIDAY_MIDNIGHT = datetime(2026, 9, 11, 0, 0)
EXP_7_CALENDAR_DAYS = "2026-09-18"            # 正好 7 个日历日之外

# 照抄 NVDA 2026-09-11 的真实到期日清单
NVDA_EXPIRIES = ["2026-09-14", "2026-09-16", "2026-09-18", "2026-09-21",
                 "2026-09-23", "2026-09-25", "2026-10-02", "2026-10-09"]


def _recorder(sink):
    """替身 logger：把 `.warning(...)` 的实参收进 sink。

    ⚠️ 不要改用「造一个 `logging.Handler` 再检查它收到了什么」——
    本文件初版就是那么写的，而 handler 从没挂到任何 logger 上 ⇒ sink 恒空 ⇒
    `assert not sink` 恒真。变异校验（M8「warning 改成恒亮」）当场抓到它没牙。
    """
    return type("L", (), {
        "warning": lambda self, *a: sink.append(a),
        "info": lambda self, *a: None,
        "debug": lambda self, *a: None,
        "error": lambda self, *a: None,
    })()


def _by_expiry(expiries, oi_per_side=100.0):
    """最小 by_expiry 夹具：每个到期日每边一份合约。"""
    return {e: {"C": [{"strike": 100.0, "openInterest": oi_per_side}],
                "P": [{"strike": 100.0, "openInterest": oi_per_side}]}
            for e in expiries}


@pytest.fixture(autouse=True)
def _reset_stats():
    C.reset_chain_selection_stats()
    yield
    C.reset_chain_selection_stats()


# ───────────────────────────── 选集规则（有牙的守卫）

def test_far_fills_quota_so_near_never_enters():
    """`far` 凑满 4 个时 `near` 一个都进不来（v0.45.188 的实测结论，此处钉死）。

    变红的变异：把 `chosen = (far + near)[:max_expiries]` 改成
    `(near + far)[:max_expiries]`，或把 `far` 的 `[:4]` 改小。
    ⚠️ **修掉差一天也会让它变红**（09-18 会进 far ⇒ 不再 disjoint）——
    实测如此。真到那一天，本条和 `test_dte_lags_calendar_by_one_day` 要一起改写，
    且先去 `ic_rerun_readiness._COHORT_HISTORY` 登记世代边界。
    """
    chosen, _ = C._select_expiries(_by_expiry(NVDA_EXPIRIES), FRIDAY_1330, 4)
    assert len(chosen) == 4
    # 代码口径 DTE<7 的（09-14/09-16/09-18）一个都不在
    assert set(chosen).isdisjoint({"2026-09-14", "2026-09-16", "2026-09-18"})


def test_near_enters_when_far_is_short():
    """`far` 不足 4 个时 `near` 确实会被用到 —— 它不是死代码，是对主力名单恒不生效的代码。

    变红的变异：删掉 `near` 那一行，或把 `chosen` 改成只取 `far`。
    """
    sparse = ["2026-09-16", "2026-09-25"]     # 代码口径 DTE = 4 / 13
    chosen, _ = C._select_expiries(_by_expiry(sparse), FRIDAY_1330, 4)
    assert "2026-09-16" in chosen, "far 只有 1 个时 near 必须补进来"


def test_dte_below_three_never_selected():
    """代码口径 DTE<3 一律排除，任何配额下都拿不到。

    变红的变异：把 `if dte >= 3` 改成 `>= 0`，或删掉该判断。
    """
    only_near = ["2026-09-12", "2026-09-13"]  # 代码口径 DTE = 0 / 1
    chosen, _ = C._select_expiries(_by_expiry(only_near), FRIDAY_1330, 4)
    assert chosen == [], "DTE<3 的到期日不该被选中，哪怕没有别的候选"


def test_near_expiry_set_is_disjoint_from_chosen():
    """`near_expiry_set` 与 `chosen` 不相交 —— 下游「排除近端合约」因此是空操作。

    这条不是在夸奖现状，是把 `options_analyzer._calc_total_oi` 恒等于全量求和
    这件事钉在测试里，免得下次有人读那段代码时以为「稳定口径」真的生效了。

    变红的变异：让 `near` 真的进 `chosen`（即修掉配额顺序），
    或把 `near_set` 的条件从 `d < 7` 改成别的。
    """
    chosen, near_set = C._select_expiries(_by_expiry(NVDA_EXPIRIES), FRIDAY_1330, 4)
    assert near_set, "夹具里本来就有 DTE<7 的到期日，near_set 不该为空"
    assert set(near_set).isdisjoint(set(chosen))


# ───────────────────────────── 差一天：characterization + 防静默修复

def test_dte_lags_calendar_by_one_day():
    """**characterization**：代码算出的 DTE 比日历天数少 1。

    这条断言的是**现状不是应然**。之所以写出来，是因为「差一天」此前完全不可见：
    `_select_expiries` 零测试覆盖，而两份 docstring（含 v0.45.188 按实测重写的
    那份）都把它描述成正确的日历 DTE。

    变红的变异：把 `today` 换成 `today.date()` 口径（即修掉这个 bug）。
    真到那一天，**先去 `ic_rerun_readiness._COHORT_HISTORY` 登记世代边界**，
    再把本条与下一条一起改写。
    """
    # 夹具自检（只验夹具，不验被测代码）：这个到期日确实正好 7 个日历日之外。
    assert (datetime.strptime(EXP_7_CALENDAR_DAYS, "%Y-%m-%d").date()
            - FRIDAY_1330.date()).days == 7

    # 以下两条都**经过被测代码**：`near_expiry_set` 收的是代码口径 DTE<7 的到期日，
    # 所以「7 个日历日之外的那一个落在 near_set 里」就是差一天的直接证据。
    chosen, near_set = C._select_expiries(_by_expiry(NVDA_EXPIRIES), FRIDAY_1330, 4)
    assert EXP_7_CALENDAR_DAYS in near_set, (
        "7 个日历日之外的到期日当前被判成 DTE<7 —— 这就是那一天之差")
    assert EXP_7_CALENDAR_DAYS not in chosen, (
        "于是它被 `DTE>=7` 的 far 桶挡在外面")


@pytest.mark.xfail(
    strict=True,
    reason="已知差一天（v0.45.190 记录）。这条一旦 XPASS 说明口径被修好了 —— "
           "请先在 ic_rerun_readiness._COHORT_HISTORY 登记世代边界，"
           "再删掉本 xfail 标记并改写 test_dte_lags_calendar_by_one_day。",
)
def test_selection_must_not_depend_on_time_of_day():
    """选到期日是**日期**运算，同一天的哪个钟点跑都该给同一个答案。

    这是「差一天」的机制性表述：`today` 带时分秒 ⇒ 00:00 跑和 13:30 跑结果不同。
    编排器 13:30 跑，所以生产上恒定拿到少一天的那个答案。

    `strict=True` 的用意：今天它 xfail（绿）。**修好的那一刻它 XPASS ⇒ 变红**，
    强制修复者来读 reason 里那句「先登记世代边界」。这正是本仓需要的那种红 ——
    危险的从来不是「没人修」，是「有人静默修了、新旧口径的分被混算」。
    """
    at_midnight, _ = C._select_expiries(_by_expiry(NVDA_EXPIRIES), FRIDAY_MIDNIGHT, 4)
    at_1330, _ = C._select_expiries(_by_expiry(NVDA_EXPIRIES), FRIDAY_1330, 4)
    assert at_midnight == at_1330


# ───────────────────────────── 观测点自身

def test_observer_uses_calendar_dte_not_the_buggy_one():
    """观测点必须用**日历**口径，否则它自己也差一天 = 复制了它要观测的 bug。

    变红的变异：把 `_record_chain_selection` 里的 `.date()` 去掉
    （即让观测点跟 `_select_expiries` 用同一套口径）—— 那样 min_cal 会变成 6。
    """
    obs = C._record_chain_selection(
        _by_expiry(NVDA_EXPIRIES), [EXP_7_CALENDAR_DAYS], FRIDAY_1330)
    assert obs is not None
    min_cal, near_n, near_oi, chosen_oi = obs
    assert min_cal == 7, "选中 09-18 时，观测点报的应是日历 DTE 7，不是代码口径的 6"
    # 09-14 / 09-16 落在 [0,7) 且未被选中 → 计 2 个
    assert near_n == 2
    assert near_oi == pytest.approx(400.0)    # 2 个到期日 × 两边 × 100
    assert chosen_oi == pytest.approx(200.0)
    assert C.chain_selection_stats()["min_cal_dte_max"] == 7


def test_observer_failure_is_counted_not_swallowed():
    """观测统计自己失败时必须计数并返回 None —— 不能让它表现为「一个都没挡掉」。

    没有 `errors` 这个计数，「统计崩了」和「真的 0 个被挡掉」在 status.json 里
    长得一模一样，那正是本仓 MEMORY 记了六次的「失败没传导到下游」。

    变红的变异：删掉 except 分支里的 `_chain_stats["errors"] += 1`，
    或把 `return None` 改成返回一个零元组。
    """
    broken = {"2026-09-18": {"C": [{"openInterest": "不是数"}], "P": []}}
    obs = C._record_chain_selection(broken, ["2026-09-18"], FRIDAY_1330)
    assert obs is None, "算不出来要返回 None，不能返回看起来正常的零值"
    assert C.chain_selection_stats()["errors"] == 1
    assert C.chain_selection_stats()["chains"] == 0, "失败的那次不该计进分母"


def test_calc_total_oi_warns_when_exclusion_is_a_noop(monkeypatch):
    """`near_expiry_set` 与链内到期日不相交时要打 warning，并且返回值不变。

    行为**故意不改** —— 改了就是口径变更，要付世代边界。这里只让「意图从未生效」
    这件事有个落点。

    变红的变异：删掉那段 warning；或者「顺手把它修好」让 stable_oi 真的少算
    （返回值断言会红）。
    """
    warnings = []
    monkeypatch.setattr("options_analyzer._log", _recorder(warnings))
    monkeypatch.setattr("options_analyzer._TOTAL_OI_NOOP_WARNED", False)
    calls = [{"expiry": "2026-09-21", "openInterest": 10}]
    puts = [{"expiry": "2026-09-21", "openInterest": 5}]
    chain = {"near_expiry_set": ["2026-09-16", "2026-09-18"]}

    total = OptionsAgent._calc_total_oi(calls, puts, chain)

    assert total == 15, "行为必须逐字节不变：排除不到东西 ⇒ 等于全量求和"
    assert warnings, "不相交是「意图从未生效」，必须留下痕迹"
    assert "空操作" in warnings[0][0]


def test_calc_total_oi_stays_silent_when_exclusion_really_applies(monkeypatch):
    """真的排除掉了东西时不该打 warning —— 上一条不能是恒真的。

    变红的变异：把上一条的 warning 条件改成无条件打印。
    这一条是上一条的**配对断言**：只证明「该响时响」不够，还要证明「不该响时不响」，
    否则那个观测点等于一个恒亮的灯（见 `alpha-hive-board-eviction`：成对断言是刚需）。
    """
    warnings = []
    monkeypatch.setattr("options_analyzer._log", _recorder(warnings))
    # ⚠️ 必须先清掉一次性标志，否则「沉默」可能只是去重的副作用，
    #    这条断言就会在**不该绿的时候也绿** —— 那就又是一条没牙的断言。
    monkeypatch.setattr("options_analyzer._TOTAL_OI_NOOP_WARNED", False)
    calls = [{"expiry": "2026-09-16", "openInterest": 7},
             {"expiry": "2026-09-21", "openInterest": 10}]
    puts = [{"expiry": "2026-09-21", "openInterest": 5}]
    chain = {"near_expiry_set": ["2026-09-16"]}

    total = OptionsAgent._calc_total_oi(calls, puts, chain)
    assert total == 15, "09-16 的 7 张应被排除掉"
    assert not warnings, f"真的排除掉东西时不该打 warning，却打了：{warnings}"


def test_calc_total_oi_noop_warning_fires_once_per_process(monkeypatch):
    """空操作 warning 进程内只打一次 —— 它在生产上结构性恒为真（30/30 每轮全命中），
    逐次打印就是一盏恒亮的灯，和不打一个效果。

    变红的变异：删掉 `_TOTAL_OI_NOOP_WARNED = True` 那一行（第二次也会打）；
    或把 `and not _TOTAL_OI_NOOP_WARNED` 从条件里去掉。
    """
    warnings = []
    monkeypatch.setattr("options_analyzer._log", _recorder(warnings))
    monkeypatch.setattr("options_analyzer._TOTAL_OI_NOOP_WARNED", False)
    calls = [{"expiry": "2026-09-21", "openInterest": 10}]
    puts = [{"expiry": "2026-09-21", "openInterest": 5}]
    chain = {"near_expiry_set": ["2026-09-16", "2026-09-18"]}

    OptionsAgent._calc_total_oi(calls, puts, chain)
    OptionsAgent._calc_total_oi(calls, puts, chain)
    OptionsAgent._calc_total_oi(calls, puts, chain)
    assert len(warnings) == 1, f"三次调用只该留一条 warning，实得 {len(warnings)} 条"
