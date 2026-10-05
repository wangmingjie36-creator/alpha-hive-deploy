"""回放行情库（v0.45.410，`replay_ohlc_store.ReplayOhlcStore`）的守卫。

为什么有它：F&G 前瞻检验每次运行都把整个窗口的日线向 Yahoo 重新下载一遍（10-03 实测占 `run()` 的 97%），
Yahoo 拒绝整段请求时退回逐次直连，次数与快照天数成正比——Step 11 的 45s 预算越往后越不可能在降级日跑完。
修法：已落定的日线只下载一次（模块 docstring 有完整理由与用户 10-04 定的三条规则）。

这里守的是：
  1. 落定规则的每一条腿：同一天两次不落定；只落定早于较早那次下载日期的日子；临时值 / 某次响应漏一天不会被冻进库；
     「这天没有日线」（休市）同样要两次确认、落定后由库权威回答；
  2. 补下载从已落定段末尾往前 `OVERLAP_DAYS` 天开始；库覆盖不到窗口左端 ⇒ 整段；覆盖整个窗口 ⇒ 不打网络；
  3. 已落定后 Yahoo 改了 ⇒ 沿用库里的值、记修订、报次数（只在首次发现时计新增，之后每次都报总数）；
  4. 坏文件改名留证、写不进去计数，两者都让 `ohlc_window.degraded` 为真（进度行 ⚠️ + attention）；
  5. 窗口集成：补尾、下载失败时已落定段照样回答、只有碰到未落定日子的请求才直连；
  6. 端到端：连跑三天结果与不用库时逐字节相同，第三天只补尾；降级日直连次数比不用库时少、结果不变；
     修订被冻结且看得见、不换图标；
  7. 只有前瞻 `run()` 构造它；`--rehearse` / `--insample` / 前提核对脚本都不用它；目录调用时求值、进数据备份。
每条测试的 docstring 写明「哪个变异会让它红」。全部离线（假 yfinance）。
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import sys
import types
from pathlib import Path

import pytest

import ic_rerun_readiness as rr
import paper_portfolio as pp
import replay_ohlc_store as rs
from replay_ohlc_store import OVERLAP_DAYS, ReplayOhlcStore
from tests import test_replay_ohlc_window as _rw
from tests.test_replay_ohlc_window import (
    BEFORE, SEED, SINCE, WINDOW, FakeYF, _core, _load_premise, _seed_only_plan, _write_production_records, fwd,
)

#: 复用 v0.45.391 的合成世界（SL / TP / TIME 出场、NaN 日线、as_of 缺 bar、B 开出 A 没开的标的）
world = _rw.world

_ROOT = Path(__file__).resolve().parent.parent

# ── 纯单元：直接调 fetch_start / merge（模拟窗口取数）──────────────────────────────────────────

W = ("2026-09-01", "2026-10-03")
HOLIDAY = "2026-09-07"   # 劳动节：工作日、但没有日线


def _bar(c):
    return {"Open": c, "High": c + 1.0, "Low": c - 1.0, "Close": c}


def _master():
    out, d, i = {}, dt.date(2026, 8, 3), 0
    while d < dt.date(2026, 10, 12):
        s = d.isoformat()
        if d.weekday() < 5 and s != HOLIDAY:
            out[s] = _bar(100.0 + i)
            i += 1
        d += dt.timedelta(days=1)
    return out


def _store(root, day):
    return ReplayOhlcStore(root, clock=lambda: dt.date.fromisoformat(day))


def _fetch(store, master, ticker="AAA", w=W):
    """一次「窗口取数」：问库从哪天下，按 Yahoo（`master`）给那段，再并进库。返回（fstart, 本次回答）。"""
    fs = store.fetch_start(ticker, *w)
    bars = None if fs is None else {d: dict(b) for d, b in master.items() if fs <= d < w[1]}
    return fs, store.merge(ticker, w[0], w[1], fs, bars)


def _file(root, ticker="AAA"):
    return json.loads((Path(root) / f"{ticker}.json").read_text(encoding="utf-8"))


def _slice(master, lo, hi):
    return {d: b for d, b in master.items() if lo <= d < hi}


class TestSettlementRule:
    def test_first_fetch_settles_nothing_and_answers_with_the_fetch(self, tmp_path):
        m = _master()
        fs, got = _fetch(_store(tmp_path, "2026-09-20"), m)
        assert fs == W[0] and got == _slice(m, *W)
        f = _file(tmp_path)
        assert f["settled"] is None and f["pending"]["fetched_on"] == "2026-09-20"

    def test_same_day_second_fetch_settles_nothing(self, tmp_path):
        """变异「不要求不同美东日期」⇒ 同一天重跑两次就落定 ⇒ 红。"""
        m = _master()
        _fetch(_store(tmp_path, "2026-09-20"), m)
        _fetch(_store(tmp_path, "2026-09-20"), m)
        assert _file(tmp_path)["settled"] is None

    def test_only_days_before_the_earlier_fetch_settle(self, tmp_path):
        """两次下载里 09-20 之后（含未来）的日子同样一致，但较早那次下载在 09-20——那时它们还没收盘 / 还没发生。
        变异「候选区间右端用下载区间而不是较早那次的日期」⇒ 未来的「没有日线」被确认 ⇒ 红。"""
        m = _master()
        _fetch(_store(tmp_path, "2026-09-20"), m)
        s2 = _store(tmp_path, "2026-09-21")
        _fetch(s2, m)
        st = _file(tmp_path)["settled"]
        assert (st["start"], st["end"]) == (W[0], "2026-09-19")
        assert s2.stats()["settled_days_added"] == 19

    def test_holiday_absence_is_settled_and_answered_by_the_store(self, tmp_path):
        """「这天没有日线」也是两次确认过的事实：已落定段内由库回答 `{}`，不是 None（None 会去打网络）。"""
        m = _master()
        _fetch(_store(tmp_path, "2026-09-20"), m)
        _fetch(_store(tmp_path, "2026-09-21"), m)
        s3 = _store(tmp_path, "2026-09-22")
        assert s3.settled_slice("AAA", HOLIDAY, "2026-09-08") == {}
        assert s3.settled_slice("AAA", "2026-09-04", "2026-09-09") == _slice(m, "2026-09-04", "2026-09-09")
        assert s3.settled_slice("AAA", "2026-09-18", "2026-09-21") is None, "跨出已落定段的请求不许由库回答"

    def test_provisional_value_is_not_frozen(self, tmp_path):
        """09-18 的日线在 09-20 那次下载里是临时值、09-21 起是终值 ⇒ 落定停在 09-17，09-22 再确认终值。
        变异「满 N 天就落定 / 单次下载就落定」⇒ 临时值被冻进库 ⇒ 红。"""
        m = _master()
        first = {**m, "2026-09-18": _bar(999.0)}
        _fetch(_store(tmp_path, "2026-09-20"), first)
        _, got = _fetch(_store(tmp_path, "2026-09-21"), m)
        assert _file(tmp_path)["settled"]["end"] == "2026-09-17"
        assert got["2026-09-18"] == m["2026-09-18"], "未落定的日子用这次下载的值"
        _fetch(_store(tmp_path, "2026-09-22"), m)
        st = _file(tmp_path)["settled"]
        assert st["end"] == "2026-09-20" and st["bars"]["2026-09-18"] == m["2026-09-18"]

    def test_a_response_missing_one_day_is_not_frozen(self, tmp_path):
        """Yahoo 某次响应漏了 09-10（v0.45.383/387 记过）⇒ 落定停在 09-09，下一次补上。
        变异「只比两边都有的日子」⇒ 漏掉的那天被确认成休市 ⇒ 红。"""
        m = _master()
        holey = {d: b for d, b in m.items() if d != "2026-09-10"}
        _fetch(_store(tmp_path, "2026-09-20"), holey)
        _fetch(_store(tmp_path, "2026-09-21"), m)
        assert _file(tmp_path)["settled"]["end"] == "2026-09-09"
        _fetch(_store(tmp_path, "2026-09-22"), m)
        st = _file(tmp_path)["settled"]
        assert st["end"] == "2026-09-20" and st["bars"]["2026-09-10"] == m["2026-09-10"]

    def test_disagreement_on_the_first_day_settles_nothing(self, tmp_path):
        """还没有已落定段时，落定段只能从比对区间的第一天起长；第一天两次就不一致 ⇒ 这次什么都不落定（下次再比），
        不能从第一天起把这次下载的值冻进去。变异「首日不一致也从首日起落定」⇒ 09-01 的未确认值进库 ⇒ 红（二次变异补）。"""
        m = _master()
        _fetch(_store(tmp_path, "2026-09-20"), {**m, W[0]: _bar(999.0)})
        _fetch(_store(tmp_path, "2026-09-21"), m)
        assert _file(tmp_path)["settled"] is None
        _fetch(_store(tmp_path, "2026-09-22"), m)
        st = _file(tmp_path)["settled"]
        assert (st["start"], st["end"]) == (W[0], "2026-09-20") and st["bars"][W[0]] == m[W[0]]

    def test_clock_going_backwards_settles_nothing_and_keeps_the_newer_pending(self, tmp_path):
        m = _master()
        _fetch(_store(tmp_path, "2026-09-21"), m)
        _fetch(_store(tmp_path, "2026-09-20"), m)
        f = _file(tmp_path)
        assert f["settled"] is None and f["pending"]["fetched_on"] == "2026-09-21"


def _settle_through_0919(root, master):
    _fetch(_store(root, "2026-09-20"), master)
    _fetch(_store(root, "2026-09-21"), master)


class TestFetchRange:
    def test_tail_starts_overlap_days_before_the_settled_end(self, tmp_path):
        """变异「补尾从已落定段末尾 +1 开始」（不重叠 ⇒ 修订永远看不见）⇒ 红。"""
        m = _master()
        _settle_through_0919(tmp_path, m)
        s = _store(tmp_path, "2026-09-22")
        assert s.fetch_start("AAA", *W) == "2026-09-10" == (
            dt.date(2026, 9, 19) - dt.timedelta(days=OVERLAP_DAYS - 1)).isoformat()
        assert OVERLAP_DAYS == 10

    def test_tail_answer_equals_a_full_download(self, tmp_path):
        """补尾那次的回答 = 已落定段 ∪ 这次下载里已落定段之外的部分 ⇒ 与整段下载逐根相同（Yahoo 没改过时）。"""
        m = _master()
        _settle_through_0919(tmp_path, m)
        s = _store(tmp_path, "2026-09-22")
        fs, got = _fetch(s, m)
        assert fs == "2026-09-10" and got == _slice(m, *W)
        assert s.stats()["tail_fetches"] == 1 and s.stats()["full_fetches"] == 0

    def test_window_starting_before_the_store_downloads_everything(self, tmp_path):
        """库覆盖不到窗口左端 ⇒ 整段（否则左边那段没人回答）；之后左边同样要两次确认才并进已落定段。"""
        m = _master()
        _settle_through_0919(tmp_path, m)
        wide = ("2026-08-20", W[1])
        assert _store(tmp_path, "2026-09-22").fetch_start("AAA", *wide) == wide[0]
        _fetch(_store(tmp_path, "2026-09-22"), m, w=wide)
        _fetch(_store(tmp_path, "2026-09-23"), m, w=wide)
        st = _file(tmp_path)["settled"]
        assert (st["start"], st["end"]) == (wide[0], "2026-09-21")

    def test_fully_settled_window_never_touches_the_network(self, tmp_path):
        m = _master()
        _settle_through_0919(tmp_path, m)
        s = _store(tmp_path, "2026-09-22")
        w = ("2026-09-02", "2026-09-15")
        fs, got = _fetch(s, m, w=w)
        assert fs is None and got == _slice(m, *w) and s.stats()["store_only"] == 1


class TestRevisions:
    """「Yahoo 改了已落定的日线」同落定一样要两次**不同美东日期**的观察给出同一个新值才算（二次检查：单次响应漏一天
    曾被永久记成修订）。确认后成为一个**版本**（v0.45.415 时点数据）：重放日早于生效日用旧值、之后用新值。
    `merge()` 返回最新版本；按重放日取版本是 `pit_slice()` 的事（`TestPointInTime` 有完整的一组）。"""

    def test_revised_bar_becomes_a_version_on_the_second_look(self, tmp_path):
        """09-22 第一次看到 09-15 的新值：只记嫌疑、不报、回答仍是旧值；09-23 再看到同一个新值：成为生效日 09-22 的版本、报。
        变异「单次即记」⇒ 第一天就报 ⇒ 红；变异「不记 / 不计」⇒ 红；变异「不按重放日取版本」⇒ 09-21 拿到新值 ⇒ 红。"""
        m = _master()
        _settle_through_0919(tmp_path, m)
        revised = {**m, "2026-09-15": _bar(555.0)}
        s1 = _store(tmp_path, "2026-09-22")
        _, got = _fetch(s1, revised)
        assert got["2026-09-15"] == m["2026-09-15"], "只是嫌疑：最新版本仍是旧值"
        f = _file(tmp_path)
        assert f["revisions"] == {} and f["revision_suspects"] == {"2026-09-15": {"seen_on": "2026-09-22", "yahoo": _bar(555.0)}}
        assert (s1.stats()["revisions_new"], s1.stats()["revised_bars"]) == (0, 0)
        s2 = _store(tmp_path, "2026-09-23")
        _, got = _fetch(s2, revised)
        assert got["2026-09-15"] == _bar(555.0), "确认后最新版本是新值"
        assert s2.pit_slice("AAA", "2026-09-14", "2026-09-17", {}, "2026-09-21")["2026-09-15"] == m["2026-09-15"]
        assert s2.pit_slice("AAA", "2026-09-14", "2026-09-17", {}, "2026-09-22")["2026-09-15"] == _bar(555.0)
        f = _file(tmp_path)
        assert f["schema"] == 2 and f["revisions"] == {"2026-09-15": [
            {"first_seen": "2026-09-22", "confirmed_on": "2026-09-23", "yahoo": _bar(555.0)}]}
        assert f["revision_suspects"] == {}
        assert (s2.stats()["revised_recent"], s2.stats()["revised_recent_tickers"]) == (1, ["AAA"])
        assert (s2.stats()["revisions_new"], s2.stats()["revised_bars"], s2.stats()["revised_tickers"]) == (1, 1, ["AAA"])

    def test_a_one_off_glitch_is_never_reported(self, tmp_path):
        """09-22 那次响应漏了 09-15（Yahoo 偶发），09-23 恢复 ⇒ 嫌疑撤销、从不报。变异「单次即记」⇒ 永久误报 ⇒ 红。"""
        m = _master()
        _settle_through_0919(tmp_path, m)
        _fetch(_store(tmp_path, "2026-09-22"), {d: b for d, b in m.items() if d != "2026-09-15"})
        s = _store(tmp_path, "2026-09-23")
        _fetch(s, m)
        f = _file(tmp_path)
        assert f["revisions"] == {} and f["revision_suspects"] == {} and s.stats()["revised_bars"] == 0

    def test_a_second_look_on_the_same_day_does_not_confirm(self, tmp_path):
        """变异「不要求不同日期」⇒ 同一天重跑就确认 ⇒ 红。"""
        m = _master()
        _settle_through_0919(tmp_path, m)
        revised = {**m, "2026-09-15": _bar(555.0)}
        _fetch(_store(tmp_path, "2026-09-22"), revised)
        _fetch(_store(tmp_path, "2026-09-22"), revised)
        assert _file(tmp_path)["revisions"] == {}

    def test_known_revision_is_reported_every_time_but_counted_new_once(self, tmp_path):
        """变异「只在确认那天报」⇒ 第三天的 revised_bars 为 0 ⇒ 红（只报一天的警示没人看得见）。"""
        m = _master()
        _settle_through_0919(tmp_path, m)
        revised = {**m, "2026-09-15": _bar(555.0)}
        _fetch(_store(tmp_path, "2026-09-22"), revised)
        _fetch(_store(tmp_path, "2026-09-23"), revised)
        s = _store(tmp_path, "2026-09-24")
        _fetch(s, revised)
        assert (s.stats()["revisions_new"], s.stats()["revised_bars"]) == (0, 1)
        assert _file(tmp_path)["revisions"]["2026-09-15"][0]["first_seen"] == "2026-09-22"

    def test_while_a_suspect_is_open_the_whole_window_is_downloaded(self, tmp_path):
        """v0.45.415：有嫌疑 ⇒ 之后每次整段下载，直到确认或撤销（拆股改的是全段历史，窗口里每个日期都要第二次观察）。
        嫌疑在重叠段最左一天（09-10）也一样被再看一次。变异「有嫌疑仍只补尾」⇒ 09-23 从 09-11 起下载 ⇒ 红。"""
        m = _master()
        _settle_through_0919(tmp_path, m)
        revised = {**m, "2026-09-10": _bar(555.0)}
        _fetch(_store(tmp_path, "2026-09-22"), revised)
        s = _store(tmp_path, "2026-09-23")
        assert s.fetch_start("AAA", *W) == W[0]
        _fetch(s, revised)
        assert "2026-09-10" in _file(tmp_path)["revisions"]
        assert _store(tmp_path, "2026-09-24").fetch_start("AAA", *W) != W[0], "确认后嫌疑清空 ⇒ 回到补尾"

    def test_a_settled_bar_that_disappears_is_a_revision_too(self, tmp_path):
        """「这天没有日线了」也是一个版本：生效日之后的重放日看不到它，之前的照旧看得到。"""
        m = _master()
        _settle_through_0919(tmp_path, m)
        gone = {d: b for d, b in m.items() if d != "2026-09-16"}
        _fetch(_store(tmp_path, "2026-09-22"), gone)
        s = _store(tmp_path, "2026-09-23")
        _, got = _fetch(s, gone)
        assert "2026-09-16" not in got
        assert s.pit_slice("AAA", "2026-09-15", "2026-09-18", {}, "2026-09-21")["2026-09-16"] == m["2026-09-16"]
        assert _file(tmp_path)["revisions"]["2026-09-16"][0]["yahoo"] is None

    def test_revision_before_the_overlap_is_frozen_silently(self, tmp_path):
        """重叠段之外更早的修订看不见——这是「首次落定后冻结」的本意，回答照旧是库里的值。"""
        m = _master()
        _settle_through_0919(tmp_path, m)
        s = _store(tmp_path, "2026-09-22")
        _, got = _fetch(s, {**m, "2026-09-02": _bar(555.0)})
        assert got["2026-09-02"] == m["2026-09-02"] and s.stats()["revised_bars"] == 0


class TestBadFilesAndWrites:
    @pytest.mark.parametrize("blob", [
        b"{not json",
        json.dumps({"schema": 99, "ticker": "AAA", "settled": None, "pending": None}).encode(),
        json.dumps({"schema": 1, "ticker": "BBB", "settled": None, "pending": None}).encode(),
        json.dumps({"schema": 1, "ticker": "AAA", "pending": None,
                    "settled": {"start": "2026-09-01", "end": "2026-09-05",
                                "bars": {"2026-09-02": {"Open": 1, "High": 1, "Low": 1, "Close": float("nan")}}}}).encode(),
        json.dumps({"schema": 1, "ticker": "AAA", "pending": None,
                    "settled": {"start": "2026-09-05", "end": "2026-09-01", "bars": {}}}).encode(),
        json.dumps({"schema": 1, "ticker": "AAA", "pending": None,
                    "settled": {"start": "2026-09-01", "end": "2026-09-05", "bars": {"2026-09-09": _bar(1.0)}}}).encode(),
        json.dumps({"schema": 1, "ticker": "AAA", "pending": None,
                    "settled": {"start": "2026-09-01", "end": "2026-09-05",
                                "bars": {"2026-09-02": {"Open": True, "High": 1, "Low": 1, "Close": 1}}}}).encode(),
        json.dumps({"schema": 1, "ticker": "AAA", "pending": None, "settled": None,
                    "revisions": {"2026-09-02": {"first_seen": "2026-09-03", "yahoo": None}}}).encode(),
        json.dumps({"schema": 1, "ticker": "AAA", "settled": None,
                    "pending": {"fetched_on": "2026-09-31", "start": "2026-09-01", "end": "2026-09-05", "bars": {}}}).encode(),
        b'{"schema": 1, "ticker": "AAA"}',
        b'{"schema": 1, "ticker": "AAA", "pending": null, "settled": {"start": "2026-09-01", "end": "2026-09-05", '
        b'"bars": {"2026-09-02": {"Open": 1' + b"0" * 400 + b', "High": 1, "Low": 1, "Close": 1}}}}',
        json.dumps({"schema": 1, "ticker": "AAA", "pending": None,
                    "settled": {"start": "2026-09-01", "end": "2026-10-05", "bars": {"2026-09-31": _bar(1.0)}}}).encode(),
        json.dumps({"schema": 1, "ticker": "AAA", "pending": None, "settled": None,
                    "revision_suspects": {"2026-09-02": {"seen_on": "2026-09-03", "yahoo": None}}}).encode(),
        json.dumps({"schema": 1, "ticker": "AAA", "pending": None,
                    "settled": {"start": "0001-01-01", "end": "2026-09-05", "bars": {}}}).encode(),
    ], ids=["json", "schema", "ticker", "nan", "inverted", "bar-outside", "bool", "revision-unsettled", "bad-date",
            "missing-keys", "huge-int", "bar-not-a-date", "suspect-unsettled", "year-1"])
    def test_unreadable_file_is_moved_aside_reported_and_rebuilt(self, tmp_path, blob):
        """变异「坏文件当空库直接覆盖」⇒ 证据没了 ⇒ 红；变异「不计数」⇒ `problem` 为假 ⇒ 红。"""
        (tmp_path / "AAA.json").write_bytes(blob)
        m = _master()
        s = _store(tmp_path, "2026-09-20")
        fs, got = _fetch(s, m)
        assert fs == W[0] and got == _slice(m, *W)
        moved = list(tmp_path.glob("AAA.json.invalid-2026-09-20-*"))
        assert len(moved) == 1 and moved[0].read_bytes() == blob
        st = s.stats()
        assert len(st["invalid_files"]) == 1 and st["problem"] is True
        rs.validate_entry(_file(tmp_path), "AAA")

    @pytest.mark.parametrize("kind", ["unreadable-file", "is-a-directory", "unreadable-root"])
    def test_a_file_that_cannot_be_read_is_handled_not_a_crash(self, tmp_path, kind):
        """二次检查补：读不了（权限 / 同名目录 / 库目录本身没权限）⇒ 按坏文件处理、本次整段下载、计数——不许让整次检验崩掉。
        旧写法 `path.exists()` 在库目录没权限时直接抛 PermissionError（它只吞 ENOENT 一类）。"""
        root = tmp_path / "store"
        root.mkdir()
        p = root / "AAA.json"
        if kind == "is-a-directory":
            p.mkdir()
        else:
            p.write_text("{}")
            (p if kind == "unreadable-file" else root).chmod(0)
        try:
            m = _master()
            s = _store(root, "2026-09-20")
            fs, got = _fetch(s, m)
            assert fs == W[0] and got == _slice(m, *W)
            assert len(s.stats()["invalid_files"]) == 1 and s.stats()["problem"] is True
        finally:
            root.chmod(0o755)
            if p.is_file():
                p.chmod(0o644)

    def test_evidence_files_keep_the_problem_visible_until_a_human_deletes_them(self, tmp_path):
        """二次检查：坏文件原先只在改名那一次报——那次运行若被预算看门狗杀掉就一次都不报，之后冻结历史被悄悄重建。
        现在留证文件还在就一直 `problem`，删掉才消。变异「problem 只看本次」⇒ 第二次运行为假 ⇒ 红。"""
        (tmp_path / "AAA.json").write_bytes(b"{bad")
        m = _master()
        _fetch(_store(tmp_path, "2026-09-20"), m)
        s2 = _store(tmp_path, "2026-09-21")
        _fetch(s2, m)
        st = s2.stats()
        assert st["invalid_files"] == [] and st["quarantined"] == ["AAA.json.invalid-2026-09-20-0"] and st["problem"]
        (tmp_path / "AAA.json.invalid-2026-09-20-0").unlink()
        s3 = _store(tmp_path, "2026-09-22")
        _fetch(s3, m)
        assert s3.stats()["problem"] is False

    def test_if_the_bad_file_cannot_be_moved_it_is_never_overwritten(self, tmp_path, monkeypatch):
        (tmp_path / "AAA.json").write_bytes(b"{bad")
        real_replace = os.replace

        def _replace(src, dst):
            if ".invalid-" in str(dst):
                raise PermissionError("测试：移不开")
            return real_replace(src, dst)
        monkeypatch.setattr(rs.os, "replace", _replace)
        m = _master()
        s = _store(tmp_path, "2026-09-20")
        _, got = _fetch(s, m)
        assert got == _slice(m, *W) and (tmp_path / "AAA.json").read_bytes() == b"{bad"
        assert "本次不写" in s.stats()["invalid_files"][0] and s.stats()["problem"] is True

    def test_write_failure_is_counted_and_the_answer_is_unaffected(self, tmp_path):
        """变异「写失败吞掉不计」⇒ `problem` 为假 ⇒ 红（下次照样整段下载、却没人知道为什么）。"""
        root = tmp_path / "not_a_dir"
        root.write_text("x")
        m = _master()
        s = _store(root, "2026-09-20")
        _, got = _fetch(s, m)
        assert got == _slice(m, *W)
        assert len(s.stats()["write_errors"]) == 1 and s.stats()["problem"] is True

    def test_written_file_is_valid_and_no_temp_file_is_left(self, tmp_path):
        root = tmp_path / "store"   # tmp_path 本身还装着 conftest 的沙箱目录
        m = _master()
        _settle_through_0919(root, m)
        rs.validate_entry(_file(root), "AAA")
        assert sorted(p.name for p in root.iterdir()) == ["AAA.json"]

    @pytest.mark.parametrize("ticker,ok", [("BRK-B", True), ("^VIX", True), ("BRK.B", True), ("A/B", False),
                                           ("../x", False), ("", False), (None, False)])
    def test_only_filename_safe_tickers_use_the_store(self, ticker, ok):
        assert ReplayOhlcStore(Path("/nonexistent"), clock=lambda: dt.date(2026, 9, 20)).supports(ticker) is ok


class TestClock:
    @pytest.mark.parametrize("utc,et_day", [("2026-10-05T03:30:00", "2026-10-04"),    # UTC 已是 10-05
                                            ("2026-10-05T04:30:00", "2026-10-05")])   # 太平洋时间还是 10-04
    def test_fetch_day_is_the_us_eastern_date(self, monkeypatch, utc, et_day):
        """变异「用本机日期 / UTC 日期」⇒ 两个时刻里至少一个红（编排器在 14:00 PDT 跑，本机日期与美东可以不同）。"""
        instant = dt.datetime.fromisoformat(utc).replace(tzinfo=dt.timezone.utc)

        class _DT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
        monkeypatch.setattr(rs, "dt", types.SimpleNamespace(datetime=_DT, date=dt.date, timedelta=dt.timedelta))
        assert rs._et_today().isoformat() == et_day
        assert ReplayOhlcStore(Path("/nonexistent")).fetch_day == et_day


# ── 窗口集成（`paper_portfolio.replay_ohlc_window(..., store=)`）──────────────────────────────

def _yf_master(master):
    return {"AAA": {d: (b["Open"], b["High"], b["Low"], b["Close"]) for d, b in master.items()}}


@pytest.fixture
def fake_yf(monkeypatch):
    m = _master()
    fake = FakeYF(_yf_master(m))
    monkeypatch.setitem(sys.modules, "yfinance", fake.module())
    monkeypatch.setattr(pp, "_PRICE_CACHE", {})
    return m, fake


def _through_window(store, requests):
    with pp.replay_ohlc_window(*W, store=store) as win:
        out = [pp._fetch_ohlc("AAA", s, e) for s, e in requests]
    return out, win


_REQS = [("2026-09-02", "2026-09-10"), ("2026-09-14", "2026-09-19"), ("2026-09-15", "2026-09-25"),
         ("2026-09-28", "2026-10-01")]


class TestWindowIntegration:
    def test_third_day_downloads_only_the_tail(self, tmp_path, fake_yf):
        """变异「窗口忽略 store、照旧整段」⇒ 第三天那次请求的 start 是窗口左端 ⇒ 红。"""
        m, fake = fake_yf
        for day in ("2026-09-20", "2026-09-21"):
            _through_window(_store(tmp_path, day), _REQS)
        n0 = len(fake.calls)
        out, win = _through_window(_store(tmp_path, "2026-09-22"), _REQS)
        calls = fake.calls[n0:]
        assert [c[2]["start"] for c in calls] == ["2026-09-10"] and calls[0][2]["end"] == W[1]
        assert out == [_slice(m, s, e) for s, e in _REQS]
        st = win.stats()
        assert st["store"]["tail_fetches"] == 1 and st["degraded"] is False and st["wide_fetches"] == 1
        assert st["store"]["served_from_store"] == 2, "前两个请求整个在已落定段（≤ 09-19）内，没等下载就由库回答"

    def test_requests_inside_the_settled_range_never_trigger_a_download(self, tmp_path, fake_yf):
        """只在早几周持有过的标的：请求全在已落定段内 ⇒ 整次不打网络。
        变异「每个碰到的标的都先下载」⇒ 有一次调用 ⇒ 红（10-03 真实数据上就是 22 个标的全补尾、只快 1s）。"""
        m, fake = fake_yf
        for day in ("2026-09-20", "2026-09-21"):
            _through_window(_store(tmp_path, day), _REQS)
        n0 = len(fake.calls)
        out, win = _through_window(_store(tmp_path, "2026-09-22"), _REQS[:2])
        assert fake.calls[n0:] == [] and out == [_slice(m, s, e) for s, e in _REQS[:2]]
        st = win.stats()
        assert (st["wide_fetches"], st["store"]["served_from_store"], st["store"]["tail_fetches"]) == (0, 2, 0)

    def test_failed_download_still_answers_settled_requests_from_the_store(self, tmp_path, fake_yf):
        """补尾下载失败之后：完全在已落定段（≤ 09-19）内的请求照样由库回答，只有碰到未落定日子的才直连。
        第一个请求就碰到未落定日子 ⇒ 先触发下载、失败 ⇒ 之后的请求都走「下载失败」那条路。
        变异「下载失败后不查库」⇒ 已落定的两个请求也直连 ⇒ 红。"""
        m, fake = fake_yf
        for day in ("2026-09-20", "2026-09-21"):
            _through_window(_store(tmp_path, day), _REQS)
        fake.wide_fail = lambda t, s, e: "raise" if e == W[1] else None
        reqs = [_REQS[2], _REQS[0], _REQS[1], _REQS[3]]
        n0 = len(fake.calls)
        out, win = _through_window(_store(tmp_path, "2026-09-22"), reqs)
        direct = [(c[2]["start"], c[2]["end"]) for c in fake.calls[n0:] if c[2]["end"] != W[1]]
        assert direct == [("2026-09-15", "2026-09-25"), ("2026-09-28", "2026-10-01")]
        assert out == [_slice(m, s, e) for s, e in reqs]
        st = win.stats()
        assert (st["store"]["served_on_fallback"], st["store"]["served_from_store"]) == (2, 0)
        assert st["direct_requests"] == 2 and st["fallback"] == 1 and st["degraded"] is True

    def test_revision_stays_reported_after_the_ticker_stops_downloading(self, tmp_path, fake_yf):
        """二次检查补：修订是在补尾时发现的；之后这个标的平仓、请求全落在已落定段内 ⇒ 不再下载、不经 `merge()`——
        重放照旧用着冻结值，修订必须照样报。变异「只在 merge() 里数修订」⇒ 最后一次 revised_bars 为 0 ⇒ 红。"""
        m, fake = fake_yf
        for day in ("2026-09-20", "2026-09-21"):
            _through_window(_store(tmp_path, day), _REQS)
        o, h, lo, c = fake.master["AAA"]["2026-09-15"]
        fake.master["AAA"]["2026-09-15"] = (o, h, lo, c + 50.0)
        _through_window(_store(tmp_path, "2026-09-22"), _REQS)
        _, win = _through_window(_store(tmp_path, "2026-09-23"), _REQS)
        assert win.stats()["store"]["revised_bars"] == 1
        n0 = len(fake.calls)
        pp._REPLAY_AS_OF = "2026-09-21"   # 重放日早于生效日 09-22 ⇒ 时点上是旧值
        try:
            out, win = _through_window(_store(tmp_path, "2026-09-24"), [_REQS[0], ("2026-09-14", "2026-09-17")])
        finally:
            pp._REPLAY_AS_OF = None
        assert fake.calls[n0:] == [], "请求全在已落定段内：不下载"
        assert out[1]["2026-09-15"] == m["2026-09-15"], "重放日早于生效日：旧值"
        st = win.stats()["store"]
        assert (st["revised_bars"], st["revised_tickers"], st["revisions_new"]) == (1, ["AAA"], 0)

    def test_halted_ticker_empty_tail_is_an_answer_not_a_failure(self, tmp_path, fake_yf):
        """二次检查：标的 09-11 之后再无日线（停牌 / 退市），仓位还在重放里每天被请求。补尾区间在库里本来就没有日线 ⇒
        空结果是权威答案：不降级、不直连、落定段照常前进。变异「补尾空结果一律当失败」⇒ 每天降级、直连逐日增长 ⇒ 红。"""
        _, fake = fake_yf
        fake.master["AAA"] = {d: v for d, v in fake.master["AAA"].items() if d <= "2026-09-11"}
        reqs = [("2026-09-02", "2026-09-10"), ("2026-09-08", "2026-09-30")]
        for day in ("2026-09-24", "2026-09-25"):
            _through_window(_store(tmp_path, day), reqs)
        assert _file(tmp_path)["settled"]["end"] == "2026-09-23"
        n0 = len(fake.calls)
        out, win = _through_window(_store(tmp_path, "2026-09-28"), reqs)
        assert [c[2]["start"] for c in fake.calls[n0:]] == ["2026-09-14"], "补尾那一次下载拿到的是空结果"
        st = win.stats()
        assert (st["fallback"], st["direct_requests"], st["degraded"]) == (0, 0, False), st
        assert out[1] == {d: b for d, b in _master().items() if "2026-09-08" <= d <= "2026-09-11"}
        assert _file(tmp_path)["settled"]["end"] == "2026-09-24"

    def test_empty_tail_for_an_active_ticker_is_still_a_failure(self, tmp_path, fake_yf):
        """库里那段**有**日线时，补尾返回空 = Yahoo 抽风（v0.45.391 起就不把它当权威答案）⇒ 照旧降级、退回直连。
        变异「补尾空结果一律接受」⇒ 未落定的日子被答成没有日线 ⇒ 红。"""
        m, fake = fake_yf
        for day in ("2026-09-20", "2026-09-21"):
            _through_window(_store(tmp_path, day), _REQS)
        fake.wide_fail = lambda t, s, e: "empty" if e == W[1] else None
        out, win = _through_window(_store(tmp_path, "2026-09-22"), [_REQS[2]])
        st = win.stats()
        assert st["fallback"] == 1 and st["degraded"] is True and out == [_slice(m, *_REQS[2])]

    def test_window_without_store_has_no_store_key(self, fake_yf):
        _, win = _through_window(None, _REQS)
        assert "store" not in win.stats()

    def test_store_problem_makes_the_window_degraded(self, tmp_path, fake_yf):
        """坏文件 / 写不进去：本次回答不变，但 `degraded` 为真（谁会红：进度行 ⚠️ + attention）。
        变异「degraded 不看 store.problem」⇒ 红。"""
        root = tmp_path / "f"
        root.write_text("x")
        m, _ = fake_yf
        out, win = _through_window(_store(root, "2026-09-20"), _REQS)
        assert out == [_slice(m, s, e) for s, e in _REQS]
        st = win.stats()
        assert st["fallback"] == 0 and st["out_of_window"] == 0 and st["degraded"] is True


# ── 端到端：前瞻 run() / evaluate() 在合成世界里 ───────────────────────────────────────────

D1, D2, D3, D4 = "2026-08-27", "2026-08-28", "2026-08-31", "2026-09-01"
#: D1、D2 两次整段下载后，已落定段 = [窗口左端, D1 前一天]；D3 补尾从那里往前 OVERLAP_DAYS 天
TAIL_START = (dt.date(2026, 8, 26) - dt.timedelta(days=OVERLAP_DAYS - 1)).isoformat()


@pytest.fixture
def fwd_day(world, monkeypatch):
    """`run(today=BEFORE)`，美东「今天」由参数定（`rs._et_today`）——走的是真的 `_forward_ohlc_store`。"""
    _write_production_records(world)
    monkeypatch.setattr(fwd, "load_seed", lambda *a, **k: SEED)
    monkeypatch.setattr(fwd, "_forward_anchor_plan", _seed_only_plan)
    monkeypatch.setattr(fwd, "FORWARD_START", SINCE)

    def _run(day, *, store=True):
        monkeypatch.setattr(pp, "_PRICE_CACHE", {})
        monkeypatch.setattr(rs, "_et_today", lambda: dt.date.fromisoformat(day))
        monkeypatch.setattr(fwd, "_forward_ohlc_store", _REAL_FORWARD_STORE if store else (lambda: None))
        n0 = len(world.fake.calls)
        res = fwd.run(today=BEFORE)
        return res, world.fake.calls[n0:]
    return _run


_REAL_FORWARD_STORE = fwd._forward_ohlc_store


class TestForwardRunWithStore:
    def test_three_days_identical_results_and_the_third_only_downloads_tails(self, fwd_day):
        """第 1、2 天整段（库还没有已落定段），第 3 天只有请求碰到未落定日子的标的补尾（早早平仓的 SLX / TPX 等
        整次不打网络）；三天的结果与不用库时逐字节相同。
        变异「run() 不传库」⇒ 第 3 天仍是 11 次整段 ⇒ 红；变异「回答里已落定段与新下载拼错」⇒ 结果不同 ⇒ 红。"""
        base, _ = fwd_day(D1, store=False)
        for day in (D1, D2):
            res, calls = fwd_day(day)
            assert _core(res) == _core(base)
            assert {c[2]["start"] for c in calls} == {WINDOW[0]} and len(calls) == 11
        res, calls = fwd_day(D3)
        assert _core(res) == _core(base)
        st = res["ohlc_window"]["store"]
        assert {c[2]["start"] for c in calls} == {TAIL_START} and 0 < len(calls) == st["tail_fetches"] < 11
        assert (st["full_fetches"], st["problem"]) == (0, False) and st["served_from_store"] > 0
        assert fwd.status_line(res) == fwd.status_line(_core(res)), "健康时进度行逐字不变"

    def test_degraded_day_sends_far_fewer_requests_direct_and_changes_nothing(self, world, fwd_day):
        """第 3 天补尾全部被拒：已落定段内的请求由库回答。直连次数比「不用库、整段被拒」少，结果与健康时相同。
        这就是本版要修的东西——降级时的直连次数不再随快照天数增长。变异「下载失败时不查库」⇒ 次数相等 ⇒ 红。"""
        healthy, _ = fwd_day(D1, store=False)
        world.fake.wide_fail = lambda t, s, e: "raise" if (s, e) == WINDOW else None
        no_store, _ = fwd_day(D1, store=False)
        world.fake.wide_fail = lambda t, s, e: None
        fwd_day(D1)
        fwd_day(D2)
        world.fake.wide_fail = lambda t, s, e: "raise" if (s, e) == (TAIL_START, WINDOW[1]) else None
        res, _ = fwd_day(D3)
        assert _core(res) == _core(healthy) == _core(no_store)
        ow, ow0 = res["ohlc_window"], no_store["ohlc_window"]
        assert 0 < ow["fallback"] == ow["wide_fetches"] < ow0["fallback"] == 11
        assert 0 < ow["direct_requests"] < ow0["direct_requests"] / 2, (ow["direct_requests"], ow0["direct_requests"])
        assert ow["store"]["served_on_fallback"] + ow["store"]["served_from_store"] > 0
        line = fwd.status_line(res)
        assert line.startswith("⚠️ ") and "次请求由行情库已落定段回答" in line, line

    def test_store_write_failure_is_visible_in_line_and_attention(self, fwd_day):
        """变异「写失败只记日志」⇒ 进度行仍是 ⏳、attention 为空 ⇒ 红。"""
        from hive_logger import PATHS
        healthy, _ = fwd_day(D1, store=False)
        PATHS.replay_ohlc_state.write_text("不是目录")
        res, _ = fwd_day(D1)
        assert _core(res) == _core(healthy)
        line = fwd.status_line(res)
        assert line.startswith("⚠️ ") and "行情库写入失败 11 次" in line, line
        items = rr._forward_test_attention("fg_exposure_gate_forward", rr._detail(res, rr._FWD_DETAIL_KEYS))
        assert [a["id"] for a in items] == ["ic_rerun.fg_exposure_gate_forward.ohlc_window_degraded"]
        assert "回放行情库" in items[0]["message"] and "写入失败 11 次" in items[0]["message"]
        assert "整段取数" not in items[0]["message"], "只因行情库的问题降级时不许说整段取数失败（二次检查）"

    def test_revised_history_is_frozen_alarmed_for_a_week_then_only_noted(self, world, fwd_day):
        """D2 后 Yahoo 改了 NEW2 在重叠段内的一天 ⇒ D3 第一次看到（只记嫌疑、不报），D4 再看到（确认）：两天的结果都与
        改之前相同（冻结）；D4 确认 ⇒ 段首 ⚠️ + attention `ohlc_store_revised`（二次检查：拆股会让 A / B 凭空出场，
        只陈述不够）；过了告警期 ⇒ 回到 ⏳、只陈述。对照：不用库的同一次运行结果确实变了——证明这条测试不是在两个相同世界上比。
        变异「修订覆盖库」⇒ 结果变 ⇒ 红；变异「修订不报 / 不告警 / 永久告警」⇒ 红。"""
        before, _ = fwd_day(D1)
        fwd_day(D2)
        day = "2026-08-24"
        assert TAIL_START <= day <= "2026-08-26"
        o, h, lo, c = world.master["NEW2"][day]
        world.master["NEW2"][day] = (o, h + 30.0, lo, c + 20.0)
        first, _ = fwd_day(D3)
        assert _core(first) == _core(before) and first["ohlc_window"]["store"]["revised_bars"] == 0
        assert "行情库：" not in fwd.status_line(first), "第一次看到只是嫌疑，不报"
        res, _ = fwd_day(D4)
        changed, _ = fwd_day(D4, store=False)
        assert _core(res) == _core(before)
        assert _core(changed) != _core(before), "夹具没起作用：这次修订本来就不改结果"
        assert res["ohlc_window"]["store"]["revised_bars"] == 1
        line = fwd.status_line(res)
        assert line.startswith("⚠️ ") and "NEW2 近 7 天确认了 1 根已落定日线被 Yahoo 改了" in line, line
        items = rr._forward_test_attention("fg_exposure_gate_forward", rr._detail(res, rr._FWD_DETAIL_KEYS))
        assert [(a["id"], a["level"]) for a in items] == [("ic_rerun.fg_exposure_gate_forward.ohlc_store_revised", "warn")]
        # 过了告警期：只陈述、段首回到 ⏳、没有 attention（窗口左端固定，永久 ⚠️ 会把人训练成无视 ⚠️）
        later, _ = fwd_day("2026-09-10")
        assert _core(later) == _core(before) and later["ohlc_window"]["store"]["revised_recent"] == 0
        line = fwd.status_line(later)
        assert line.startswith("⏳ ") and "行情库：NEW2 共 1 根已落定日线 Yahoo 后来改了" in line, line
        assert rr._forward_test_attention("fg_exposure_gate_forward", rr._detail(later, rr._FWD_DETAIL_KEYS)) == []

    def test_total_outage_answered_by_the_store_is_not_called_ohlc_unavailable(self):
        """served=0 且直连全空，但库回答过请求 ⇒ 不是「一根行情都没拿到」。变异「不看 served_on_fallback」⇒ 红。"""
        ohlc = {"served": 0, "direct_requests": 4, "direct_empty": 4}
        assert fwd._ohlc_unavailable(ohlc) is True
        for k in ("served_on_fallback", "served_from_store"):
            assert fwd._ohlc_unavailable({**ohlc, "store": {k: 3}}) is False, k
            assert fwd._ohlc_partly_missing({**ohlc, "store": {k: 3}}) is True, k


# ── 只有前瞻 run() 用它；路径与备份 ─────────────────────────────────────────────────────

class TestOnlyTheForwardRunUsesTheStore:
    def test_only_forward_ohlc_store_constructs_it_and_only_run_calls_that(self):
        """非测试代码里 `ReplayOhlcStore(...)` 只在 `_forward_ohlc_store` 里构造，`_forward_ohlc_store()` 只在 `run`
        里调用（`rehearse` / 生产 `run_for_date` 都不用库）。变异「rehearse 也传库」⇒ 红。"""
        from tests._repo_files import own_python_files
        files, _how = own_python_files(_ROOT)
        ctor, calls = set(), set()
        for p in files:
            rel = p.relative_to(_ROOT)
            if "tests" in rel.parts:
                continue
            src = p.read_text(encoding="utf-8", errors="replace")
            if "ReplayOhlcStore" not in src and "_forward_ohlc_store" not in src:
                continue
            tree = ast.parse(src)
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call):
                        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                        if name == "ReplayOhlcStore":
                            ctor.add((str(rel), fn.name))
                        elif name == "_forward_ohlc_store":
                            calls.add((str(rel), fn.name))
        fg = "experiments/fg_exposure_gate_forward_test.py"
        assert ctor == {(fg, "_forward_ohlc_store")}, ctor
        assert calls == {(fg, "run")}, calls

    def test_insample_refuses_a_store(self, tmp_path):
        with pytest.raises(ValueError, match="样本内"):
            fwd.evaluate([], "2026-08-01", "2026-08-02", tmp_path, insample=True,
                         ohlc_store=ReplayOhlcStore(tmp_path, clock=lambda: dt.date(2026, 9, 1)))

    def test_premise_check_runs_without_the_store_and_restores_it(self, world, monkeypatch):
        """前提核对脚本核「整段切片 ≡ 逐次直连」：开着库会把已落定的日子换成首次落定的值、混淆它，也不该写数据根。
        变异「脚本不关库」⇒ 窗口带着库 / 目录被建出来 ⇒ 红。"""
        from hive_logger import PATHS
        _write_production_records(world)
        monkeypatch.setattr(pp, "_PRICE_CACHE", {})
        monkeypatch.setattr(fwd, "load_seed", lambda *a, **k: SEED)
        monkeypatch.setattr(fwd, "_forward_anchor_plan", _seed_only_plan)
        monkeypatch.setattr(fwd, "FORWARD_START", SINCE)
        prem = _load_premise()
        seen = []
        orig = pp.replay_ohlc_window

        def spy(start, end, **kw):
            seen.append(kw.get("store"))
            return orig(start, end, **kw)
        monkeypatch.setattr(pp, "replay_ohlc_window", spy)
        res = prem.check(today=BEFORE, fwd_module=fwd)
        assert res["status"] == "ok", res
        assert seen == [None] and not PATHS.replay_ohlc_state.exists()
        assert fwd._forward_ohlc_store is _REAL_FORWARD_STORE


class TestPathAndBackup:
    def test_path_is_evaluated_at_call_time(self, monkeypatch, tmp_path):
        from hive_logger import PATHS
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path / "elsewhere"))
        assert PATHS.replay_ohlc_state == tmp_path / "elsewhere" / "replay_ohlc_state"

    def test_store_dir_is_backed_up_moved_and_ignored(self):
        """首次落定后冻结 ⇒ 丢了重取到的可能是修订后的值 ⇒ 不是可重建缓存：要进私有备份、迁移规则与 .gitignore。
        变异「从 STATE_DIRS 拿掉」⇒ 红。"""
        from data_backup import export, migrate_data_root
        assert "replay_ohlc_state" in export.STATE_DIRS
        assert "replay_ohlc_state" in migrate_data_root.MOVE_DIRS
        assert "/replay_ohlc_state/" in (_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
