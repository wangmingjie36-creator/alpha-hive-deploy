"""近端 Max Pain 的口径守卫（v0.45.188）。

**背景。** v0.45.188 之前，「近端磁吸目标价」取的是 `fetch_cboe_chain` 返回链里
最早的那个到期日 —— 而那条链经 `cboe_options._select_expiries` 按设计排除 DTE<7
（周权标的 far 恒占满配额），于是「最近」实际是**最早的 DTE≥7 那一个**，与
「近端」二字相反。2026-09-10 实测全 30 只标的（29 只有数据）**28 只**与真·≤7天
口径不符，最大偏 20%；NVDA 显示值 09-08→09-10 是 225→200，而 ≤7 天真值两天都是
225 —— 那次跳变全部是「换了一个到期日」造成的假象。

**纪律。** 本文件每条断言都在自己的 docstring 里写出「什么变异会让它变红」。
举不出变异的断言不许加（见 auto-memory `alpha-hive-test-writes-production`：
对方栽在 `assert id(x) 调用前后相等` 这种 Python 里根本不可能变红的断言上）。
"""

from datetime import date, datetime, timedelta

import pytest

from pheromone_board import PheromoneBoard
from swarm_agents.oracle_bee import OracleBeeEcho

TODAY = date(2026, 9, 10)
STOCK_PRICE = 218.36

# 夹具照抄 NVDA 2026-09-10 的形状（数量级缩小，比例保留）：
#   近端三个到期日（DTE 1 / 4 / 6）的 OI 压在 225；
#   最早的 DTE≥7 到期日（09-18，DTE 8）的 OI 压在 200。
# 于是「≤7 天聚合」与「最早的 DTE≥7 那一个」必然给出不同答案 —— 这正是夹具
# 存在的意义，`test_fixture_discriminates` 会把这一点钉死。
NEAR_EXPIRIES = ("2026-09-11", "2026-09-14", "2026-09-16")   # DTE 1 / 4 / 6
FAR_EXPIRY = "2026-09-18"                                     # DTE 8
# 已过期到期日，OI 压在 210。它存在的唯一理由是让「窗口下界」那条守卫**有牙**：
# 没有它，把 `0 <= dte` 去掉的变异一条测试都红不了（实测确认过）。
EXPIRED_EXPIRY = "2026-09-04"                                 # DTE -6


def _chain() -> dict:
    def _leg(near_strike: float, far_strike: float) -> dict:
        out: dict = {}
        for s in (200.0, 210.0, 220.0, 225.0, 230.0):
            per_exp = {}
            for e in NEAR_EXPIRIES:
                per_exp[e] = 40000 if s == near_strike else 800
            per_exp[FAR_EXPIRY] = 90000 if s == far_strike else 700
            per_exp[EXPIRED_EXPIRY] = 200000 if s == 210.0 else 600
            out[str(s)] = per_exp
        return out

    return {
        "data_available": True,
        "call_exp_oi": _leg(near_strike=225.0, far_strike=200.0),
        "put_exp_oi": _leg(near_strike=225.0, far_strike=200.0),
    }


def _bee() -> OracleBeeEcho:
    return OracleBeeEcho(PheromoneBoard())


def _max_pain_of(expiries) -> float:
    """按「只取这些到期日」算 Max Pain —— 用来复现旧口径做对照。"""
    keep = set(expiries)
    chain = _chain()
    call_oi, put_oi = {}, {}
    for src, dst in ((chain["call_exp_oi"], call_oi), (chain["put_exp_oi"], put_oi)):
        for strike_s, per_exp in src.items():
            total = sum(oi for e, oi in per_exp.items() if e in keep)
            if total:
                dst[float(strike_s)] = total
    return OracleBeeEcho._max_pain_from_oi(call_oi, put_oi, STOCK_PRICE)


@pytest.fixture
def frozen_today(monkeypatch):
    """把 `pdt_today` 钉在 2026-09-10。

    ⚠️ 夹具值故意不等于真实今天 —— 否则「接上了」和「没接上」长得一模一样
    （auto-memory `alpha-hive-adapted-weights-bypass` 的 v0.45.177 自查条）。
    """
    monkeypatch.setattr("swarm_agents.oracle_bee.pdt_today", lambda: TODAY.isoformat())
    return TODAY


