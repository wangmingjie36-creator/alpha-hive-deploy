"""
v0.45.26 回归：数据隔离名单必须在**入库口**生效。

守的不变式：已证实取自错误交易日的观测，不得通过任何路径进入 `signal_archive`。

为什么必须挡在入库口而不是分析时过滤：`backfill()` 用 `INSERT OR REPLACE`
从 `.swarm_results_*.json` 重建，所以删库不足以清除污染——下一次回填会原样带回。
而若改在分析时过滤，每个下游都得记得过滤一次，漏一个就前功尽弃。

按项目惯例配「喂退化数据看它红」的用例。
"""
import json
import sqlite3

import pytest

import signal_archive as sa


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "p.db"
    sa.ensure_schema(p)
    return p


def _fake_swarm(iv=41.0, sent=55.0):
    """最小 swarm_results：含一个被隔离信号与一个未被隔离信号。"""
    return {
        "AAA": {
            "agent_details": {
                "OracleBeeEcho": {"details": {"iv_current": iv}},
                "BuzzBeeWhisper": {"details": {"sentiment_pct": sent}},
            }
        }
    }


def test_quarantined_signal_never_enters_db(db):
    """隔离日期的 options.iv_current 不得入库，同批其它信号照常入库。

    用的是 08-24 那条**全天**隔离（无 `tickers` 键）——`_fake_swarm()` 的
    "AAA" 不在任何标的名单里，专门用来证明这条隔离不挑标的。
    """
    q = sa.QUARANTINE[0]
    assert "tickers" not in q, "本测试假定第一条是全天隔离，条目顺序变了要检查这里"
    sa.archive(_fake_swarm(), q["date"], db)
    conn = sqlite3.connect(db)
    iv = conn.execute(
        "SELECT COUNT(*) FROM signal_archive WHERE date=? AND signal='options.iv_current'",
        (q["date"],)).fetchone()[0]
    sent = conn.execute(
        "SELECT COUNT(*) FROM signal_archive WHERE date=? AND signal='sentiment.pct'",
        (q["date"],)).fetchone()[0]
    assert iv == 0, "被隔离的信号入库了——回填会把已清除的污染带回来"
    assert sent == 1, "隔离误伤了同批的其它信号"


def test_other_dates_unaffected(db):
    """非隔离日期的同一信号必须正常入库——隔离是按 (日期, 信号) 精确生效的。"""
    sa.archive(_fake_swarm(), "2026-08-25", db)
    conn = sqlite3.connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM signal_archive WHERE date='2026-08-25' "
        "AND signal='options.iv_current'").fetchone()[0]
    assert n == 1, "隔离扩散到了未被污染的日期"


def test_is_quarantined_matrix():
    """`is_quarantined` 现在是 (date, ticker, signal) 三元——v0.45.266 加了按标的
    精确隔离的条目后，只按 (日期, 信号) 判会把全部标的一起误伤或一起放过。"""
    q = sa.QUARANTINE[0]
    assert sa.is_quarantined(q["date"], "ANY_TICKER", "options.iv_current") is True, (
        "无 tickers 键的条目必须对任意标的生效（既有全天隔离语义不变）")
    assert sa.is_quarantined(q["date"], "ANY_TICKER", "sentiment.pct") is False
    assert sa.is_quarantined("2026-08-25", "ANY_TICKER", "options.iv_current") is False


def test_quarantine_entries_are_documented():
    """
    每条隔离都必须写明原因与证据 —— 没有出处的隔离等于凭空删数据，
    半年后没人知道该不该恢复。含 tickers 键的条目还必须非空且无重复
    （空元组会静默不隔离任何标的，与「本条目存在」自相矛盾）。
    """
    assert sa.QUARANTINE, "隔离名单为空（若确已清空，请连同本测试一并移除）"
    for q in sa.QUARANTINE:
        assert q.get("date") and q.get("signals"), "隔离项缺 date/signals"
        assert len(q.get("reason", "")) > 40, f"{q['date']} 的 reason 过于简略"
        assert q.get("evidence"), f"{q['date']} 缺 evidence（日志/文件出处）"
        if "tickers" in q:
            tickers = q["tickers"]
            assert tickers, f"{q['date']} 的 tickers 是空的——等于隔离了个寂寞"
            assert len(set(tickers)) == len(tickers), f"{q['date']} 的 tickers 有重复"


