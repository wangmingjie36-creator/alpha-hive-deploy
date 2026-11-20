"""「让 Claude 核对」（v0.45.794）的守卫：全程不出网——`one_shot` 用对话测试的假客户端（脚本化回复），
`ThesisClaude` 用假引擎或真引擎配假客户端；后台线程换成同步调用。

守的是 `alphabot/thesis_claude.py` docstring 的不变式：人确认了才花钱、花钱只走对话的同一道闸、出错就停、
结果逐条落盘且原文改了作废、只对最新一天显示、只开 NVDA。"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import ledger_io
from alphabot import chat as C
from alphabot import thesis_auto as TA
from alphabot import thesis_claude as TC
from tests.test_alphabot import REPO, _demo_service
from tests.test_alphabot_chat import T0, FakeClient, resp, spend_lines, text

CONDS = [
    {"id": "competitor_threat", "level": 1, "label": "竞争对手威胁", "trigger": "AMD/Intel 发布超越 H100 的产品"},
    {"id": "export_ban", "level": 2, "label": "美国芯片出口禁令", "trigger": "BIS 新规则禁止对华销售"},
]
DATES = ["2026-11-19", "2026-11-20"]


def verdict(status="not_fired", **kw):
    obj = {"status": status, "finding": "查到了最新进展", "ambiguity": "", "as_of": "2026-11-19",
           "sources": [{"url": "https://example.com/a", "date": "2026-11-19", "what": "公告"}], "confidence": "high", **kw}
    return f"核对如下。\n```json\n{json.dumps(obj, ensure_ascii=False)}\n```"


class FakeEngine:
    """只实现 ThesisClaude 用到的两个方法；`script` 每项是一条 one_shot 的回复文字或要抛的异常。"""

    def __init__(self, *script, spent=0.5):
        self.script, self.calls, self.spent = list(script), [], spent

    def one_shot(self, prompt, *, tag, system):
        self.calls.append({"prompt": prompt, "tag": tag, "system": system})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return {"text": item, "cost_usd": 0.4, "searches": 5, "calls": 1, "notes": [], "stop_reason": "end_turn",
                "served_by": None}

    def status(self):
        return {"spent_today": self.spent, "cap_usd": 3.0}


@pytest.fixture
def svc(tmp_path):
    return _demo_service(state_dir=tmp_path / "state")


def make(svc, eng, conds=None, dates=None, **kw):
    box = {"conds": list(conds if conds is not None else CONDS)}
    tc = TC.ThesisClaude(svc, eng, conditions_fn=lambda t: (box["conds"], None),
                         dates_fn=lambda: list(dates if dates is not None else DATES), spawn=lambda fn: fn(), **kw)
    return tc, box


class TestConditions:
    def test_real_config_nvda_is_the_four_not_auto_checked(self):
        cfg = json.loads((REPO / "thesis_breaks_config.json").read_text(encoding="utf-8"))
        auto_ids = {i for (t, i) in TA.CHECKS if t == "NVDA"}
        ids = [c["id"] for c in TC.manual_conditions(cfg["NVDA"], auto_ids)]
        assert set(ids) == {"datacenter_revenue_decline", "competitor_threat", "export_ban", "ceo_departure"}
        assert not any(("NVDA", i) in TA.CHECKS for i in ids), "归 thesis_auto 的条件不该再让 Claude 付费核对"

    def test_machine_and_blank_conditions_are_skipped(self):
        block = {"level_1_warning": {"conditions": [
            {"_machine": True, "field": "x", "op": ">", "value": 1}, {"id": "a"}, {"id": "b", "trigger": "t", "metric": "m"}]},
            "level_2_stop_loss": {"conditions": []}}
        assert TC.manual_conditions(block) == [{"id": "b", "level": 1, "label": "m", "trigger": "t"}]
        assert TC.manual_conditions(block, {"b"}) == []

    def test_only_nvda(self):
        assert TC.TICKERS == ("NVDA",), "用户 2026-11-20 定只做 NVDA；加票先问用户（每条都要花钱）"


class TestParse:
    def test_good(self):
        out, why = TC.parse_reply(verdict("fired", confidence="low"))
        assert why is None and out["status"] == "fired" and out["confidence"] == "low"
        assert out["sources"][0]["url"] == "https://example.com/a"

    @pytest.mark.parametrize("txt,frag", [
        ("没有代码块", "没有 JSON"), ("```json\n{bad\n```", "不是合法 JSON"), ("```json\n[1]\n```", "不是对象"),
        ("```json\n{\"status\": \"maybe\"}\n```", "三种之一")])
    def test_bad_is_explained(self, txt, frag):
        out, why = TC.parse_reply(txt)
        assert out is None and frag in why

    def test_non_http_sources_and_bad_confidence_dropped(self):
        out, _ = TC.parse_reply(verdict(sources=[{"url": "javascript:alert(1)"}, {"url": "https://ok"}], confidence="huge"))
        assert [s["url"] for s in out["sources"]] == ["https://ok"] and out["confidence"] is None


class TestConsentGate:
    @pytest.mark.parametrize("confirm", [None, False, "true", 1, "yes"])
    def test_needs_literal_true(self, svc, confirm):
        eng = FakeEngine(verdict())
        tc, _ = make(svc, eng)
        with pytest.raises(C.ChatError, match="确认"):
            tc.start("NVDA", confirm)
        assert eng.calls == [], "没确认就发了请求"

    def test_other_tickers_refused(self, svc):
        eng = FakeEngine(verdict())
        tc, _ = make(svc, eng)
        with pytest.raises(C.ChatError, match="只做 NVDA"):
            tc.start("AMD", True)
        assert tc.status("AMD")["supported"] is False and eng.calls == []

    def test_demo_refused(self, tmp_path):
        s = _demo_service(state_dir=tmp_path / "s", demo=True)
        eng = FakeEngine(verdict())
        tc, _ = make(s, eng)
        with pytest.raises(C.ChatError, match="演示"):
            tc.start("NVDA", True)
        assert eng.calls == []

    def test_second_start_while_running_is_409(self, svc):
        eng = FakeEngine(verdict(), verdict())
        tc = TC.ThesisClaude(svc, eng, conditions_fn=lambda t: (CONDS, None), dates_fn=lambda: DATES,
                             spawn=lambda fn: None)          # 线程没跑完：进度一直挂着
        tc.start("NVDA", True)
        with pytest.raises(C.ChatError) as e:
            tc.start("NVDA", True)
        assert e.value.status == 409


class TestRun:
    def test_happy_path_saves_each_and_counts_cost(self, svc):
        eng = FakeEngine(verdict("not_fired"), verdict("fired"))
        tc, _ = make(svc, eng)
        assert tc.start("NVDA", True)["total"] == 2
        assert [c["tag"] for c in eng.calls] == ["thesis:NVDA:competitor_threat", "thesis:NVDA:export_ban"]
        assert "2026-" in eng.calls[0]["prompt"] and CONDS[0]["trigger"] in eng.calls[0]["prompt"]
        st = tc.status("NVDA")
        assert st["applicable"] and st["running"] is None
        last = st["last"]
        assert last["state"] == "done" and last["spent_usd"] == 0.8 and last["stopped_zh"] is None
        assert [r["status"] for r in last["results"]] == ["not_fired", "fired"]
        assert all(r["stale"] is None for r in last["results"])
        on_disk = ledger_io.load_json(Path(svc.state_dir()) / "thesis_claude" / "NVDA.json")
        assert on_disk["results"][1]["trigger"] == CONDS[1]["trigger"], "落盘要带核对时的条件原文"

    @pytest.mark.parametrize("exc", [C.ChatMaybeBilled("断线，可能已计费"), C.BudgetExceeded("超上限"),
                                     C.ChatUnavailable("取不到客户端"), C.ChatRejected("被拒")])
    def test_any_error_stops_the_rest(self, svc, exc):
        eng = FakeEngine(exc, verdict())
        tc, _ = make(svc, eng)
        tc.start("NVDA", True)
        assert len(eng.calls) == 1, "出错后还在发请求（可能再付一次）"
        last = tc.status("NVDA")["last"]
        assert last["state"] == "stopped" and "1 条没发请求" in last["stopped_zh"]
        assert last["not_run"] == ["export_ban"] and last["results"][0]["error_zh"] == str(exc)

    def test_unparseable_reply_is_kept_as_paid_error_and_run_continues(self, svc):
        eng = FakeEngine("我不给 JSON", verdict())
        tc, _ = make(svc, eng)
        tc.start("NVDA", True)
        last = tc.status("NVDA")["last"]
        assert last["state"] == "done" and len(eng.calls) == 2
        assert last["results"][0]["status"] is None and "已计费" in last["results"][0]["error_zh"]
        assert last["spent_usd"] == 0.8

    def test_changed_trigger_voids_that_result(self, svc):
        eng = FakeEngine(verdict(), verdict())
        tc, box = make(svc, eng)
        tc.start("NVDA", True)
        box["conds"] = [CONDS[0], {**CONDS[1], "trigger": "改过的原文"}]
        res = {r["id"]: r["stale"] for r in tc.status("NVDA")["last"]["results"]}
        assert res == {"competitor_threat": None, "export_ban": "trigger_changed"}
        box["conds"] = [CONDS[0]]
        assert tc.status("NVDA")["last"]["results"][1]["stale"] == "not_in_config"

    def test_only_latest_day_is_applicable(self, svc):
        tc, _ = make(svc, FakeEngine())
        assert tc.status("NVDA", "2026-11-19")["applicable"] is False
        assert tc.status("NVDA", "2026-11-20")["applicable"] is True

    def test_running_on_disk_but_not_in_process_is_interrupted(self, svc):
        p = Path(svc.state_dir()) / "thesis_claude" / "NVDA.json"
        ledger_io.write_json(p, {"ticker": "NVDA", "state": "running", "results": [], "spent_usd": 0})
        tc, _ = make(svc, FakeEngine())
        assert tc.status("NVDA")["last"]["state"] == "interrupted"

    def test_save_failure_keeps_result_in_memory_and_says_so(self, svc, monkeypatch):
        def boom(*a, **k):
            raise ledger_io.LedgerWriteError("磁盘满")
        monkeypatch.setattr(TC.ledger_io, "write_json", boom)
        tc, _ = make(svc, FakeEngine(verdict(), verdict()))
        tc.start("NVDA", True)
        st = tc.status("NVDA")
        assert st["save_error"] and "磁盘满" in st["save_error"]
        assert [r["status"] for r in st["last"]["results"]] == ["not_fired", "not_fired"], "付了钱的结果不能因为没存上就丢"

    def test_corrupt_saved_file_is_reported_not_treated_as_empty(self, svc):
        p = Path(svc.state_dir()) / "thesis_claude" / "NVDA.json"
        p.parent.mkdir(parents=True)
        p.write_text("{坏", encoding="utf-8")
        st = make(svc, FakeEngine())[0].status("NVDA")
        assert st["last"] is None and "读不了" in st["load_error"]


class TestOneShotUsesTheSameGate:
    """真 ChatEngine + 假客户端：one_shot 走 `_gated_call`——同一个上限、同一份 spend.jsonl。"""

    def _eng(self, svc, *script, **kw):
        client = FakeClient(*script)
        return C.ChatEngine(svc, client_fn=lambda: client, now_fn=lambda: T0, **kw), client

    def test_books_spend_with_tag_and_sends_only_web_search(self, svc):
        assert svc.settings()["chat_enabled"] is False          # 不看对话开关：按钮每次都要人确认
        eng, client = self._eng(svc, resp([text(verdict())]))
        out = eng.one_shot("问", tag="thesis:NVDA:x", system="系统")
        assert out["calls"] == 1 and out["cost_usd"] > 0 and "```json" in out["text"]
        req = client.requests[0]
        assert [t["type"] for t in req["tools"]] == ["web_search_20260209"]
        assert req["system"][0]["text"] == "系统" and client.options[0]["max_retries"] == 0
        rows = spend_lines(svc)
        assert len(rows) == 1 and rows[0]["conversation"] == "thesis:NVDA:x"

    def test_pause_turn_continues_then_stops_at_cap(self, svc):
        eng, client = self._eng(svc, *[resp([text("…")], stop="pause_turn") for _ in range(C.ONE_SHOT_MAX_CALLS)])
        out = eng.one_shot("问", tag="t", system="s")
        assert out["calls"] == C.ONE_SHOT_MAX_CALLS and any("调用上限" in n for n in out["notes"])
        assert len(spend_lines(svc)) == C.ONE_SHOT_MAX_CALLS

    def test_budget_gate_blocks_before_sending(self, svc):
        eng, client = self._eng(svc, resp([text("x")]), cap_usd=0.01)
        with pytest.raises(C.BudgetExceeded):
            eng.one_shot("问", tag="t", system="s")
        assert client.requests == [] and spend_lines(svc) == []

    def test_demo_never_calls(self, tmp_path):
        s = _demo_service(state_dir=tmp_path / "s", demo=True)
        eng, client = self._eng(s, resp([text("x")]))
        with pytest.raises(C.ChatDisabled):
            eng.one_shot("问", tag="t", system="s")
        assert client.requests == []

    def test_end_to_end_with_thesis_claude(self, svc):
        eng, client = self._eng(svc, resp([text(verdict("fired"))]), resp([text(verdict())]))
        tc, _ = make(svc, eng)
        tc.start("NVDA", True)
        st = tc.status("NVDA")
        assert [r["status"] for r in st["last"]["results"]] == ["fired", "not_fired"]
        assert st["spent_today"] == pytest.approx(sum(r["usd"] for r in spend_lines(svc)))


class TestModuleBoundaries:
    def test_thesis_claude_never_touches_the_api_or_books_spend(self):
        tree = ast.parse((REPO / "alphabot" / "thesis_claude.py").read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not ({"anthropic", "llm_service", "httpx"} & (names | mods)), "花钱只许经 ChatEngine.one_shot"
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not ({"_gated_call", "_call", "_record", "_write_spend", "_book"} & attrs), "别绕过 one_shot 直接碰闸内部"

    def test_route_needs_write_header_and_confirm(self, svc):
        from starlette.testclient import TestClient
        from alphabot.server import create_app
        eng = FakeEngine(verdict(), verdict())
        tc, _ = make(svc, eng)
        cl = TestClient(create_app(svc, thesis_claude=tc), base_url="http://127.0.0.1:8765")
        assert cl.post("/api/thesis-claude/NVDA", json={"confirm": True}).status_code == 403
        h = {"X-AlphaBot": "1"}
        assert cl.post("/api/thesis-claude/NVDA", json={}, headers=h).status_code == 400
        assert eng.calls == []
        r = cl.post("/api/thesis-claude/NVDA", json={"confirm": True}, headers=h)
        assert r.status_code == 200 and r.json()["started"] is True and len(eng.calls) == 2
        g = cl.get("/api/thesis-claude/NVDA").json()
        assert g["supported"] and g["estimate"]["low_usd"] == round(TC.EST_PER_CONDITION_USD[0] * 2, 2)