class TestFixtureItself:
    def test_fixture_discriminates(self):
        """夹具自证：新旧两种口径在这份数据上必须给出**不同**的答案。

        变红条件：有人把夹具改成两种口径答案相同（比如把 far 那条腿的 OI 也压在
        225）—— 那样下面所有口径断言都会变成恒真，全绿却什么都没验。
        """
        near_answer = _max_pain_of(NEAR_EXPIRIES)
        old_answer = _max_pain_of([FAR_EXPIRY])
        leaked_answer = _max_pain_of(list(NEAR_EXPIRIES) + [EXPIRED_EXPIRY])
        assert near_answer == 225.0, f"夹具坏了：≤7天口径应为 225，实得 {near_answer}"
        assert old_answer == 200.0, f"夹具坏了：旧口径应为 200，实得 {old_answer}"
        assert near_answer != old_answer
        # 下界那条守卫的牙：混进已过期到期日必须改变答案，否则它守了个寂寞
        assert leaked_answer != near_answer, (
            f"夹具坏了：混进已过期到期日后答案没变（{leaked_answer}）"
            "，`0 <= dte` 那条守卫将无法变红")


class TestWindowSemantics:
    def test_uses_near_window_not_earliest_far(self, frozen_today):
        """核心回归：必须用 ≤7 天聚合，不能退回「最早的 DTE≥7 那一个」。

        变红条件：把 `_calc_max_pain` 改回从 `fetch_cboe_chain` 取 `_exps[0]`
        （即 v0.45.188 之前的实现）⇒ 得到 200.0。
        """
        r = _bee()._calc_max_pain("TEST", STOCK_PRICE, {"full_chain_oi": _chain()})
        assert r["max_pain"] == 225.0, f"应为 ≤7 天口径的 225，实得 {r['max_pain']}"
        assert r["window_days"] == 7

    def test_expiries_used_all_inside_window(self, frozen_today):
        """不变式：报出来的每个到期日都必须落在 [0, window_days] 内。

        这条直接断言**被消费的那个 dict**（`max_pain` 结果本身流进
        `_pub_details` 与展示层），不是它的上游 —— 探针放上游等于没放
        （auto-memory `alpha-hive-adapted-weights-bypass` v0.45.177）。

        变红条件：`_near_oi_by_strike` 的窗口判据去掉 `0 <=`（放进已过期到期日），
        或把上界放宽到 30 天（09-18 会混进来）。
        """
        r = _bee()._calc_max_pain("TEST", STOCK_PRICE, {"full_chain_oi": _chain()})
        assert r["expiries_used"], "窗口内应当有到期日"
        for e in r["expiries_used"]:
            dte = (datetime.strptime(e, "%Y-%m-%d").date() - frozen_today).days
            assert 0 <= dte <= r["window_days"], f"{e} 的 DTE={dte} 越出窗口"
        assert FAR_EXPIRY not in r["expiries_used"]
        assert EXPIRED_EXPIRY not in r["expiries_used"]

    @pytest.mark.parametrize("dte,should_count", [(0, True), (7, True), (8, False)])
    def test_window_boundary_is_inclusive(self, frozen_today, dte, should_count):
        """边界：DTE=0 与 DTE=7 都算近端，DTE=8 不算。

        变红条件：把 `0 <= dte <= window_days` 改成 `0 < dte`（DTE=0 掉出）
        或 `dte < window_days`（DTE=7 掉出）。
        """
        exp = (frozen_today + timedelta(days=dte)).isoformat()
        chain = {"data_available": True,
                 "call_exp_oi": {"220.0": {exp: 5000}},
                 "put_exp_oi": {"220.0": {exp: 5000}}}
        _c, _p, used, _bad = OracleBeeEcho._near_oi_by_strike(
            chain, frozen_today, OracleBeeEcho.NEAR_WINDOW_DAYS)
        assert (exp in used) is should_count