# ══════════════════════════════════════════════════════════════════════════
# v0.45.266：按标的精确隔离（v0.45.238 期权快照会话错位）
#
# 与 2026-08-24 那条不同：v0.45.238 的普查（session_date_at 逐份重判）显示
# 11 个坏槽位日里**同一天既有陈旧标的也有正常标的**（例：2026-09-03 只有
# 24/30 只是坏的），若照旧全天拦截会连累好数据。故 QUARANTINE 条目支持可选
# `tickers` 白名单：给了就只挡这些标的，不给（如 08-24 那条）就仍是全天。
# ══════════════════════════════════════════════════════════════════════════

def _stale_snapshot_entry():
    """找到 2026-07-08 那条——9 个标的正常、只有 NVDA 是坏的，最适合验证精确隔离。"""
    for q in sa.QUARANTINE:
        if q["date"] == "2026-07-08":
            return q
    raise AssertionError("2026-07-08 的隔离条目不存在——v0.45.266 还没加")


class TestPerTickerQuarantine:

    def test_stale_ticker_is_quarantined(self):
        q = _stale_snapshot_entry()
        assert sa.is_quarantined("2026-07-08", "NVDA", "options.iv_current") is True

    def test_clean_ticker_same_day_is_not_quarantined(self):
        """成对断言：没有这条，「整天一起挡」也能让上一条为真。"""
        assert sa.is_quarantined("2026-07-08", "AMZN", "options.iv_current") is False

    def test_archive_gates_only_the_stale_ticker(self, db):
        """端到端：同一批 swarm_results 里 NVDA 与 AMZN 都有期权信号，
        归档后只有 NVDA 的 options.iv_current 缺席。"""
        swarm = {
            "NVDA": {"agent_details": {"OracleBeeEcho": {"details": {"iv_current": 55.0}}}},
            "AMZN": {"agent_details": {"OracleBeeEcho": {"details": {"iv_current": 30.0}}}},
        }
        sa.archive(swarm, "2026-07-08", db)
        with sqlite3.connect(db) as c:
            rows = {t: v for t, v in c.execute(
                "SELECT ticker, value FROM signal_archive WHERE date='2026-07-08' "
                "AND signal='options.iv_current'")}
        assert "NVDA" not in rows, "NVDA 那天的期权数据是陈旧的（v0.45.238），入库了"
        assert rows.get("AMZN") == 30.0, "AMZN 当天数据正常，被精确隔离误伤了"

    def test_untouched_ticker_family_stays(self, db):
        """隔离只按 `signals` 白名单里的信号生效——同一标的的 Agent 评分照常入库
        （划界原则：只隔离市场观测，不隔离审计轨迹，见 v0.45.28）。"""
        swarm = {"NVDA": {"final_score": 6.0, "agent_details": {
            "OracleBeeEcho": {"details": {"iv_current": 55.0}, "score": 7.0}}}}
        sa.archive(swarm, "2026-07-08", db)
        with sqlite3.connect(db) as c:
            sigs = {s for (s,) in c.execute(
                "SELECT signal FROM signal_archive WHERE date='2026-07-08' AND ticker='NVDA'")}
        assert "agent.OracleBeeEcho.score" in sigs
        assert "composite.final_score" in sigs
        assert "options.iv_current" not in sigs

    def test_all_eleven_stale_days_are_present(self):
        """11 个坏槽位日（v0.45.238 CHANGELOG 表）必须每天都有条目，一天都不许漏。"""
        days = {"2026-06-10", "2026-07-08", "2026-07-15", "2026-07-17", "2026-07-22",
                "2026-07-23", "2026-07-24", "2026-08-11", "2026-08-14",
                "2026-09-03", "2026-09-09"}
        present = {q["date"] for q in sa.QUARANTINE if q["date"] in days}
        assert present == days, f"缺 {days - present}"


def test_backfill_does_not_resurrect(db, tmp_path, monkeypatch):
    """端到端：删库后再跑 backfill，被隔离的行不得复活。

    用第一条（全天隔离）而不是按标的隔离的条目——`_fake_swarm()` 只造了
    一个不在任何标的白名单里的假标的 "AAA"，配全天隔离才对得上。
    """
    q = sa.QUARANTINE[0]
    assert "tickers" not in q
    f = tmp_path / f".swarm_results_{q['date']}.json"
    f.write_text(json.dumps(_fake_swarm()), encoding="utf-8")
    monkeypatch.setattr(sa, "DB_PATH", db)
    sa.backfill(".swarm_results_*.json", db)
    conn = sqlite3.connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM signal_archive WHERE date=? AND signal='options.iv_current'",
        (q["date"],)).fetchone()[0]
    assert n == 0, "backfill 让被隔离的污染复活了"


