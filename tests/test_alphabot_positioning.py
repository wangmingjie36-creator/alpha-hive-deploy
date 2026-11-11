"""深度页「做市商与大额成交」（v0.45.771）：`options_compare.dealer_view` / `big_trades`。

两半都是**推算**，不是观测：做市商一半以「净多 Call、净空 Put」为前提；大额成交一半只按规模定义，看不到身份与买卖方向。
测试钉的是：前提与限制原文在输出里、算术对、取不到不编、隔日验证按「这次未平仓 − 上次未平仓」对上次的成交量。全部离线。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from alphabot import deep_report as DR
from alphabot import options_compare as OC

REPO = Path(__file__).resolve().parent.parent
BANNED = ("机构买", "机构卖", "机构看", "看涨", "看跌", "看多", "看空", "主力", "聪明钱", "鲸鱼", "押注", "建议", "适合")


def _gex(**kw):
    base = {"available": True, "reason": None, "regime": "negative_gex", "total_gex": -243.0461, "gex_flip": 165.0,
            "largest_call_wall": 200.0, "largest_put_wall": 170.0, "stock_price": 168.89, "n_expiries": 24,
            "chain_view": "cboe_full_expiries"}
    return base | kw


def _flow(typ, strike, expiry, vol, prem, oi=1000, ratio=None):
    return {"type": typ, "strike": strike, "expiry": expiry, "volume": vol, "dollar_premium": prem, "oi": oi,
            "ratio": ratio if ratio is not None else round(vol / oi, 2), "otm_pct": 1.0, "days_to_exp": 9, "bullish": "call" in typ}


def _res(gex=None, flows=None, call_oi=None, put_oi=None, cal=None):
    return {"gex_state": gex if gex is not None else _gex(),
            "agent_details": {"OracleBeeEcho": {"details": {
                "unusual_activity": flows or [],
                "full_chain_oi": {"call_exp_oi": call_oi or {}, "put_exp_oi": put_oi or {}},
                "gamma_calendar": {"expiry_oi": cal or []}}}}}


class TestDealer:
    def test_negative_gamma_direction_and_size(self):
        d = OC.dealer_view(_res())
        assert d["available"] and d["regime"] == "negative_gex"
        # 净 GEX（百万美元）÷ 股价 = 每 1 美元要交易的股数；× 股价 × 1% = 每 1% 的金额
        assert d["shares_per_dollar"] == round(243.0461e6 / 168.89)
        assert d["usd_per_pct"] == round(243.0461e6 * 168.89 * 0.01)
        assert d["flip_dist_pct"] == -2.3 and d["call_wall_dist_pct"] == 18.42 and d["put_wall_dist_pct"] == 0.66
        text = " ".join(d["facts"])
        assert "上涨时做市商要买入股票、下跌时要卖出" in text and "放大波动" in text
        assert "约 144 万股" in text and "每变动 1% 约 410 百万美元" in text
        assert "翻转位 165" in text and "下方 2.3%，离得近" in text

    def test_positive_gamma_reads_the_other_way(self):
        text = " ".join(OC.dealer_view(_res(_gex(regime="positive_gex", total_gex=120.0)))["facts"])
        assert "上涨时做市商要卖出股票、下跌时要买入" in text and "压住波动" in text and "放大波动" not in text

    def test_assumption_is_always_stated_and_never_presented_as_observed(self):
        for r in (_res(), _res({"available": False, "reason": "chain_unavailable"}), {}):
            d = OC.dealer_view(r)
            assert d["assumption"] == OC.DEALER_ASSUMPTION
        assert "假设" in OC.DEALER_ASSUMPTION and "不是观测" in OC.DEALER_ASSUMPTION and "反过来" in OC.DEALER_ASSUMPTION

    def test_unavailable_gives_no_conclusion_but_the_full_field_set(self):
        d = OC.dealer_view(_res({"available": False, "reason": "chain_unavailable"}))
        assert d["available"] is False and d["reason"] == "chain_unavailable" and d["facts"] == []
        assert set(d) == set(OC.dealer_view(_res())), "两种情况字段要一样，页面才不会因缺键显示成别的"
        assert OC.dealer_view({})["reason"] == "no_gex_state" and OC.dealer_view({})["reason_zh"] == OC.REASONS["no_gex_state"]

    def test_missing_spot_drops_distances_instead_of_crashing(self):
        d = OC.dealer_view(_res(_gex(stock_price=None)))
        assert d["flip_dist_pct"] is None and d["shares_per_dollar"] is None
        assert any("Call 侧 200，Put 侧 170" in f for f in d["facts"]) and not any("距现价" in f for f in d["facts"])

    def test_expiry_concentration(self):
        cal = [{"expiry": "2026-12-18", "days_to": 38, "net_gamma": -150.0, "call_oi": 9, "put_oi": 11},
               {"expiry": "2026-11-20", "days_to": 10, "net_gamma": -250.0, "call_oi": 9, "put_oi": 7},
               {"expiry": "bad", "net_gamma": None}]
        d = OC.dealer_view(_res(cal=cal))
        assert [e["expiry"] for e in d["expiries"]] == ["2026-11-20", "2026-12-18"] and d["expiries"][0]["share_pct"] == 62.5
        assert any("2026-11-20（10 天后），占所列到期日 gamma 的 62%" in f for f in d["facts"])

    def test_change_versus_the_previous_scan(self):
        prev = _res(_gex(total_gex=-182.0, gex_flip=177.5, largest_put_wall=180.0))
        d = OC.dealer_view(_res(), prev)
        assert d["change"] == {"available": True, "regime_flipped": False, "prev_regime": "negative_gex",
                               "total_gex_musd": -61.05, "flip": -12.5, "call_wall": 0.0, "put_wall": -10.0}
        f = d["facts"][-1]
        assert "量级变大（-182 → -243 百万美元）" in f and "翻转位下移到 165" in f and "Put 侧集中位下移到 170" in f and "Call 侧" not in f
        flipped = OC.dealer_view(_res(), _res(_gex(regime="positive_gex", total_gex=50.0)))
        assert flipped["change"]["regime_flipped"] and "由正转负，对冲方向变了" in flipped["facts"][-1]
        assert OC.dealer_view(_res(), None)["change"] == {"available": False}


E = "2026-11-20"
PREV_FLOWS = [_flow("large_call_volume", 180.0, E, 20000, 5e6), _flow("large_put_volume", 175.0, E, 20000, 4e6),
              _flow("call_sweep", 190.0, E, 10000, 3e6), _flow("large_put_volume", 200.0, E, 5000, 2e6),
              _flow("large_call_volume", 300.0, E, 9000, 1.5e6), _flow("large_call_volume", 150.0, "2026-11-06", 9000, 1.2e6),
              _flow("large_call_volume", 185.0, E, 100, 5e5)]          # 最后一张不到阈值
PREV = _res(flows=PREV_FLOWS, call_oi={"180.0": {E: 8000}, "190.0": {E: 1000}, "150.0": {"2026-11-06": 500}},
            put_oi={"175.0": {E: 30000}, "200.0": {E: 60000}})
TODAY = _res(flows=[_flow("large_call_volume", 170.0, E, 5000, 2e6, oi=1000), _flow("large_put_volume", 165.0, E, 500, 3e6, oi=9000),
                    _flow("large_put_volume", 165.0, E, 500, 2.5e6, oi=9000)],
             call_oi={"180.0": {E: 20000}, "190.0": {E: 2500}}, put_oi={"175.0": {E: 30400}, "200.0": {E: 58000}})


class TestBigTrades:
    def test_today_counts_contracts_once_and_flags_opening_evidence(self):
        b = OC.big_trades(TODAY, None, "2026-11-10", None)
        assert b["count"] == 2 and b["n_call"] == 1 and b["n_put"] == 1, "同一张合约被两条规则各标一次只算一次"
        assert b["put_premium"] == 3e6 and b["n_opening"] == 1 and b["n_opening_judged"] == 2
        assert "2 个合约的成交权利金过 1 百万美元" in b["facts"][0] and "1 个的成交量超过当时的未平仓量" in b["facts"][1]
        assert b["follow"]["available"] is False and "隔日验证做不了" in b["facts"][-1]

    def test_follow_through_compares_oi_change_to_the_earlier_volume(self):
        f = OC.big_trades(TODAY, PREV, "2026-11-10", "2026-11-09")["follow"]
        by = {(r["side"], r["strike"]): r for r in f["rows"]}
        assert (by[("call", 180.0)]["verdict"], by[("call", 180.0)]["kept_ratio"]) == ("kept", 0.6)       # +12000 / 20000
        assert (by[("call", 190.0)]["verdict"], by[("call", 190.0)]["kept_ratio"]) == ("partly", 0.15)    # +1500 / 10000
        assert (by[("put", 175.0)]["verdict"], by[("put", 175.0)]["kept_ratio"]) == ("gone", 0.02)        # +400 / 20000
        assert (by[("put", 200.0)]["verdict"], by[("put", 200.0)]["kept_ratio"]) == ("closing", -0.4)     # −2000 / 5000
        assert by[("call", 300.0)]["verdict"] == "unknown" and by[("call", 300.0)]["oi_change"] is None, "两边都没有这张合约 ⇒ 对不上，不当 0"
        assert by[("call", 150.0)]["verdict"] == "expired"
        assert f["tally"] == {"kept": 1, "partly": 1, "gone": 1, "closing": 1, "expired": 1, "unknown": 1} and f["count"] == 6
        assert f["prev_date"] == "2026-11-09"

    def test_follow_through_sentence_and_thresholds_come_from_rules(self):
        b = OC.big_trades(TODAY, PREV, "2026-11-10", "2026-11-09")
        s = next(x for x in b["facts"] if x.startswith("上一次扫描（2026-11-09）"))
        assert "6 个大额成交合约" in s and "1 个大部分留下了，1 个留下一部分，1 个基本没留下，1 个未平仓反而减少" in s
        assert "1 个已到期" in s and "1 个对不上" in s and "留下的占判得了的 50%" in s
        src = (REPO / "alphabot" / "options_compare.py").read_text(encoding="utf-8").split("def big_trades")[1].split("\ndef ")[0]
        assert 'RULES["kept_mostly"]' in src and 'RULES["kept_partly"]' in src and 'RULES["big_premium_usd"]' in src

    def test_oi_movers_cover_every_expiry_and_use_string_strike_keys(self):
        prev = _res(call_oi={"195.0": {"2027-02-19": 1151, E: 10}, "210.0": {"2027-02-19": 63434}}, put_oi={"140.0": {"2027-01-15": 27384}})
        today = _res(call_oi={"195.0": {"2027-02-19": 56133, E: 10}, "210.0": {"2027-02-19": 16147}, "999.0": {E: 5}},
                     put_oi={"140.0": {"2027-01-15": 74415}})
        m = OC.big_trades(today, prev, "2026-11-10", "2026-11-09")["oi_movers"]
        assert [(x["side"], x["strike"], x["expiry"], x["oi_change"]) for x in m] == [
            ("call", 195.0, "2027-02-19", 54982.0), ("call", 210.0, "2027-02-19", -47287.0), ("put", 140.0, "2027-01-15", 47031.0)]
        # 没变的（195 / 11-20）不列；只有一边有的（999）不当成「从 0 涨上来」

    def test_caveat_is_always_there_and_says_what_cannot_be_seen(self):
        for args in ((TODAY, PREV), (TODAY, None), ({}, None)):
            assert OC.big_trades(*args, "2026-11-10", "2026-11-09")["caveat"] == OC.BIG_TRADE_CAVEAT
        c = OC.BIG_TRADE_CAVEAT
        assert "不是一笔订单" in c and "不等于机构" in c and "是买入还是卖出" in c and "隔夜才更新" in c

    def test_no_identity_or_direction_claims_anywhere(self):
        d, b = OC.dealer_view(_res(), PREV), OC.big_trades(TODAY, PREV, "2026-11-10", "2026-11-09")
        text = " ".join(d["facts"] + b["facts"]) + " ".join(OC.KEPT_ZH.values())
        assert not [w for w in BANNED if w in text], text

    def test_nothing_flagged_is_said_plainly(self):
        b = OC.big_trades(_res(), None, "2026-11-10", None)
        assert b["count"] == 0 and b["top"] == [] and b["facts"][0] == "今天没有成交权利金过 1 百万美元的合约。"

    def test_sweep_is_labelled_by_its_real_rule(self):
        # 扫描的 `*_sweep` 判据是「成交量 ≥ 未平仓的 5 倍」，不是跨交易所扫单
        assert OC.FLOW_TYPE_ZH["call_sweep"] == "Call 成交远超未平仓" and "扫单" not in "".join(OC.FLOW_TYPE_ZH.values())


class TestDeepPage:
    def test_build_carries_the_section_with_and_without_a_previous_scan(self):
        full = DR.build("NVDA", "2026-11-10", TODAY, None, prev=PREV, prev_date="2026-11-09")["positioning"]
        assert full["prev_date"] == "2026-11-09" and full["dealer"]["available"] and full["big"]["follow"]["count"] == 6
        alone = DR.build("NVDA", "2026-11-10", TODAY)["positioning"]
        assert alone["prev_date"] is None and alone["big"]["follow"]["available"] is False and alone["dealer"]["change"] == {"available": False}

    def test_page_states_both_premises_and_does_not_restyle_them(self):
        js = (REPO / "alphabot" / "static" / "pages" / "deep.js").read_text(encoding="utf-8")
        body = js.split("function positioning(d) {")[1].split("\nfunction ")[0]
        assert "note(dl.assumption)" in body and "note(bg.caveat)" in body, "前提与限制用服务端的原文，页面不另写一份"
        assert "不是某一方没走" in body and "对不出是不是同一批人" in body
        # 未平仓隔夜才更新：两次扫描之差说的是上一次扫描那天的开平仓，不是这一次当天的——两张表下面都要写明
        assert body.count("note(when, ") == 2 and "（含）到 ${d.date}（不含）之间的交易日里" in body and "要到下一次扫描才看得到" in body
        assert not [w for w in BANNED if w in body] and "style=" not in js

    def test_service_passes_the_previous_scan_of_the_same_ticker(self, tmp_path):
        import json
        from alphabot.service import AlphaBotService
        for d, r in (("2026-11-06", {"NVDA": PREV}), ("2026-11-09", {"JNJ": PREV}), ("2026-11-10", {"NVDA": TODAY})):
            (tmp_path / f".swarm_results_{d}.json").write_text(json.dumps(r), encoding="utf-8")
        s = AlphaBotService(state_dir=tmp_path, compare_root=tmp_path, bars_fn=lambda t: {"available": False, "bars": []})
        out = s.deep("NVDA")
        # 11-09 那天没有 NVDA ⇒ 往回找到 11-06（跨过没有这只票的日子，不拿别的票顶替）
        assert out["available"] and out["positioning"]["prev_date"] == "2026-11-06"
        assert out["positioning"]["big"]["follow"]["count"] == 6
        first = s.deep("NVDA", "2026-11-06")
        assert first["positioning"]["prev_date"] is None


@pytest.mark.parametrize("fn", ["dealer_view", "big_trades"])
def test_garbage_in_never_raises(fn):
    for junk in (None, "x", [], {"gex_state": "x", "agent_details": {"OracleBeeEcho": {"details": {"unusual_activity": "x", "full_chain_oi": 3}}}}):
        out = getattr(OC, fn)(junk, junk) if fn == "dealer_view" else OC.big_trades(junk, junk, "2026-11-10", "2026-11-09")
        assert isinstance(out, dict) and isinstance(out["facts"], list)