class TestNoSilentFallback:
    def test_does_not_fall_back_to_truncated_chain(self, monkeypatch, frozen_today):
        """`full_chain_oi` 缺失时必须返回 None，**不许**回退旧的截断链。

        旧链给的是构造上就不符合「近端」含义的数，回退等于把「没数据」悄悄换成
        「错数据」—— 本仓头号元形状（auto-memory `alpha-hive-failure-propagation`）。

        变红条件：有人在 `_calc_max_pain` 里加一条 `fetch_cboe_chain` 兜底
        ⇒ 探针被调用 ⇒ 断言失败。
        """
        import cboe_options
        called = []
        monkeypatch.setattr(cboe_options, "fetch_cboe_chain",
                            lambda *a, **k: called.append(a) or None)

        r = _bee()._calc_max_pain("TEST", STOCK_PRICE, {"full_chain_oi": None})

        assert r["max_pain"] is None
        assert r["summary"] == ""
        assert not called, "不应回退到 fetch_cboe_chain（那条链按设计排除 DTE<7）"

    def test_unavailable_carries_reason(self, frozen_today):
        """算不出时必须说明原因 —— 「谁会红？」在展示层与日志里都要有落点。

        变红条件：删掉 `unavailable_reason` 的赋值，或把它改成空串。
        """
        r = _bee()._calc_max_pain("TEST", STOCK_PRICE, {})
        assert r["max_pain"] is None
        assert r["unavailable_reason"], "不可用时必须带原因"

    def test_pub_details_does_not_carry_dead_kaijing_fields(self):
        """口径字段不许往信息素板塞 —— 板上的 `max_pain*` 零读者且板有 80 条上限。

        审计轨迹走 `AgentResult.details` 里的完整 max_pain dict（那份才落进
        `.swarm_results_*.json` 并被看板/报告读到）。

        变红条件：把 `_pub_details["max_pain_window_days"] = ...` 之类加回去。
        """
        import inspect

        src = inspect.getsource(OracleBeeEcho.analyze)
        for dead in ("max_pain_window_days", "max_pain_expiries", "max_pain_unavailable"):
            assert f'_pub_details["{dead}"]' not in src, (
                f"{dead} 被塞进信息素板，但板上 max_pain* 没有任何读者")

    def test_empty_window_is_reported_not_faked(self, frozen_today):
        """窗口内没有到期日时报 None + 原因，而不是拿远月凑一个数。

        变红条件：把「窗口内无到期日」这条分支改成退回全链聚合 ⇒ 会算出一个值。
        """
        chain = {"data_available": True,
                 "call_exp_oi": {"220.0": {"2026-12-18": 50000}},
                 "put_exp_oi": {"220.0": {"2026-12-18": 50000}}}
        r = _bee()._calc_max_pain("TEST", STOCK_PRICE, {"full_chain_oi": chain})
        assert r["max_pain"] is None
        assert "无到期日" in r["unavailable_reason"]


class TestDashboardLabelSameSource:
    """看板卡片的「到期日」标签必须与算出那个数字的到期日同源。

    换源前两者恰好同源（都取自被截断的主链，一起错）；换源后若标签仍读
    `expiration_dates`，就会出现「$225」旁边标着 09-18/09-21/09-23 —— 而那三个
    到期日一张合约都没参与这个数。同 auto-memory `alpha-hive-prediction-retention`：
    **同量在同页出现两次就迟早是两个数 —— 要同源，不要「保持一致」。**
    """

    MP_EXPIRIES = ["2026-09-11", "2026-09-14", "2026-09-16"]
    MAIN_CHAIN = ["2026-09-18", "2026-09-21", "2026-09-23"]

    def _row(self):
        import dashboard_renderer as D
        detail = {
            "max_pain": {"max_pain": 225.0, "distance_pct": -2.95,
                         "window_days": 7, "expiries_used": self.MP_EXPIRIES},
            "expiration_dates": self.MAIN_CHAIN,
            "full_chain_oi": {"data_available": True},
        }
        # ScoutBee 的 price 必须给 —— 缺了 `_detail` 会退回打 yfinance 取价，
        # 被 conftest 的离线守卫拦下（这条守卫是对的：测试不许伸手取外网）。
        sd = {"TEST": {"agent_details": {
            "OracleBeeEcho": {"details": detail},
            "ScoutBeeNova": {"details": {"price": 218.36}},
        }}}
        return D._detail("TEST", sd)

    def test_label_expiries_come_from_max_pain_not_main_chain(self):
        """变红条件：把 `near_mp_expiries` 改回读 `expiration_dates`。"""
        row = self._row()
        assert row["near_mp_expiries"] == self.MP_EXPIRIES
        assert row["near_mp_expiries"] != self.MAIN_CHAIN
        assert row["near_mp_window"] == 7

    def test_main_chain_list_still_available_separately(self):
        """主链列表本身保留（它是另一个量），只是不再用于磁吸标签。

        变红条件：删掉 `near_expiry_dates` 这个键。
        """
        assert self._row()["near_expiry_dates"] == self.MAIN_CHAIN