class TestFakeSnapshotQuarantineV0_45_472:
    """v0.45.472：期权快照现价 / 整条链不是当天观测的 (日期, 标的)，以及 CBOE 时代退到 yfinance 备用链的 GEX。

    变红的变异：删掉下面点名的几条 v0.45.472 条目（只抽查 3 条，不是全部 49 条——v0.45.479 更正原写法「任一条」）；
    或把 yfinance 那批的 signals 扩成五类（误伤当天真实的 IV / P/C / OI）。
    """

    def test_sample_chain_day_is_quarantined_whole(self):
        for sig in ("options.iv_current", "options.put_call_ratio", "options.gamma_exposure",
                    "options.total_oi", "bear.options_bear"):
            assert sa.is_quarantined("2026-06-22", "AMZN", sig), sig

    def test_strike_shaped_price_day_is_quarantined(self):
        assert sa.is_quarantined("2026-08-14", "QCOM", "options.gamma_exposure")

    def test_yfinance_fallback_quarantines_gex_only(self):
        assert sa.is_quarantined("2026-09-10", "BILI", "options.gamma_exposure")
        assert not sa.is_quarantined("2026-09-10", "BILI", "options.iv_current")
        assert not sa.is_quarantined("2026-09-10", "BILI", "options.put_call_ratio")

    def test_after_hours_price_days_are_not_quarantined(self):
        """财报日盘后价是真实价（另一类、已修），不是假现价，不隔离。"""
        assert not sa.is_quarantined("2026-08-26", "CRM", "options.gamma_exposure")
        assert not sa.is_quarantined("2026-07-29", "MSFT", "options.iv_current")


def test_fingerprinted_yfinance_days_quarantine_gex_only():
    """v0.45.475：按 IV 小数位指纹认出的 yfinance 备用链日子（09-01 BILI 在 quote_set 之前；10-05 BILI 的 error 被回填抹掉）。

    变红的变异：删掉这两条；或把 signals 扩到 IV / P/C。
    """
    for d in ("2026-09-01", "2026-10-05"):
        assert sa.is_quarantined(d, "BILI", "options.gamma_exposure")
        assert not sa.is_quarantined(d, "BILI", "options.iv_current")


def test_iv_constant_25_rows_quarantined_only_for_iv_current():
    """v0.45.478（用户批准）：iv_current 兜底常数 25.0 剩下的 6 行——只挡 `options.iv_current`。

    变红的变异：删掉任一条目；或把 VKTX 09-15 ~ 17 的 signals 扩到 iv_rank（那时 hv_proxy，不读 current_iv）；
    或去掉 tickers（会把同日其他标的的真实 IV 一起挡掉）。
    v0.45.481：CRCL 06-09、BILI / META 06-15 的 iv_rank 已核实由 25.0 算出、另条隔离——本条只对 VKTX 断言 iv_rank 不挡，
    另三对的 iv_rank 归 `test_iv_rank_from_constant_25_quarantined` 管。
    """
    pairs = [("2026-06-09", "CRCL"), ("2026-06-15", "BILI"), ("2026-06-15", "META"),
             ("2026-09-15", "VKTX"), ("2026-09-16", "VKTX"), ("2026-09-17", "VKTX")]
    for d, t in pairs:
        assert sa.is_quarantined(d, t, "options.iv_current"), (d, t)
        if t == "VKTX":
            assert not sa.is_quarantined(d, t, "options.iv_rank"), (d, t)
    assert not sa.is_quarantined("2026-09-15", "NVDA", "options.iv_current")
    # v0.45.479：原对照用的 06-09 NVDA 其实是坏的（归档那次运行现价 63、OI 251、P/C 0.0，已整份隔离），换成归档运行核过是真值的一对
    assert not sa.is_quarantined("2026-06-23", "TSLA", "options.iv_current")


