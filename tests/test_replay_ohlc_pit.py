"""回放行情库的时点数据（v0.45.415，`replay_ohlc_store` + `paper_portfolio._REPLAY_AS_OF`）的守卫。

为什么：重放第 d 天要看到生产在第 d 天运行时 Yahoo 给它的价格。拆股（Yahoo 回溯复权全段历史）后，生产
`_check_exit` 重扫入场以来的全部日线、对比复权前的止损价 ⇒ 多头被记成假止损、出场日倒填（生产自己的 bug，v0.45.416 已修：
生产按日线判口径、把仓位换到复权口径）。只有时点数据能让 A 逐笔复现生产当时看到的价格：v0.45.410 冻结首次落定值会在落定段末端留假跳空、提前假出场；
现取会把拆股前每一笔都按复权价重放。用户 10-05 定：生效日 = 第一次下载到新值的美东日期；缺口日的旧版本只有一次观察，接受。

这里守的是：
  1. 版本按重放日取：生效日之前旧值、之后新值；同一天多个版本按生效日排；不知道重放日 ⇒ 最新版本并计数；没有修订 ⇒ 快路径；
  2. 拆股（全段改值）：嫌疑当天整段补下、之后每次整段直到确认；重叠段之外的老日子也得到版本；缺口日按嫌疑出现前那次下载的
     值落定、确认之前也按它回答；偶发抽风 ⇒ 撤销、不留版本；
  3. v0.45.410 写下的 schema 1 文件（`revisions[d]` 是单个对象）读得进、按时点回答、写回成 schema 2；
  4. `run_replay` 逐日把重放日告诉窗口、结束（含抛异常）后恢复；生产 `run_for_date` 不碰它；
  5. 退回直连的结果里已落定的日子也按时点由库回答，只换值、不增日子（直连失败 ⇒ 仍是 {}）；下载失败后由库回答的请求也按时点；
     没有库时原样返回同一个对象；嫌疑当天整段补下失败 ⇒ 什么都不并、该标的降级；不知道重放日 ⇒ 窗口降级；
  6. 端到端：合成世界里 GAPX 在 08-19 拆股（Yahoo 从那天起回溯复权），生产逐日真跑、在 08-19 把 GAPX 换到复权口径
     （v0.45.416；关掉 416 则记下倒填到 08-11 的假止损——夹具自证）；Step 11 逐日真跑——每一天自证都是 100%、A 记下与生产
     同一笔 GAPX 平仓；关掉 416 时不用库、或不按重放日取版本，自证都掉（时点数据对「口径敏感的下游」必要）；开着 416 时
     即便日线不是时点的，拆股也不再让 A 分叉。
全部离线（假 yfinance）。每条测试的 docstring 写明「哪个变异会让它红」。
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import shutil
import sys
import types
from pathlib import Path

import pytest

import paper_portfolio as pp
import replay_ohlc_store as rs
from replay_ohlc_store import ReplayOhlcStore
from tests import test_replay_ohlc_window as _rw
from tests.test_replay_ohlc_store import W, _bar, _file, _master, _slice, _store

_ROOT = Path(__file__).resolve().parent.parent

#: 复用 v0.45.391 的合成世界（快照、F&G / 波动率库、SEED）
world = _rw.world
fwd = _rw.fwd


def _fetch_like_window(store, yahoo, ticker="AAA", w=W, refetch_ok=True):
    """照窗口的做法取一次：问起点 → 下载 → 补尾里已落定的日子被改了 ⇒ 整段再下一次、只并整段（整段失败 ⇒ 什么都不并，
    返回 None）→ 合并。返回最新版本回答。"""
    fs = store.fetch_start(ticker, *w)
    bars = None if fs is None else {d: dict(b) for d, b in yahoo.items() if fs <= d < w[1]}
    if fs is not None and fs != w[0] and store.revision_in(ticker, fs, w[1], bars):
        store.note_suspect_refetch(refetch_ok)
        if not refetch_ok:
            return None
        fs, bars = w[0], {d: dict(b) for d, b in yahoo.items() if w[0] <= d < w[1]}
    return store.merge(ticker, w[0], w[1], fs, bars)


def _split(master, x, ratio=2.0):
    """拆股后 Yahoo 看到的全段：除权日 x 之前的日线全部除以 ratio（回溯复权）；x 起是拆股后的真实价（同样是 /ratio 的水平）。"""
    return {d: {k: v / ratio for k, v in b.items()} for d, b in master.items()}


def _upto(master, now):
    """「now 那天收盘后」Yahoo 有的日线：日期 ≤ now。"""
    return {d: b for d, b in master.items() if d <= now}


# ── 1. 按重放日取版本 ─────────────────────────────────────────────────────────────

class TestVersionsByReplayDay:
    def _two_versions(self, root):
        """09-15 先在 09-22 改成 555（09-23 确认），再在 09-25 改成 777（09-26 确认）。"""
        m = _master()
        for day in ("2026-09-20", "2026-09-21"):
            _fetch_like_window(_store(root, day), _upto(m, day))
        v1 = {**m, "2026-09-15": _bar(555.0)}
        for day in ("2026-09-22", "2026-09-23", "2026-09-24"):
            _fetch_like_window(_store(root, day), _upto(v1, day))
        v2 = {**m, "2026-09-15": _bar(777.0)}
        for day in ("2026-09-25", "2026-09-26"):
            _fetch_like_window(_store(root, day), _upto(v2, day))
        return m

    def test_each_replay_day_sees_the_version_in_force_that_day(self, tmp_path):
        """变异「按 as_of 取版本时忽略生效日 / 只认最后一个版本」⇒ 红。"""
        m = self._two_versions(tmp_path)
        s = _store(tmp_path, "2026-09-27")
        got = {as_of: s.pit_slice("AAA", "2026-09-15", "2026-09-16", {}, as_of)["2026-09-15"]["Close"]
               for as_of in ("2026-09-21", "2026-09-22", "2026-09-24", "2026-09-25", "2026-09-30")}
        assert got == {"2026-09-21": m["2026-09-15"]["Close"], "2026-09-22": 555.0, "2026-09-24": 555.0,
                       "2026-09-25": 777.0, "2026-09-30": 777.0}
        assert [v["first_seen"] for v in _file(tmp_path)["revisions"]["2026-09-15"]] == ["2026-09-22", "2026-09-25"]
        assert s.stats()["served_older_version"] == 3

    def test_unknown_replay_day_gets_the_latest_version_and_is_counted(self, tmp_path):
        """不知道重放日（没经 run_replay）⇒ 最新版本，`as_of_unknown` 计数。变异「不计」⇒ 红（那就是一次没被看见的口径变化）。"""
        self._two_versions(tmp_path)
        s = _store(tmp_path, "2026-09-27")
        assert s.pit_slice("AAA", "2026-09-15", "2026-09-16", {}, None)["2026-09-15"]["Close"] == 777.0
        assert s.stats()["as_of_unknown"] == 1

    def test_a_clock_that_goes_back_never_writes_versions_out_of_order(self, tmp_path):
        """二次检查（审查者实测）：已有生效日 09-22 的版本后时钟倒退到 09-20 / 09-21、看到另一个新值 ⇒ 原先照样追加生效日 09-20 的版本，
        列表不再升序，下次读库自己把整个文件当坏文件改名（已落定历史全丢）。现在先不确认。变异「不查顺序」⇒ 红。"""
        m = _master()
        for day in ("2026-09-20", "2026-09-21"):
            _fetch_like_window(_store(tmp_path, day), _upto(m, day))
        v1 = {**m, "2026-09-15": _bar(555.0)}
        for day in ("2026-09-22", "2026-09-23"):
            _fetch_like_window(_store(tmp_path, day), _upto(v1, day))
        v2 = {**m, "2026-09-15": _bar(777.0)}
        for day in ("2026-09-20", "2026-09-21"):   # 时钟倒退
            _fetch_like_window(_store(tmp_path, day), _upto(v2, "2026-09-23"))
        s = _store(tmp_path, "2026-09-24")
        s.fetch_start("AAA", *W)
        assert s.stats()["invalid_files"] == []
        firsts = [v["first_seen"] for v in _file(tmp_path)["revisions"]["2026-09-15"]]
        assert firsts == sorted(set(firsts)), firsts

    def test_no_revision_in_range_takes_the_fast_path(self, tmp_path):
        """这段没有任何修订 ⇒ 回答就是基础版本，不计 as_of_unknown、不计旧版本（没有修订时与 v0.45.410 逐根相同）。"""
        self._two_versions(tmp_path)
        s = _store(tmp_path, "2026-09-27")
        assert s.pit_slice("AAA", "2026-09-02", "2026-09-10", {}, None) == _slice(_master(), "2026-09-02", "2026-09-10")
        assert (s.stats()["as_of_unknown"], s.stats()["served_older_version"]) == (0, 0)


# ── 2. 拆股：全段改值 ─────────────────────────────────────────────────────────────

class TestSplitAtStoreLevel:
    """09-20 / 09-21 两次正常下载（落定到 09-19），09-22、09-23 再各一次（落定到 09-21）；09-24 起 Yahoo 回溯复权（2:1）。"""

    X = "2026-09-24"

    def _before(self, root):
        m = _master()
        for day in ("2026-09-20", "2026-09-21", "2026-09-22", "2026-09-23"):
            _fetch_like_window(_store(root, day), _upto(m, day))
        assert _file(root)["settled"]["end"] == "2026-09-21"
        return m, _split(m, self.X)

    def test_suspicion_day_downloads_the_whole_window_and_keeps_the_last_pre_split_download(self, tmp_path):
        """嫌疑当天：补尾后当场整段再下一次（老日子也有第一次观察）；留住嫌疑出现前那次下载（09-23 的）当缺口日旧版本来源；
        确认之前，早于嫌疑日的重放日在缺口日上也用它。变异「不整段补下」/「不留 pre_suspect」/「缺口日用现取值」⇒ 红。"""
        m, post = self._before(tmp_path)
        s = _store(tmp_path, self.X)
        _fetch_like_window(s, _upto(post, self.X))
        f = _file(tmp_path)
        assert s.stats()["suspect_refetches"] == 1 and s.stats()["full_fetches"] == 1
        assert "2026-09-02" in f["revision_suspects"], "重叠段之外的老日子也在整段补下时记了嫌疑"
        assert f["pre_suspect"]["fetched_on"] == "2026-09-23" and f["revisions"] == {}
        # 缺口日 09-22 / 09-23（还没落定）：重放日 09-23 用 09-23 下载的值，不是拆股后的现取值
        got = s.pit_slice("AAA", "2026-09-22", "2026-09-24", {"2026-09-22": post["2026-09-22"]}, "2026-09-23")
        assert got == _slice(m, "2026-09-22", "2026-09-24")

    def test_confirmation_versions_every_date_and_settles_gap_days_at_their_old_value(self, tmp_path):
        """第二天整段确认：窗口里每个已落定日期都有生效日 09-24 的新版本；缺口日按旧值落定、新值记成版本；pre_suspect 清掉。
        重放日 09-23 看到的全段 = 拆股前；09-24 起 = 复权后。变异「缺口日按新值落定」⇒ 09-23 看到复权价 ⇒ 红。"""
        m, post = self._before(tmp_path)
        _fetch_like_window(_store(tmp_path, self.X), _upto(post, self.X))
        s = _store(tmp_path, "2026-09-25")
        assert s.fetch_start("AAA", *W) == W[0], "嫌疑未决 ⇒ 整段"
        _fetch_like_window(s, _upto(post, "2026-09-25"))
        f = _file(tmp_path)
        assert f["settled"]["end"] == "2026-09-23" and f["pre_suspect"] is None and f["revision_suspects"] == {}
        assert s.stats()["gap_days_settled"] == 2
        assert {v[0]["first_seen"] for v in f["revisions"].values()} == {self.X}
        before = s.pit_slice("AAA", "2026-09-01", "2026-09-24", {}, "2026-09-23")
        after = s.pit_slice("AAA", "2026-09-01", "2026-09-24", {}, self.X)
        assert before == _slice(m, "2026-09-01", "2026-09-24")
        assert after == _slice(post, "2026-09-01", "2026-09-24")

    def test_a_gap_day_whose_two_post_split_looks_disagree_waits_for_confirmation(self, tmp_path):
        """缺口日 09-23：嫌疑当天那次整段下载里它的复权值是个怪数，确认当天才是正常复权值 ⇒ 两次「改了之后」的观察不一致：
        按旧值落定、不记版本、记嫌疑（seen_on = 确认当天），下一次再确认；另一个缺口日 09-22 两次一致 ⇒ 当场有版本。
        变异「缺口日新版本不要求两次观察」⇒ 09-23 直接拿到版本 ⇒ 红（变异 P18 存活后补）。"""
        m, post = self._before(tmp_path)
        wobbly = {**post, "2026-09-23": _bar(999.0)}
        _fetch_like_window(_store(tmp_path, self.X), _upto(wobbly, self.X))
        _fetch_like_window(_store(tmp_path, "2026-09-25"), _upto(post, "2026-09-25"))
        f = _file(tmp_path)
        assert f["settled"]["bars"]["2026-09-23"] == m["2026-09-23"], "基础版本 = 嫌疑出现前那次下载的旧值"
        assert "2026-09-23" not in f["revisions"] and f["revision_suspects"]["2026-09-23"]["seen_on"] == "2026-09-25"
        assert f["revisions"]["2026-09-22"][0]["first_seen"] == self.X
        s = _store(tmp_path, "2026-09-26")
        _fetch_like_window(s, _upto(post, "2026-09-26"))
        assert _file(tmp_path)["revisions"]["2026-09-23"][0]["first_seen"] == "2026-09-25"

    def test_failed_whole_refetch_merges_nothing_so_the_whole_series_switches_on_one_day(self, tmp_path):
        """二次检查（审查者实测）：嫌疑当天补尾成功、整段补下失败。原先先并补尾 ⇒ 重叠段生效日 09-24、更早的日子 09-25 ⇒
        重放日 09-24 永久读到一半复权前、一半复权后的序列。现在整段失败 ⇒ 什么都不并（库文件一字不变），下次补尾再看到、
        再整段 ⇒ 全段生效日同一天（晚一天，已知局限）。变异「先并补尾再整段」⇒ 生效日两个值 ⇒ 红。"""
        m, post = self._before(tmp_path)
        before = _file(tmp_path)
        s = _store(tmp_path, self.X)
        assert _fetch_like_window(s, _upto(post, self.X), refetch_ok=False) is None
        assert _file(tmp_path) == before and s.stats()["suspect_refetch_failed"] == 1
        for day in ("2026-09-25", "2026-09-26"):
            _fetch_like_window(_store(tmp_path, day), _upto(post, day))
        f = _file(tmp_path)
        assert {v[0]["first_seen"] for v in f["revisions"].values()} == {"2026-09-25"}
        s = _store(tmp_path, "2026-09-27")
        assert s.pit_slice("AAA", "2026-09-01", "2026-09-24", {}, self.X) == _slice(m, "2026-09-01", "2026-09-24")
        assert s.pit_slice("AAA", "2026-09-01", "2026-09-24", {}, "2026-09-25") == _slice(post, "2026-09-01", "2026-09-24")

    def test_window_refetch_failure_degrades_the_ticker_visibly(self, tmp_path, monkeypatch):
        """窗口层：整段补下失败 ⇒ 该标的本次降级（`fallback_tickers` 有它、`degraded` 为真——谁会红：进度行 ⚠️ + attention），
        库文件不动；成功时整段补下不计入 `wide_fetches`（进度行读作「N 个标的」），另由 `suspect_refetches` 计。
        变异「失败只计数」⇒ degraded 为假 ⇒ 红。"""
        m, post = self._before(tmp_path)
        before = _file(tmp_path)
        rows = {d: (b["Open"], b["High"], b["Low"], b["Close"]) for d, b in _upto(post, self.X).items()}
        monkeypatch.setattr(pp, "_PRICE_CACHE", {})
        monkeypatch.setattr(pp, "_REPLAY_AS_OF", "2026-09-23")
        fake = _rw.FakeYF({"AAA": rows}, wide_fail=lambda t, st, e: "raise" if st == W[0] and e == W[1] else None)
        monkeypatch.setitem(sys.modules, "yfinance", fake.module())
        with pp.replay_ohlc_window(*W, store=_store(tmp_path, self.X)) as win:
            pp._fetch_ohlc("AAA", "2026-09-14", "2026-09-25")
        st = win.stats()
        assert "AAA" in st["fallback_tickers"] and "整段补下失败" in st["fallback_tickers"]["AAA"] and st["degraded"]
        assert st["store"]["suspect_refetch_failed"] == 1 and _file(tmp_path) == before
        fake = _rw.FakeYF({"AAA": rows})
        monkeypatch.setitem(sys.modules, "yfinance", fake.module())
        with pp.replay_ohlc_window(*W, store=_store(tmp_path, self.X)) as win:
            pp._fetch_ohlc("AAA", "2026-09-14", "2026-09-25")
        st = win.stats()
        assert (st["wide_fetches"], st["store"]["suspect_refetches"], len(fake.calls)) == (1, 1, 2)
        assert not st["degraded"] and "2026-09-02" in _file(tmp_path)["revision_suspects"]

    def test_same_et_day_rerun_before_and_after_the_split_keeps_the_pre_split_download(self, tmp_path):
        """二次检查（审查者实测）：同一美东日先跑一次（拆股前）、再跑一次（拆股后）——夜里补跑与次日例行 Step 11 正是同一美东日
        （10-05 实测）。原先只认「更早美东日期」的上一次下载 ⇒ 缺口日拿不到旧值、按复权价落定。现在认「与已落定段一致」的那次。
        变异「pre_suspect 仍要求 fetched_on < today」⇒ 红。"""
        m, post = self._before(tmp_path)
        _fetch_like_window(_store(tmp_path, self.X), _upto(m, "2026-09-23"))   # 同一美东日、开盘前：还没复权
        _fetch_like_window(_store(tmp_path, self.X), _upto(post, self.X))      # 同一美东日、收盘后：已复权
        f = _file(tmp_path)
        assert f["pre_suspect"]["fetched_on"] == self.X and f["pre_suspect"]["bars"]["2026-09-23"] == m["2026-09-23"]
        s = _store(tmp_path, "2026-09-25")
        _fetch_like_window(s, _upto(post, "2026-09-25"))
        assert s.stats()["gap_days_settled"] >= 1 and s.stats()["gap_days_unobserved"] == 0
        assert s.pit_slice("AAA", "2026-09-01", "2026-09-24", {}, "2026-09-23") == _slice(m, "2026-09-01", "2026-09-24")
        assert s.pit_slice("AAA", "2026-09-01", "2026-09-24", {}, self.X) == _slice(post, "2026-09-01", "2026-09-24")

    def test_gap_day_without_a_pre_change_observation_is_recorded_and_reported(self, tmp_path):
        """拆股前一天恰好漏了一根已落定日线：嫌疑（漏数据）那天留住的是再前一天的下载，覆盖不到拆股前最后一天 ⇒ 那天只能按新值。
        结构上分不出「漏数据」与「拆股」，所以不猜：记进 `unobserved_gap_days`、只要在窗口里就报（进度行陈述）。
        变异「不记」⇒ 红。"""
        m = _master()
        for day in ("2026-09-20", "2026-09-21", "2026-09-22"):
            _fetch_like_window(_store(tmp_path, day), _upto(m, day))
        glitch = {d: b for d, b in m.items() if d != "2026-09-15"}
        _fetch_like_window(_store(tmp_path, "2026-09-23"), _upto(glitch, "2026-09-23"))
        post = _split(m, self.X)
        _fetch_like_window(_store(tmp_path, self.X), _upto(post, self.X))
        s = _store(tmp_path, "2026-09-25")
        _fetch_like_window(s, _upto(post, "2026-09-25"))
        assert _file(tmp_path)["unobserved_gap_days"] == ["2026-09-23"] and s.stats()["gap_days_unobserved"] == 1
        st = _store(tmp_path, "2026-09-26")
        st.fetch_start("AAA", *W)
        stats = st.stats(*W)
        assert (stats["unobserved_gap_days"], stats["unobserved_gap_tickers"]) == (1, ["AAA"])
        assert "缺口日没有拆股" in fwd._ohlc_gap_unobserved_note({"ohlc_window": {"store": stats}})
        assert s.pit_slice("AAA", "2026-09-21", "2026-09-23", {}, "2026-09-22") == _slice(m, "2026-09-21", "2026-09-23")

    def test_no_pre_change_download_at_all_records_every_gap_day(self, tmp_path):
        """嫌疑早已在、却没有 `pre_suspect`（如 0.45.410 写下、已带嫌疑的 schema 1 文件）：上一次下载已是改之后的，不能当旧值；
        确认时缺口日全记进 `unobserved_gap_days`。变异「ps 为 None 时照旧要它」⇒ 崩 / 不记 ⇒ 红。"""
        m, post = self._before(tmp_path)
        _fetch_like_window(_store(tmp_path, self.X), _upto(post, self.X))
        f = _file(tmp_path)
        f["pre_suspect"] = None
        (tmp_path / "AAA.json").write_text(json.dumps(f), encoding="utf-8")
        # 同一天再跑一次：嫌疑未决、上一次下载已是复权后的——不许把它当「改之前」留住（变异「一致性判断恒真」⇒ 红）
        _fetch_like_window(_store(tmp_path, self.X), _upto(post, self.X))
        assert _file(tmp_path)["pre_suspect"] is None
        s = _store(tmp_path, "2026-09-25")
        _fetch_like_window(s, _upto(post, "2026-09-25"))
        f = _file(tmp_path)
        assert f["unobserved_gap_days"] == ["2026-09-22", "2026-09-23"] and s.stats()["gap_days_unobserved"] == 2
        assert f["pre_suspect"] is None and f["revision_suspects"] == {}

    def test_open_suspect_pauses_settlement_so_gap_days_wait_for_their_old_value(self, tmp_path):
        """二次检查（审查者实测）：拆股后第二天什么都没确认（老日子与昨天差一点），而缺口日两次复权后的观察一致 ⇒ 原先 ② 照常
        按复权价落定缺口日、pre_suspect 随即作废。现在有未决嫌疑就暂停落定；第三天同一个复权值再出现 ⇒ 确认，生效日仍是第一次
        出现那天，缺口日按旧值。变异「② 不看嫌疑」⇒ 红；变异「嫌疑不记之前的值」⇒ 生效日变成 09-25 ⇒ 红。"""
        m, post = self._before(tmp_path)
        _fetch_like_window(_store(tmp_path, self.X), _upto(post, self.X))
        jitter = {d: ({k: v + 0.01 for k, v in b.items()} if d <= "2026-09-21" else b) for d, b in post.items()}
        _fetch_like_window(_store(tmp_path, "2026-09-25"), _upto(jitter, "2026-09-25"))
        f = _file(tmp_path)
        assert f["settled"]["end"] == "2026-09-21" and f["revisions"] == {} and f["pre_suspect"]
        # 嫌疑出现日按第一次看到改动那天（09-24）算，不是当前嫌疑值那天（09-25）：重放日 09-24 的缺口日已是复权后
        gap_now = _slice(post, "2026-09-22", "2026-09-24")
        s25 = _store(tmp_path, "2026-09-25")
        assert s25.pit_slice("AAA", "2026-09-22", "2026-09-24", gap_now, self.X) == gap_now
        assert s25.pit_slice("AAA", "2026-09-22", "2026-09-24", gap_now, "2026-09-23") == _slice(m, "2026-09-22", "2026-09-24")
        s = _store(tmp_path, "2026-09-26")
        _fetch_like_window(s, _upto(post, "2026-09-26"))
        f = _file(tmp_path)
        assert {v[0]["first_seen"] for v in f["revisions"].values()} == {self.X}
        assert s.pit_slice("AAA", "2026-09-01", "2026-09-24", {}, "2026-09-23") == _slice(m, "2026-09-01", "2026-09-24")
        assert s.pit_slice("AAA", "2026-09-01", "2026-09-24", {}, self.X) == _slice(post, "2026-09-01", "2026-09-24")

    def test_a_dropped_bar_on_confirmation_day_does_not_move_that_dates_effective_day(self, tmp_path):
        """二次检查（审查者实测）：确认当天恰好漏了 09-10 ⇒ 原先嫌疑被换成「没有日线」、生效日后挪两天，版本永久错位（重放日 09-24
        看到一根拆股前的价夹在复权价里）。现在嫌疑记住之前的值，复权值再出现即确认、生效日 = 第一次出现那天。变异 ⇒ 红。"""
        m, post = self._before(tmp_path)
        _fetch_like_window(_store(tmp_path, self.X), _upto(post, self.X))
        dropped = {d: b for d, b in post.items() if d != "2026-09-10"}
        _fetch_like_window(_store(tmp_path, "2026-09-25"), _upto(dropped, "2026-09-25"))
        assert "2026-09-10" not in _file(tmp_path)["revisions"]
        s = _store(tmp_path, "2026-09-26")
        _fetch_like_window(s, _upto(post, "2026-09-26"))
        assert _file(tmp_path)["revisions"]["2026-09-10"][0]["first_seen"] == self.X
        assert s.pit_slice("AAA", "2026-09-01", "2026-09-24", {}, self.X) == _slice(post, "2026-09-01", "2026-09-24")

    def test_a_one_off_glitch_leaves_no_version_and_no_pre_suspect(self, tmp_path):
        """09-24 那次响应漏了 09-15，09-25 恢复 ⇒ 嫌疑撤销、pre_suspect 丢掉、缺口日照常落定、没有任何版本。
        变异「嫌疑撤销后不丢 pre_suspect」⇒ 红（之后的重放日还会被它改写）。"""
        m, _ = self._before(tmp_path)
        glitch = {d: b for d, b in m.items() if d != "2026-09-15"}
        _fetch_like_window(_store(tmp_path, self.X), _upto(glitch, self.X))
        _fetch_like_window(_store(tmp_path, "2026-09-25"), _upto(m, "2026-09-25"))
        f = _file(tmp_path)
        assert f["revisions"] == {} and f["revision_suspects"] == {} and f["pre_suspect"] is None
        assert f["settled"]["end"] == "2026-09-23"


# ── 3. schema 1（v0.45.410 写下的文件）───────────────────────────────────────────

class TestSchema1Files:
    def test_v410_file_is_read_answered_by_replay_day_and_written_back_as_schema_2(self, tmp_path):
        """今天（10-05）生产会用 v0.45.410 写下 schema 1 的库文件（`revisions[d]` 是单个对象）。新代码必须读得进（否则全被当坏文件
        改名、进度行永久 ⚠️），按时点回答，写回成 schema 2。变异「只认 schema 2」⇒ 红。"""
        m = _master()
        entry = {"schema": 1, "ticker": "AAA", "pending": None, "revision_suspects": {},
                 "settled": {"start": "2026-09-01", "end": "2026-09-19", "bars": _slice(m, "2026-09-01", "2026-09-20")},
                 "revisions": {"2026-09-15": {"first_seen": "2026-09-16", "confirmed_on": "2026-09-17",
                                              "yahoo": _bar(555.0)}}}
        tmp_path.joinpath("AAA.json").write_text(json.dumps(entry), encoding="utf-8")
        s = _store(tmp_path, "2026-09-22")
        assert s.pit_slice("AAA", "2026-09-15", "2026-09-16", {}, "2026-09-15")["2026-09-15"] == m["2026-09-15"]
        assert s.settled_slice("AAA", "2026-09-15", "2026-09-16", as_of="2026-09-16")["2026-09-15"] == _bar(555.0)
        assert s.stats()["invalid_files"] == [] and s.stats()["problem"] is False
        _fetch_like_window(s, _upto({**m, "2026-09-15": _bar(555.0)}, "2026-09-22"))
        f = _file(tmp_path)
        assert f["schema"] == 2 and isinstance(f["revisions"]["2026-09-15"], list) and "pre_suspect" in f

    @pytest.mark.parametrize("mutate", ["versions-not-ascending", "empty-version-list", "bad-pre-suspect",
                                        "version-without-yahoo", "suspect-without-yahoo", "earlier-too-long",
                                        "gaps-not-a-list"])
    def test_malformed_versions_are_quarantined(self, tmp_path, mutate):
        m = _master()
        entry = {"schema": 2, "ticker": "AAA", "pending": None, "revision_suspects": {}, "pre_suspect": None,
                 "settled": {"start": "2026-09-01", "end": "2026-09-19", "bars": _slice(m, "2026-09-01", "2026-09-20")},
                 "revisions": {}}
        if mutate == "versions-not-ascending":
            entry["revisions"]["2026-09-15"] = [{"first_seen": "2026-09-18", "yahoo": None},
                                                {"first_seen": "2026-09-17", "yahoo": None}]
        elif mutate == "empty-version-list":
            entry["revisions"]["2026-09-15"] = []
        elif mutate == "version-without-yahoo":   # 二次检查：原先能过校验、之后 KeyError（每次运行都崩、从不改名留证）
            entry["revisions"]["2026-09-15"] = [{"first_seen": "2026-09-17", "confirmed_on": "2026-09-18"}]
        elif mutate == "suspect-without-yahoo":
            entry["revision_suspects"]["2026-09-15"] = {"seen_on": "2026-09-18"}
        elif mutate == "earlier-too-long":
            entry["revision_suspects"]["2026-09-15"] = {"seen_on": "2026-09-18", "yahoo": None,
                                                        "earlier": [{"seen_on": "2026-09-17", "yahoo": None}] * 9}
        elif mutate == "gaps-not-a-list":
            entry["unobserved_gap_days"] = "2026-09-18"
        else:
            entry["pre_suspect"] = {"fetched_on": "2026-09-18", "start": "2026-09-20", "end": "2026-09-10", "bars": {}}
        tmp_path.joinpath("AAA.json").write_text(json.dumps(entry), encoding="utf-8")
        s = _store(tmp_path, "2026-09-22")
        assert s.fetch_start("AAA", *W) == W[0] and len(s.stats()["invalid_files"]) == 1


# ── 4. run_replay 告诉窗口当前重放日 ──────────────────────────────────────────────

class TestReplayDayPlumbing:
    def test_run_replay_publishes_each_day_and_restores(self, tmp_path, monkeypatch):
        """变异「run_replay 不设 / 设错 / 不恢复」⇒ 红。"""
        seen = []
        monkeypatch.setattr(pp, "run_for_date", lambda d, verbose=False: seen.append((d, pp._REPLAY_AS_OF)))
        pp._REPLAY_AS_OF = "sentinel"
        try:
            pp.run_replay({}, tmp_path / "s", dates=["2026-08-12", "2026-08-13"])
            assert seen == [("2026-08-12", "2026-08-12"), ("2026-08-13", "2026-08-13")]
            assert pp._REPLAY_AS_OF == "sentinel"

            def boom(d, verbose=False):
                raise RuntimeError("测试：重放中途抛")
            monkeypatch.setattr(pp, "run_for_date", boom)
            with pytest.raises(RuntimeError):
                pp.run_replay({}, tmp_path / "t", dates=["2026-08-12"])
            assert pp._REPLAY_AS_OF == "sentinel"
        finally:
            pp._REPLAY_AS_OF = None

    def test_unknown_replay_day_on_a_versioned_bar_degrades_the_window(self, tmp_path, monkeypatch):
        """`as_of_unknown` 应恒为 0；不为 0 ⇒ 时点数据在那几次请求上失效。二次检查：原先只进 stderr 汇总行——现在让窗口
        `degraded` 为真、进度行写出原因（谁会红）。变异「degraded 不看 as_of_unknown」⇒ 红。"""
        m = _master()
        for day in ("2026-09-20", "2026-09-21"):
            _fetch_like_window(_store(tmp_path, day), _upto(m, day))
        revised = {**m, "2026-09-15": _bar(555.0)}
        for day in ("2026-09-22", "2026-09-23"):
            _fetch_like_window(_store(tmp_path, day), _upto(revised, day))
        monkeypatch.setattr(pp, "_REPLAY_AS_OF", None)
        with pp.replay_ohlc_window(*W, store=_store(tmp_path, "2026-09-24")) as win:
            pp._fetch_ohlc("AAA", "2026-09-14", "2026-09-17")
        st = win.stats()
        assert st["store"]["as_of_unknown"] == 1 and st["degraded"] is True and st["fallback"] == 0
        note = fwd._ohlc_window_note({"ohlc_window": st})
        assert "不知道重放日" in note and "整段取数失败" not in note, note

    def test_only_run_replay_writes_it_and_production_run_for_date_never_reads_it(self):
        """重放日只由 `run_replay` 写；生产 `run_for_date` 及其调用的生产函数不碰它（时点数据只活在回放里）。"""
        src = (_ROOT / "paper_portfolio.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        writers = {fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
                   for n in ast.walk(fn) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "_REPLAY_AS_OF" for t in n.targets)}
        assert writers == {"run_replay"}, writers
        funcs = {n.name: ast.get_source_segment(src, n) for n in tree.body if isinstance(n, ast.FunctionDef)}
        for name in ("run_for_date", "_check_exit", "_mark_to_market", "_open_position", "main"):
            assert "_REPLAY_AS_OF" not in funcs[name], name


# ── 5. 退回直连时已落定的日子也按时点 ─────────────────────────────────────────────

class TestDirectFallbackIsPointInTime:
    def test_settled_dates_in_a_direct_answer_come_from_the_store(self, tmp_path, monkeypatch):
        """补尾下载失败、请求跨过已落定段末尾 ⇒ 直连拿到的是 Yahoo 现在的值；其中已落定的日子要换成库按重放日的值——
        否则修订与降级同时出现时同一天两个值。变异「直连结果原样返回」⇒ 红。"""
        m = _master()
        for day in ("2026-09-20", "2026-09-21"):
            _fetch_like_window(_store(tmp_path, day), _upto(m, day))
        revised = {**m, "2026-09-15": _bar(555.0)}
        for day in ("2026-09-22", "2026-09-23"):
            _fetch_like_window(_store(tmp_path, day), _upto(revised, day))
        fake = _rw.FakeYF({"AAA": {d: (b["Open"], b["High"], b["Low"], b["Close"]) for d, b in revised.items()}},
                          wide_fail=lambda t, s, e: "raise" if e == W[1] else None)
        monkeypatch.setitem(sys.modules, "yfinance", fake.module())
        monkeypatch.setattr(pp, "_PRICE_CACHE", {})
        monkeypatch.setattr(pp, "_REPLAY_AS_OF", "2026-09-21")
        with pp.replay_ohlc_window(*W, store=_store(tmp_path, "2026-09-24")) as win:
            got = pp._fetch_ohlc("AAA", "2026-09-14", "2026-09-25")
        assert win.stats()["direct_requests"] == 1
        assert got["2026-09-15"] == m["2026-09-15"], "重放日 09-21 早于生效日 09-22：旧值"
        assert got["2026-09-24"] == revised["2026-09-24"], "未落定的日子照用直连"

    def _settled_and_revised(self, root):
        m = _master()
        for day in ("2026-09-20", "2026-09-21"):
            _fetch_like_window(_store(root, day), _upto(m, day))
        revised = {**m, "2026-09-15": _bar(555.0)}
        for day in ("2026-09-22", "2026-09-23"):
            _fetch_like_window(_store(root, day), _upto(revised, day))
        return m, revised

    @pytest.mark.parametrize("how", ["direct-fails", "direct-short"])
    def test_overlay_never_adds_days_the_direct_answer_did_not_have(self, tmp_path, monkeypatch, how):
        """二次检查（审查者实测）：直连失败 / 拿得短时，原先库会把直连没拿到的已落定日子补进来——没有修订时也与 v0.45.410
        不同（410 返回 {} ⇒ 仓位不出场、按入场价估值），且计数说「一根都没拿到」而重放其实用了行情。现在只换值、不增日子。
        变异「overlay 返回 pit_slice 全部」⇒ 红。"""
        m = _master()
        for day in ("2026-09-20", "2026-09-21"):
            _fetch_like_window(_store(tmp_path, day), _upto(m, day))
        rows = {d: (b["Open"], b["High"], b["Low"], b["Close"]) for d, b in m.items()
                if how == "direct-short" and not "2026-09-14" <= d <= "2026-09-16"}
        fake = _rw.FakeYF({"AAA": rows}, wide_fail=lambda t, st, e: "raise" if how == "direct-fails" or e == W[1] else None)
        monkeypatch.setitem(sys.modules, "yfinance", fake.module())
        monkeypatch.setattr(pp, "_PRICE_CACHE", {})
        monkeypatch.setattr(pp, "_REPLAY_AS_OF", "2026-09-21")
        with pp.replay_ohlc_window(*W, store=_store(tmp_path, "2026-09-22")) as win:
            got = pp._fetch_ohlc("AAA", "2026-09-14", "2026-09-25")
        want = {} if how == "direct-fails" else _slice({d: _bar(rows[d][3]) for d in rows}, "2026-09-14", "2026-09-25")
        assert got == want
        assert (win.stats()["direct_requests"], win.stats()["direct_empty"]) == (1, 1 if how == "direct-fails" else 0)

    def test_settled_request_after_a_failed_download_is_point_in_time(self, tmp_path, monkeypatch):
        """下载失败后、整个落在已落定段内的请求由库回答——也要按重放日取版本（`_settled_on_fallback` 传 as_of）。
        变异「那里传 as_of=None」⇒ 拿到 555 ⇒ 红（审查者实测原先四个测试文件都不红）。"""
        m, revised = self._settled_and_revised(tmp_path)
        fake = _rw.FakeYF({"AAA": {}}, wide_fail=lambda t, st, e: "raise")
        monkeypatch.setitem(sys.modules, "yfinance", fake.module())
        monkeypatch.setattr(pp, "_PRICE_CACHE", {})
        monkeypatch.setattr(pp, "_REPLAY_AS_OF", "2026-09-21")
        with pp.replay_ohlc_window(*W, store=_store(tmp_path, "2026-09-24")) as win:
            pp._fetch_ohlc("AAA", "2026-09-14", "2026-09-25")          # 碰到未落定日子 ⇒ 下载 ⇒ 失败 ⇒ 该标的降级
            got = pp._fetch_ohlc("AAA", "2026-09-14", "2026-09-17")    # 整个落在已落定段内 ⇒ 库回答
        assert "AAA" in win.stats()["fallback_tickers"] and win.stats()["store"]["served_on_fallback"] == 1
        assert got["2026-09-15"] == m["2026-09-15"], "重放日 09-21 早于生效日 09-22：旧值"

    def test_non_iso_request_with_a_store_does_not_raise(self, tmp_path, monkeypatch):
        """窗口外 / 非 YYYY-MM-DD 的请求走原直连路径，410 对它们不抛；overlay 也不许抛（`_shift` 解析不了 '2026-09-9'）。"""
        m, revised = self._settled_and_revised(tmp_path)
        fake = _rw.FakeYF({"AAA": {d: (b["Open"], b["High"], b["Low"], b["Close"]) for d, b in revised.items()}})
        monkeypatch.setitem(sys.modules, "yfinance", fake.module())
        monkeypatch.setattr(pp, "_PRICE_CACHE", {})
        with pp.replay_ohlc_window(*W, store=_store(tmp_path, "2026-09-24")) as win:
            got = pp._fetch_ohlc("AAA", "2026-09-02", "2026-09-9")
        assert win.stats()["out_of_window"] == 1 and "2026-09-08" in got

    def test_without_a_store_the_direct_answer_is_the_same_object(self):
        win = pp._ReplayOhlcWindow(*W)
        out = {"2026-09-02": _bar(1.0)}
        assert win.overlay_direct("AAA", "2026-09-01", "2026-09-05", out) is out


# ── 6. 端到端：拆股前后逐日跑生产与 Step 11 ─────────────────────────────────────────

SPLIT_DAY = "2026-08-19"   # GAPX 2:1 拆股的除权日（Yahoo 从这天起回溯复权）


class _TimeYF:
    """随「现在」变化的 Yahoo：只给日期 ≤ now 的日线；GAPX 在 now ≥ SPLIT_DAY 时全段 /2（回溯复权 + 拆股后真实价）。

    v0.45.416：同真 yfinance，响应带「Stock Splits」列（GAPX 在 now ≥ SPLIT_DAY 时除权日那行是 2.0）；不带 `end` 的请求
    （`paper_portfolio._fetch_split_events` 查拆股记录）给到「现在」为止。"""

    def __init__(self, master):
        self.master = master
        self.now = None
        self.calls = []
        self.wide_fail = lambda t, s, e: None   # 与 FakeYF 同名属性，免得别处读到

    def module(self):
        return types.SimpleNamespace(Ticker=self._ticker)

    def _ticker(self, t):
        fake = self

        class _T:
            def history(self, *args, **kw):
                import pandas as pd
                fake.calls.append((t, args, dict(kw)))
                lo = pd.Timestamp(kw["start"]).strftime("%Y-%m-%d")
                hi = pd.Timestamp(kw["end"]).strftime("%Y-%m-%d") if kw.get("end") is not None else "9999-12-31"
                split_on = t == "GAPX" and fake.now >= SPLIT_DAY
                r = 2.0 if split_on else 1.0
                rows = sorted((d, v) for d, v in fake.master.get(t, {}).items() if lo <= d < hi and d <= fake.now)
                rows = [(d, [x / r if isinstance(x, float) else x for x in v] + [r if split_on and d == SPLIT_DAY else 0.0])
                        for d, v in rows]
                if not rows:
                    return pd.DataFrame()
                return pd.DataFrame([v for _, v in rows], columns=["Open", "High", "Low", "Close", "Stock Splits"],
                                    index=pd.to_datetime([d for d, _ in rows]))
        return _T()


@pytest.fixture
def split_world(world, monkeypatch):
    """生产逐日真跑（每天看那天的 Yahoo）→ 生产记录；之后由测试逐日跑 Step 11。"""
    yf = _TimeYF(world.master)
    monkeypatch.setitem(sys.modules, "yfinance", yf.module())
    prod = _run_production(yf, world.tmp / "prod_run", monkeypatch)
    monkeypatch.setattr(fwd, "load_seed", lambda *a, **k: _rw.SEED)
    monkeypatch.setattr(fwd, "_forward_anchor_plan", _rw._seed_only_plan)
    monkeypatch.setattr(fwd, "FORWARD_START", _rw.SINCE)

    def step11(day, *, store=True):
        yf.now = day
        monkeypatch.setattr(pp, "_PRICE_CACHE", {})
        monkeypatch.setattr(pp, "_SPLIT_EVENTS_CACHE", {})
        monkeypatch.setattr(rs, "_et_today", lambda: dt.date.fromisoformat(day))
        monkeypatch.setattr(fwd, "_forward_ohlc_store", _REAL_FORWARD_STORE if store else (lambda: None))
        return fwd.run(today=day)
    return types.SimpleNamespace(yf=yf, prod=prod, step11=step11, monkeypatch=monkeypatch)


_REAL_FORWARD_STORE = fwd._forward_ohlc_store


def _run_production(yf, prod, monkeypatch):
    """生产逐日真跑（每天看那天的 Yahoo、每天一个新进程），记录装成「生产实际记录」。返回状态目录。"""
    prod.mkdir()
    for name, blob in _rw.SEED.items():
        (prod / name).write_bytes(blob)
    for d in _rw.DATES:
        yf.now = d
        monkeypatch.setattr(pp, "_PRICE_CACHE", {})
        monkeypatch.setattr(pp, "_SPLIT_EVENTS_CACHE", {})
        pp.run_replay({}, prod, dates=[d])   # 没有窗口：每天就是生产那天的样子
    pp.CLOSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(prod / "closed_trades.jsonl", pp.CLOSED_FILE)
    shutil.copy(prod / "positions.jsonl", pp.POSITIONS_FILE)
    return prod


def _without_416(monkeypatch):
    """关掉 v0.45.416 的拆股口径（= 416 之前的 paper_portfolio：日线怎么给就怎么对绝对价位）——对照用。"""
    monkeypatch.setattr(pp, "_reconcile_split_basis", lambda *a, **k: {"status": "ok"})


def _gapx_closed(prod):
    return [json.loads(x) for x in (prod / "closed_trades.jsonl").read_text().splitlines() if '"GAPX"' in x]


class TestSplitEndToEnd:
    def test_production_rescales_gapx_on_split_day_instead_of_faking_a_stop(self, split_world):
        """夹具自证（v0.45.416 起）：生产在 08-19 看到复权后日线，把 08-10 入场的 GAPX 多头换到复权口径（入场价 100 → 50），
        之后按复权口径走到时间止损——不再记倒填到 08-11 的假止损。没有这一条，下面的「自证 100%」可能只是因为拆股根本没碰到
        任何仓位。变异「桩不给 Stock Splits 列」⇒ 拆股记录查不到、GAPX 判不了 ⇒ 红。"""
        (t,) = _gapx_closed(split_world.prod)
        assert t["split_adjustments"] == [{"ex_date": SPLIT_DAY, "ratio": 2.0, "applied_as_of": SPLIT_DAY}]
        assert t["entry_price"] == pytest.approx(50.0)
        assert t["exit_reason"] == "TIME" and t["exit_date"] > SPLIT_DAY

    def test_without_416_production_books_the_backdated_fake_stop(self, split_world, monkeypatch):
        """对照：关掉 416，同一份世界里生产在 08-19 把 GAPX 记成 08-11 出场的止损（倒填，v0.45.415 时的夹具自证原样）——
        证明上一条的「没有假单」来自 416，且拆股确实碰到了在场仓位。"""
        _without_416(monkeypatch)
        prod = _run_production(split_world.yf, split_world.prod.parent / "prod_without_416", monkeypatch)
        (t,) = _gapx_closed(prod)
        assert (t["exit_reason"], t["exit_date"], t["exit_price"]) == ("SL", "2026-08-11", 93.0)

    def test_self_proof_stays_100_percent_through_the_split_every_day(self, split_world):
        """Step 11 逐日跑（08-13 起，含拆股当天 08-19 的嫌疑、08-20 的确认），每一天自证都 100%；拆股当天整段补下了 GAPX，
        确认那天 GAPX 有版本、缺口日按旧值落定。变异「缺口日用现取值」⇒ 08-19 那天红；「不按重放日取版本」⇒ 08-20 起红。"""
        days = _rw.DATES[1:] + ["2026-08-28"]
        stats = {}
        for day in days:
            res = split_world.step11(day)
            assert res.get("selfproof_rate") == 1.0, (day, res.get("reason"), res.get("selfproof"))
            stats[day] = res["ohlc_window"]["store"]
        assert stats[SPLIT_DAY]["suspect_refetches"] >= 1 and stats[SPLIT_DAY]["revised_tickers"] == []
        assert stats["2026-08-20"]["revised_tickers"] == ["GAPX"] and stats["2026-08-20"]["gap_days_settled"] >= 1
        assert all(st["as_of_unknown"] == 0 for st in stats.values()), "所有回放都经 run_replay，重放日恒已知"
        assert stats["2026-08-28"]["served_older_version"] > 0
        # 确认后 7 天内告警（生产按 416 换了口径或判不了，需人看）；之后只陈述、回到 ⏳。v0.45.416 起告警不再说生产记了假单。
        line = fwd.status_line(split_world.step11("2026-08-24"))
        assert line.startswith("⚠️ ") and "GAPX 近 7 天确认了" in line and "复权口径" in line, line
        assert "假止损" not in line and "另立任务" not in line, line
        line = fwd.status_line(split_world.step11("2026-08-28"))
        assert line.startswith("⏳ ") and "按时点重放" in line, line

    def test_a_books_the_same_gapx_trade_as_production(self, split_world, monkeypatch):
        """自证只比入场四元组——出场日 / 出场价错了也是 100%（审查者实测：把生效日比较改成 `<`，A 记成 08-19 出场，自证照样
        每天 11/11）。这里直接比：A 自己（不含种子 / 锚点里带进来的）记下的每一笔 GAPX 平仓，与生产那一笔逐字段相同。
        变异「生效日比较用 <」⇒ 红。"""
        (prod_t,) = _gapx_closed(split_world.prod)
        orig, mine = fwd._replay_variant, []

        def spy(overrides, state_dir, dates, seed=None):
            r = orig(overrides, state_dir, dates, seed=seed)
            f = Path(state_dir) / "closed_trades.jsonl"
            if overrides == {} and f.exists():
                seeded = set((seed or {}).get("closed_trades.jsonl", b"").decode("utf-8").splitlines())
                mine.extend(json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()
                            if '"GAPX"' in x and x not in seeded)
            return r
        monkeypatch.setattr(fwd, "_replay_variant", spy)
        for day in _rw.DATES[1:] + ["2026-08-28"]:
            split_world.step11(day)
        key = ("entry_date", "exit_date", "exit_reason", "exit_price")
        assert mine, "A 从没自己记下 GAPX 那笔——夹具没碰到拆股"
        assert {tuple(t[k] for k in key) for t in mine} == {tuple(prod_t[k] for k in key)}

    @pytest.mark.parametrize("how", ["no-store", "no-replay-day"])
    def test_without_point_in_time_the_same_world_fails_the_self_proof(self, split_world, how, monkeypatch):
        """对照：关掉 416（下游对口径敏感——416 之前的 paper_portfolio），同一份世界里不用库（现取：拆股前也按复权价重放 ⇒
        08-12 就假止损）、或库不按重放日取版本（一律最新版本），A 都与生产对不上。证明 100% 来自时点数据，不是夹具空转。"""
        _without_416(monkeypatch)
        _run_production(split_world.yf, split_world.prod.parent / "prod_without_416", monkeypatch)
        for day in _rw.DATES[1:]:
            split_world.step11(day)
        if how == "no-replay-day":
            orig = ReplayOhlcStore.pit_slice
            monkeypatch.setattr(ReplayOhlcStore, "pit_slice",
                                lambda self, t, s, e, cur, as_of: orig(self, t, s, e, cur, None))
        res = split_world.step11("2026-08-28", store=(how != "no-store"))
        assert res.get("selfproof_rate", 1.0) < 1.0 or res.get("status") == "cannot_judge", res.get("selfproof")


class TestSplitWith416NeedsNoPointInTime:
    """v0.45.416 之后：拆股本身不再要求日线是时点的——事后复权的日线在除权日之前判 `future_split`（当天不碰仓位、
    按入场价估值，不影响入场四元组），除权日那个重放日换口径，出场与生产同一笔。时点数据对数据更正等其他修订仍是必需的。"""

    def test_non_pit_replay_reproduces_production_through_the_split(self, split_world):
        """不用库（现取，全段复权）：A 逐笔复现生产。变异「416 不排除除权日 > as_of 的拆股」⇒ 拆股前就换口径、仍复现入场，
        但 GAPX 早换口径是时点泄漏——由 `test_paper_portfolio_split_adjust.TestPointInTime` 守；本条守「不再分叉」。"""
        for day in _rw.DATES[1:]:
            split_world.step11(day)
        res = split_world.step11("2026-08-28", store=False)
        assert res.get("selfproof_rate") == 1.0, (res.get("status"), res.get("reason"), res.get("selfproof"))
