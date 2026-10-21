"""
经济日历的**冻结形状**（v0.45.572）——给「验逻辑」的测试用，不给「验数据」的测试用。

`economic_calendar` 的表每年要抄一次新日程。`test_economic_calendar.py` 与
`test_economic_calendar_watch.py` 里大量场景（「2026-09-20 这天 CPI / NFP 已 stale」
「2027-06-01 三张表已走完」「上游有 2027、我们只核到 2026-12」）是照**真实表尾 / verified_through**
挑的日期——每抄进一年，这些场景就整体失效。v0.45.572 抄 2027 时 7 条一起红，红的全不是逻辑。

所以验逻辑的测试钉在这里：真实表截到抄录 2027 之前的表尾，`verified_through` 同步回退到当时的值。
以后再抄新年份，它们不受影响；**真实表够不够长、抄得对不对，只由不用本夹具的测试守**
（`TestCoverageHorizon` 用真实 today、`TestDateProvenance` / `TestPublishedDates` 看真实表）。
"""

from datetime import date

# 抄录 2027 之前（2026-10-21）四张表的表尾；当时各表 verified_through 与表尾相同
FROZEN_TABLE_ENDS = {"fomc": "2028-01-26", "cpi": "2026-12-10",
                     "nfp": "2026-12-04", "gdp": "2026-10-29"}
_TABLE_GLOBALS = {"fomc": "_FOMC", "cpi": "_CPI", "nfp": "_NFP", "gdp": "_GDP"}


def freeze_calendar(monkeypatch):
    """把 `economic_calendar` 钉成冻结形状，并正面核对夹具真的接上了。"""
    import economic_calendar as ec
    for key, end in FROZEN_TABLE_ENDS.items():
        frozen = [d for d in ec._TABLE_SPECS[key]["dates"] if d <= end]
        assert frozen and frozen[-1] == end, (
            f"{key} 真实表里已没有冻结表尾 {end}（被改值或删除？）——场景前提变了，重新挑冻结形状")
        # 两条读路径各打一处：get_upcoming_events 读模块级列表，get_calendar_health 读 _TABLE_SPECS
        monkeypatch.setattr(ec, _TABLE_GLOBALS[key], frozen)
        monkeypatch.setitem(ec._TABLE_SPECS[key], "dates", frozen)
        monkeypatch.setitem(ec._TABLE_SPECS[key], "verified_through", end)

    # 正面核对两条路径都看见冻结形状（只打一条时两者看见不同的日历，测试照样可能绿）
    ref = date(2026, 10, 1)
    h = ec.get_calendar_health(ref_date=ref)
    assert {k: t["last_date"] for k, t in h["per_table"].items()} == FROZEN_TABLE_ENDS
    assert {k: t["verified_through"] for k, t in h["per_table"].items()} == FROZEN_TABLE_ENDS
    seen = {e["type"]: e["date"] for e in ec.get_upcoming_events(days=1000, ref_date=ref)}
    assert {k: seen[k] for k in FROZEN_TABLE_ENDS} == FROZEN_TABLE_ENDS
    # 上面的自检会触发「一个状态只吼一次」的节流——不清掉，测试要等的那声 WARNING 会被它先吃掉
    ec._reset_warning_throttle()