def test_v0_45_476_false_positives_stay_unquarantined():
    """v0.45.476 撤回的 6 对误判：CRWD 四个周日快照（对照的是拆股调整后的收盘，比值恒 ≈ 4）；
    VKTX 08-12（34.5）与 MU 08-26（952.0）不在各自行权价网格中点上，不可能是行权价中位数。

    变红的变异：把它们加回任何隔离条目。
    """
    for d, t in (("2026-04-26", "CRWD"), ("2026-05-03", "CRWD"), ("2026-05-10", "CRWD"), ("2026-05-17", "CRWD"),
                 ("2026-08-12", "VKTX"), ("2026-08-26", "MU")):
        for sig in ("options.iv_current", "options.gamma_exposure", "options.put_call_ratio"):
            assert not sa.is_quarantined(d, t, sig), (d, t, sig)
    assert sa.is_quarantined("2026-04-26", "AAPL", "options.gamma_exposure"), "同日其余标的照样隔离"



class TestSwarmBasedReclassificationV0_45_479:
    """v0.45.479：按 swarm_results（归档的真实来源）重判后的隔离面。

    变红的变异：删掉任一 v0.45.479 条目；或把 06-23 TSLA / 06-26 BILI 加回去；或把 06-23 NVDA 整份隔离。
    """

    def test_archived_run_fakes_are_quarantined(self):
        for d, t in (("2026-06-09", "VKTX"), ("2026-06-09", "NVDA"), ("2026-06-15", "BILI"),
                     ("2026-06-17", "META"), ("2026-06-29", "BILI"), ("2026-03-31", "AMZN")):
            assert sa.is_quarantined(d, t, "options.put_call_ratio"), (d, t)

    def test_missed_yfinance_day_quarantines_gex_only(self):
        assert sa.is_quarantined("2026-08-31", "TMO", "options.gamma_exposure")
        assert not sa.is_quarantined("2026-08-31", "TMO", "options.put_call_ratio")

    def test_archived_run_real_data_is_released(self):
        for d, t in (("2026-06-23", "TSLA"), ("2026-06-26", "BILI")):
            assert not sa.is_quarantined(d, t, "options.gamma_exposure"), (d, t)
        assert not sa.is_quarantined("2026-06-23", "NVDA", "options.total_oi")
        assert sa.is_quarantined("2026-06-23", "NVDA", "options.iv_current"), "IV 12.5 是垃圾常数，只挡这一项"


def test_iv_rank_from_constant_25_quarantined():
    """v0.45.481（用户批准）：08-14 前 iv_current == 25.0 的 15 对，iv_rank / iv_percentile 是常数的函数——两类都挡。

    变红的变异：删任一日期条目；signals 少了 iv_percentile；去掉 tickers（同日真实标的会被一起挡掉）；
    把 VKTX 09-15 ~ 17（hv_proxy 时代，rank 不读 current_iv）也加进来。
    """
    pairs = [("2026-04-26", "AAPL"), ("2026-04-26", "HPE"), ("2026-04-26", "DELL"), ("2026-05-03", "AAPL"),
             ("2026-05-10", "UPST"), ("2026-05-17", "ICLN"), ("2026-06-09", "CRCL"),
             ("2026-06-15", "QCOM"), ("2026-06-15", "TSLA"), ("2026-06-15", "NVDA"), ("2026-06-15", "MSFT"),
             ("2026-06-15", "BILI"), ("2026-06-15", "AMZN"), ("2026-06-15", "META"), ("2026-06-15", "CRCL")]
    for d, t in pairs:
        for s in ("options.iv_rank", "options.iv_percentile"):
            assert sa.is_quarantined(d, t, s), (d, t, s)
    for d in ("2026-09-15", "2026-09-16", "2026-09-17"):
        assert not sa.is_quarantined(d, "VKTX", "options.iv_rank"), d
    assert not sa.is_quarantined("2026-04-26", "NVDA", "options.iv_rank")
    assert not sa.is_quarantined("2026-06-23", "TSLA", "options.iv_rank")


