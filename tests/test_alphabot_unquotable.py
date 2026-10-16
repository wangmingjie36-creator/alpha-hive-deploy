"""Alpha Bot「不可报价」原因讲清楚（v0.45.519）的守卫。

每组回答一句「谁会红」：
  · 原因码双向对账：引擎写出的每个原因码前缀都登记在前端 `REASON_LABEL`，表里每个键也真有人写
    （漏登记 ⇒ 页面显示「未登记：…」而没人发现；多登记 ⇒ 表里躺着死键）（`TestReasonTableIsComplete`）；
  · 数据层：`_side_brief` 新增的 ask / mid / oi 就是梯子里同一条腿的值，既有键一个不少（`TestSideBrief`）；
  · 前端接线：总览 / 梯子 / 结构表 / 怎么卖都经 `reasonInfo` 显示原因，不再原样吐代码；点差上限取自服务端，
    不写字面量（`TestFrontendWiring`）。
全部离线（合成链注入 `fetch_fn`）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import sell_strike_candidates as C
import sell_strike_ledger as LG
import sell_strike_levels as L
import sell_strike_report as R
from alphabot import synthetic as syn

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "alphabot" / "static"
AS_OF = "2026-09-01"


def _engine_reason_codes() -> set:
    """从引擎与 howto 源码里静态扫出所有写进 reason 的码前缀（冒号前）。"""
    codes = set()
    src = (REPO / "sell_strike_candidates.py").read_text(encoding="utf-8")
    # reasons.append("x") / reasons.append(f"x:{...}") / _empty_quote(..., ["x"])
    for m in re.finditer(r'reasons\.append\(f?"([a-z_]+)[:"]', src):
        codes.add(m.group(1))
    for m in re.finditer(r'_empty_quote\([^)]*\[\s*"([a-z_]+)"', src):
        codes.add(m.group(1))
    howto = (REPO / "alphabot" / "howto.py").read_text(encoding="utf-8")
    for m in re.finditer(r'"reason":\s*"([a-z_]+)"', howto):
        codes.add(m.group(1))
    return codes


def _label_keys() -> set:
    txt = (STATIC / "lib.js").read_text(encoding="utf-8")
    body = re.search(r"export const REASON_LABEL = \{(.*?)\};", txt, re.S).group(1)
    return set(re.findall(r"^\s*([a-z_]+):", body, re.M))


class TestReasonTableIsComplete:
    def test_every_engine_code_is_labelled(self):
        codes = _engine_reason_codes()
        assert len(codes) >= 6, f"扫到的原因码太少——解析规则坏了，这条测试在空转：{codes}"
        assert not codes - _label_keys(), f"引擎写出但前端表里没有：{sorted(codes - _label_keys())}"

    def test_no_dead_label_keys(self):
        assert not _label_keys() - _engine_reason_codes(), sorted(_label_keys() - _engine_reason_codes())

    def test_scan_has_teeth(self):
        """扫描器对一个新加的原因码要能看见（否则上面两条永远绿）。"""
        fake = 'reasons.append(f"brand_new_reason:{side}")'
        assert re.search(r'reasons\.append\(f?"([a-z_]+)[:"]', fake).group(1) == "brand_new_reason"
        assert "brand_new_reason" not in _label_keys()

    def test_short_spread_is_the_warn_tone(self):
        txt = (STATIC / "lib.js").read_text(encoding="utf-8")
        assert re.search(r"REASON_TONE = \{ short_spread_too_wide: \"warn\" \}", txt)


class TestSideBrief:
    @pytest.fixture(scope="class")
    def row(self):
        raw = syn.demo_payload("NVDA", AS_OF)
        lm = L.level_map(raw["contracts"], raw["underlying_price"])
        return LG.build_tenor_row(raw, lm, "monthly", as_of=AS_OF, earnings_info={"earnings_date": None}, ticker="NVDA")

    def test_new_keys_are_the_ladder_leg_values(self, row):
        brief = R.row_brief(row, blind=True)
        seen = 0
        for label, rungs in (("route_legs", R.route_rungs(row)), ("base_legs", {s: C.BASE_RUNG for s in LG.SIDES})):
            for side in LG.SIDES:
                b = brief[label][side]
                if b is None or b.get("strike") is None:
                    continue
                leg = row["ladder"][side][C.rung_key(rungs[side])]["short"]
                assert (b["ask"], b["mid"], b["oi"]) == (leg["ask"], leg["mid"], leg["oi"]), (label, side)
                assert b["oi"] is not None
                seen += 1
        assert seen >= 3

    def test_existing_keys_are_untouched(self, row):
        keys = set(R.row_brief(row, blind=True)["route_legs"]["put"])
        assert {"rung", "strike", "delta", "itm_prob", "bid", "spread_pct", "quotable", "reason",
                "yield_raw", "yield_annualized"} <= keys

    def test_overview_endpoint_carries_them(self):
        from alphabot.server import create_app
        from alphabot.service import AlphaBotService
        from starlette.testclient import TestClient
        svc = AlphaBotService(fetch_fn=syn.demo_fetch, demo=True,
                              bars_fn=lambda t: {"available": True, "source": "demo", "bars": syn.demo_bars(t, AS_OF)})
        c = TestClient(create_app(svc, port=8765), base_url="http://127.0.0.1:8765")
        rows = c.get("/api/overview").json()["tenors"]["monthly"]
        legs = [r["route_legs"][s] for r in rows if r.get("route_legs") for s in ("put", "call")
                if r["route_legs"][s] and r["route_legs"][s].get("strike") is not None]
        assert legs and all("oi" in x and "ask" in x for x in legs)


class TestFrontendWiring:
    def _txt(self, name):
        return (STATIC / "pages" / name).read_text(encoding="utf-8")

    def test_pages_show_labels_not_raw_codes(self):
        for name in ("overview.js", "ticker.js", "howto.js"):
            t = self._txt(name)
            assert "reasonInfo" in t, name
            assert "short_spread_too_wide" not in t or name == "overview.js", name
        # 总览只许用它判分类（code 比较），不许把原始 reason 字符串直接拼进文案
        ov = self._txt("overview.js")
        assert "`不可报价：${leg.reason" not in ov
        tk = self._txt("ticker.js")
        assert "(q.reason || \"—\").split" not in tk and "不可报价：${q.reason" not in tk

    def test_spread_cap_comes_from_the_server(self):
        ov = self._txt("overview.js")
        assert "methodRules" in ov and "max_spread_pct" in ov
        assert not re.search(r"上限\s*\d+\s*%", ov), "点差上限写成了字面量"

    def test_help_does_not_hardcode_the_cap(self):
        txt = self._txt("help.js")
        assert "不可报价正常吗" in txt
        assert not re.search(r"点差(超过|上限)[^。]{0,6}\d+\s*%", txt)