def test_yahoo_placeholder_iv_rows_quarantined():
    """v0.45.486（用户批准）：iv_current = 12.5 / 6.25（Yahoo 求解失败阶梯值的中位数）。08-14 前的 12 对连同由它算出的
    iv_rank / iv_percentile；TMO 08-31 只挡 iv_current（rank 走 hv_proxy，不读 current_iv）。

    变红的变异：删任一日期条目；08-14 前的条目少了 iv_rank 或 iv_percentile；TMO 也挡 iv_rank；去掉 tickers。
    """
    pre = [("2026-03-11", "NVDA"), ("2026-03-12", "NVDA"), ("2026-06-15", "RKLB"),
           ("2026-06-17", "BILI"), ("2026-06-17", "CRCL"), ("2026-06-17", "RKLB"), ("2026-06-17", "VKTX"),
           ("2026-06-23", "CRCL"), ("2026-06-23", "META"), ("2026-06-23", "QCOM"), ("2026-06-23", "RKLB"),
           ("2026-06-23", "VKTX")]
    for d, t in pre:
        for s in ("options.iv_current", "options.iv_rank", "options.iv_percentile"):
            assert sa.is_quarantined(d, t, s), (d, t, s)
    assert sa.is_quarantined("2026-08-31", "TMO", "options.iv_current")
    assert not sa.is_quarantined("2026-08-31", "TMO", "options.iv_rank")
    # 同日真实的一对（v0.45.479 按归档运行核过）
    assert not sa.is_quarantined("2026-06-23", "TSLA", "options.iv_current")
    assert not sa.is_quarantined("2026-06-23", "TSLA", "options.iv_rank")


def test_bili_1012_replay_placeholder_iv_quarantined():
    """v0.45.499（用户批准「删掉假 iv」）：10-12 重放写入的 BILI iv_current = 6.25（Yahoo 失败阶梯值，yfinance 链）。
    形状同 486 的 TMO 08-31：只挡 iv_current，rank 走 hv_proxy 不挡；P/C 当时不在批准范围（512 另立条目）。

    变红的变异：删该条目；去掉 tickers（同日 29 只真观测被连坐）；signals 加上 iv_rank。
    """
    assert sa.is_quarantined("2026-10-12", "BILI", "options.iv_current")
    assert not sa.is_quarantined("2026-10-12", "BILI", "options.iv_rank")
    assert not sa.is_quarantined("2026-10-12", "NVDA", "options.iv_current")


def test_bili_1012_stale_oi_put_call_quarantined():
    """v0.45.512（用户批准）：同一条 yfinance 链的 P/C 298.46——OI 陈旧（当天 Yahoo OI 几乎清零、只剩冻结旧数）。
    只挡 BILI 这一天的 put_call_ratio（同链 total_oi / gamma_exposure 由 522 另立条目，见下一条）；iv_rank 不挡；别的日子 / 标的不挡。
    （499 那条原先断言「P/C 不挡」——那是当时的批准范围，512 起改由本条覆盖。）

    变红的变异：删该条目；去掉 tickers（同日 29 只真观测被连坐）；signals 加上 iv_rank；日期错一天。
    """
    assert sa.is_quarantined("2026-10-12", "BILI", "options.put_call_ratio")
    assert not sa.is_quarantined("2026-10-12", "BILI", "options.iv_rank")
    assert not sa.is_quarantined("2026-10-12", "NVDA", "options.put_call_ratio")
    assert not sa.is_quarantined("2026-10-13", "BILI", "options.put_call_ratio")


_UNIVERSE_0924 = ("ABBV", "AMC", "AMZN", "BILI", "BRK-B", "COST", "CRCL", "CRM", "CVX", "DE", "DELL", "ENPH", "JNJ",
                  "META", "MSFT", "MU", "NEE", "NFLX", "NVDA", "QCOM", "RKLB", "SNOW", "T", "TMO", "TMUS", "TSLA",
                  "VKTX", "VZ", "WMT", "XOM")


def test_bad_chain_option_observations_quarantined_v0_45_522():
    """v0.45.522（用户批准「不要脏数据污染历史库」）：坏链派生的期权观测。

    ① BILI 10-12 同链 total_oi / GEX；② 06-17 VKTX / CRCL 残链（假中性 P/C、total_oi 13 / 97、GEX 0.0）；
    ③ 样本链早退写的 total_oi = 0（09-24 / 09-25 30/30、10-14 BILI）。

    变红的变异：删任一条目；③ 去掉 tickers 或漏标的；② 漏 signals；日期错一天；连坐同日真观测（见下面的 not）。
    """
    for sig in ("options.total_oi", "options.gamma_exposure"):
        assert sa.is_quarantined("2026-10-12", "BILI", sig), sig
    for tk in ("VKTX", "CRCL"):
        for sig in ("options.put_call_ratio", "options.total_oi", "options.gamma_exposure"):
            assert sa.is_quarantined("2026-06-17", tk, sig), (tk, sig)
    assert not sa.is_quarantined("2026-06-16", "VKTX", "options.total_oi")
    assert not sa.is_quarantined("2026-06-18", "CRCL", "options.total_oi")
    for d in ("2026-09-24", "2026-09-25"):
        for tk in _UNIVERSE_0924:          # 当天 30/30 都是样本链早退的 0，逐只核（漏一只就红）
            assert sa.is_quarantined(d, tk, "options.total_oi"), (d, tk)
        assert not sa.is_quarantined(d, "NVDA", "options.put_call_ratio")
    assert sa.is_quarantined("2026-10-14", "BILI", "options.total_oi")
    assert not sa.is_quarantined("2026-10-14", "NVDA", "options.total_oi")
    assert not sa.is_quarantined("2026-10-13", "BILI", "options.total_oi")


def test_rank_rows_from_placeholder_iv_whose_iv_current_was_purged_earlier():
    """v0.45.490（486 二次检查）：486 只从归档里现存的 iv_current 找 12.5 / 6.25，漏了 iv_current 早被 472 / 479 清掉的
    7 对——它们 08-14 前的 iv_rank / iv_percentile 同样由失败值算出。

    变红的变异：删任一日期条目；少了 iv_percentile；去掉 tickers。
    """
    pairs = [("2026-06-09", "RKLB"), ("2026-06-17", "MSFT"), ("2026-06-17", "TSLA"), ("2026-06-17", "AMZN"),
             ("2026-06-23", "NVDA"), ("2026-06-23", "AMZN"), ("2026-06-29", "AMZN")]
    for d, t in pairs:
        for s in ("options.iv_rank", "options.iv_percentile"):
            assert sa.is_quarantined(d, t, s), (d, t, s)
    assert not sa.is_quarantined("2026-06-23", "TSLA", "options.iv_rank")
    # v0.45.494：原来这里还有 06-29 NVDA 作对照——它的 iv_current 已被别的条目隔离，按 494 的镜像规则 rank 也该挡，
    # 不再是「真实的一对」，删掉（只留 479 按归档运行核过的 06-23 TSLA）。


class TestRankMirrorBefore0814:
    """v0.45.494（用户批准）：08-14 前 iv_rank / iv_percentile 由 current_iv 算出 ⇒ 凡隔离了 iv_current 的 (日期, 标的)，
    这两类一并隔离（规则派生，见 `_rank_mirror_entries`）。08-14 起 rank 不读 current_iv，不跟。"""

    @pytest.mark.parametrize("d,t", [("2026-07-08", "NVDA"),    # v0.45.238 一族（无版本号的「同上机制」条目）
                                     ("2026-04-09", "CRCL"),    # v0.45.472 样本链
                                     ("2026-06-10", "NVDA")])   # v0.45.238 会话错位
    def test_pre_cutoff_rank_follows_iv_current(self, d, t):
        """变红的变异：删掉 `QUARANTINE.extend(...)`；或镜像只挡 iv_rank 不挡 iv_percentile。"""
        assert sa.is_quarantined(d, t, "options.iv_current")
        assert sa.is_quarantined(d, t, "options.iv_rank")
        assert sa.is_quarantined(d, t, "options.iv_percentile")

    def test_post_cutoff_rank_not_mirrored(self):
        """08-24（v0.45.16 整日条目）与 VKTX 09-15（hv_proxy）：rank 不读 current_iv，不跟着隔离。

        变红的变异：截止日往后挪（如 09-30）。
        """
        assert sa.is_quarantined("2026-08-24", "NVDA", "options.iv_current")
        assert not sa.is_quarantined("2026-08-24", "NVDA", "options.iv_rank")
        assert not sa.is_quarantined("2026-09-15", "VKTX", "options.iv_rank")

    def test_tickers_stay_scoped(self):
        """镜像沿用源条目的 tickers：同日没被隔离 iv_current 的标的，rank 不受牵连。

        变红的变异：镜像丢掉 tickers（变成整日）。
        """
        assert not sa.is_quarantined("2026-04-09", "NVDA", "options.iv_current")
        assert not sa.is_quarantined("2026-04-09", "NVDA", "options.iv_rank")

    def test_mirrors_are_documented_and_tagged(self):
        """每条镜像都带标记（--only-tag 清库靠它）、写明源版本与证据。"""
        mirrors = [q for q in sa.QUARANTINE if "_mirror_of" in q]
        assert len(mirrors) >= 40
        for q in mirrors:
            assert q["reason"].startswith(sa._RANK_MIRROR_TAG) and "镜像" in q["reason"]
            assert "CHANGELOG.md#v0.45.494" in q["evidence"]
            assert q["date"] < sa._RANK_FROM_CURRENT_IV_UNTIL
