"""cloud_snapshot_fetch vintage 校验守卫（v0.45.36）。

守什么
------
目录名来自墙上时钟（`_business_date`），数据新鲜度必须由 payload 自证。
CBOE 在盘前/休市**照常 200** 返回上一交易日的结算数据，顶层 `timestamp`
却是当下时刻——首跑（2026-08-26 02:28 PDT）就这样把 8/25 的数据存成了
`cloud_snapshots/2026-08-26/`，manifest 还报 30/30 成功。

实测证据（写死进回归用例，见 test_regression_first_run_pollution）：
    快照 price_at_fetch = 213.67 ；NVDA 8/25 收盘 = 213.05 ；8/26 尚未开盘。

全部按「喂退化数据看它红」构造：先证明旧行为会被抓住，再证明正路不误伤。
"""

import sys
import os
import json

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cloud_snapshot_fetch as csf  # noqa: E402


def _payload(last_trade="2026-08-26T15:26:31", *, drop_lt=False, price=210.955):
    """一份最小但字段名与真实 CBOE `data` 段一致的 payload。"""
    p = {
        "symbol": "NVDA",
        "options": [{"option": "NVDA260828C00200000", "iv": 0.35}],
        "current_price": price,
        "close": price,
        "prev_day_close": 213.05,
        "last_trade_time": last_trade,
    }
    if drop_lt:
        p.pop("last_trade_time")
    return p


# ══════════════════════════════════════════════════════════════════
# _vintage() —— 指纹解析
# ══════════════════════════════════════════════════════════════════

def test_vintage_parses_naive_et():
    assert csf._vintage(_payload())[0] == "2026-08-26"


def test_vintage_uses_last_trade_not_top_level_timestamp():
    """刻意不看 CDN 生成时刻——那正是骗过首跑的字段。

    构造：payload 里塞一个『今天』的 timestamp，但成交时刻是昨天。
    旧口径会认为新鲜，新口径必须认定为 8/25。
    """
    p = _payload(last_trade="2026-08-25T16:00:02")
    p["timestamp"] = "2026-08-26 09:28:04"      # CDN 生成时刻＝当下
    assert csf._vintage(p)[0] == "2026-08-25"


def test_vintage_tz_aware_normalised_to_et():
    # 20:30 UTC = 16:30 ET，同一天；若误按 UTC 取日期这里仍是 26 号，
    # 故用跨日的 03:30 UTC（= 前一天 23:30 ET）才能真正区分口径。
    d, _ = csf._vintage(_payload(last_trade="2026-08-27T03:30:00+00:00"))
    assert d == "2026-08-26"


@pytest.mark.parametrize("bad", [None, "", "not-a-date", 12345, {"t": 1}])
def test_vintage_unparseable_returns_none_not_today(bad):
    """拿不到就说拿不到——绝不回落成 `今天`。

    这正是 MEMORY「安全默认值判据」要治的形态：默认值不得让下游
    误以为掌握了信息。
    """
    p = _payload()
    p["last_trade_time"] = bad
    assert csf._vintage(p)[0] is None


# ══════════════════════════════════════════════════════════════════
# _fetch_one_ticker() —— 拒绝陈旧
# ══════════════════════════════════════════════════════════════════

def test_stale_payload_raises(monkeypatch):
    """核心守卫：vintage 早于业务日 → StaleVintageError。"""
    import cboe_options as co
    monkeypatch.setattr(co, "_fetch_cboe_payload",
                        lambda t, to, **k: _payload(last_trade="2026-08-25T16:00:02"))
    with pytest.raises(csf.StaleVintageError) as ei:
        csf._fetch_one_ticker("NVDA", "2026-08-26")
    assert "2026-08-25" in str(ei.value)


def test_stale_rejected_before_expensive_parse(monkeypatch):
    """陈旧数据必须在链解析**之前**被拦——解析得再干净也是错的一天。"""
    import cboe_options as co
    calls = []
    monkeypatch.setattr(co, "_fetch_cboe_payload",
                        lambda t, to, **k: _payload(last_trade="2026-08-25T16:00:02"))
    for fn in ("fetch_cboe_chain", "fetch_cboe_iv_term_structure", "fetch_cboe_full_chain_oi"):
        monkeypatch.setattr(co, fn, lambda *a, _f=fn, **k: calls.append(_f))
    with pytest.raises(csf.StaleVintageError):
        csf._fetch_one_ticker("NVDA", "2026-08-26")
    assert calls == [], f"陈旧 payload 仍触发了解析：{calls}"


def test_fresh_payload_stamps_vintage(monkeypatch):
    import cboe_options as co
    monkeypatch.setattr(co, "_fetch_cboe_payload", lambda t, to, **k: _payload())
    monkeypatch.setattr(co, "fetch_cboe_chain", lambda *a, **k: {"expiries": []})
    monkeypatch.setattr(co, "fetch_cboe_iv_term_structure", lambda *a, **k: [])
    monkeypatch.setattr(co, "fetch_cboe_full_chain_oi", lambda *a, **k: {})
    out = csf._fetch_one_ticker("NVDA", "2026-08-26")
    assert out["vintage_status"] == "ok"
    assert out["vintage_date"] == "2026-08-26"
    assert out["last_trade_time_et"] == "2026-08-26T15:26:31"
    assert out["prev_day_close"] == 213.05


def test_missing_timestamp_is_unverifiable_not_ok(monkeypatch):
    """证不出新鲜 ≠ 陈旧：照常落盘，但状态必须是 unverifiable。"""
    import cboe_options as co
    monkeypatch.setattr(co, "_fetch_cboe_payload", lambda t, to, **k: _payload(drop_lt=True))
    monkeypatch.setattr(co, "fetch_cboe_chain", lambda *a, **k: {"expiries": []})
    monkeypatch.setattr(co, "fetch_cboe_iv_term_structure", lambda *a, **k: [])
    monkeypatch.setattr(co, "fetch_cboe_full_chain_oi", lambda *a, **k: {})
    out = csf._fetch_one_ticker("NVDA", "2026-08-26")
    assert out["vintage_status"] == "unverifiable"
    assert out["vintage_date"] is None


def test_regression_first_run_pollution(monkeypatch):
    """回放首跑事故的真实数字，证明它现在会被拒绝。

    2026-08-26 02:28 PDT（盘前）拉取 → CBOE 返回 8/25 收盘后的最后成交，
    price 213.67（8/25 收盘 213.05 + 盘后漂移）。旧代码存成了 8/26。
    """
    import cboe_options as co
    monkeypatch.setattr(co, "_fetch_cboe_payload",
                        lambda t, to, **k: _payload(last_trade="2026-08-25T19:59:58", price=213.67))
    with pytest.raises(csf.StaleVintageError):
        csf._fetch_one_ticker("NVDA", "2026-08-26")


# ══════════════════════════════════════════════════════════════════
# main() —— 市场级中止与 manifest 审计
# ══════════════════════════════════════════════════════════════════

def _run_main(monkeypatch, tmp_path, payload_fn, tickers="A,B,C,D,E"):
    import cboe_options as co
    monkeypatch.setattr(co, "_fetch_cboe_payload", payload_fn)
    monkeypatch.setattr(co, "fetch_cboe_chain", lambda *a, **k: {"expiries": []})
    monkeypatch.setattr(co, "fetch_cboe_iv_term_structure", lambda *a, **k: [])
    monkeypatch.setattr(co, "fetch_cboe_full_chain_oi", lambda *a, **k: {})
    monkeypatch.setattr(csf, "_business_date", lambda: "2026-08-26")
    monkeypatch.setattr(csf.time, "sleep", lambda s: None)      # 别真等 0.5s × N
    import cboe_fetcher, fear_greed
    monkeypatch.setattr(cboe_fetcher.CBOEDailyFetcher, "fetch_all", lambda self: {})
    monkeypatch.setattr(fear_greed, "get_fear_greed", lambda: {"value": 50})
    monkeypatch.setattr(sys, "argv",
                        ["x", "--out", str(tmp_path), "--tickers", tickers])
    rc = csf.main()
    mf = json.load(open(tmp_path / "2026-08-26" / "manifest.json"))
    return rc, mf, tmp_path / "2026-08-26"


def test_market_wide_stale_aborts_after_three(monkeypatch, tmp_path):
    """休市/盘前触发：三连陈旧就停手，不白烧 30 次请求，且一个标的都不落盘。"""
    rc, mf, day = _run_main(
        monkeypatch, tmp_path,
        lambda t, to, **k: _payload(last_trade="2026-08-25T16:00:02"))
    assert mf["abort_reason"] == "stale_vintage"
    assert mf["tickers_ok"] == 0
    assert len(mf["vintage_stale"]) == 3, "应在第 3 个就中止，而不是跑满 5 个"
    assert not list(day.glob("[A-E].json")), "陈旧批次不得留下任何标的文件"
    assert rc == 2, "tickers_ok=0 → routine 据此拒绝 commit"


def test_all_unverifiable_keeps_data_but_flags(monkeypatch, tmp_path):
    """CBOE 改字段名 ≠ 数据陈旧——不能因为证不出来就丢掉可能是好的一天。"""
    rc, mf, day = _run_main(monkeypatch, tmp_path, lambda t, to, **k: _payload(drop_lt=True))
    assert mf["vintage_unverifiable_all"] is True
    assert mf["tickers_ok"] == 5, "数据应照常落盘"
    assert mf["vintage_ok"] == 0
    assert rc == 1, "可提交但必须是非零码，逼 routine 如实报告"


def test_clean_day_has_no_vintage_noise(monkeypatch, tmp_path):
    """正路不误伤：全新鲜时审计字段必须干净，退出码 0。"""
    rc, mf, _ = _run_main(monkeypatch, tmp_path, lambda t, to, **k: _payload())
    assert (mf["vintage_stale"], mf["vintage_unverifiable"]) == ([], [])
    assert mf["vintage_unverifiable_all"] is False
    assert mf["abort_reason"] is None
    assert mf["vintage_ok"] == 5 and rc == 0


# ══════════════════════════════════════════════════════════════════
# 陈旧标的补抓（v0.45.39）—— 滞后型能救，卡死型救不了
# ══════════════════════════════════════════════════════════════════

def test_lagging_ticker_recovered_on_retry(monkeypatch, tmp_path):
    """CDN 刷新滞后的标的（实测 TMUS 收盘后约 20 分钟才更新）必须被补回。

    构造：B 第一次拿到昨天的数据，第二次（大盘段跑完之后）拿到今天的。
    """
    calls = {}

    def payload(t, to, **k):
        calls[t] = calls.get(t, 0) + 1
        if t == "B" and calls[t] == 1:
            return _payload(last_trade="2026-08-25T16:00:00")
        return _payload()

    rc, mf, day = _run_main(monkeypatch, tmp_path, payload, tickers="A,B,C")
    assert mf["tickers_ok"] == 3, f"补抓没生效：{mf['failed']}"
    assert mf["vintage_stale"] == [], "补回后不该还留在 stale 名单里"
    assert "B" not in mf["failed"], "补回的标的必须从 failed 里移除"
    assert (day / "B.json").exists()
    assert calls["B"] == 2, "B 应当被抓两次（首轮 + 补抓）"
    assert rc == 0


def test_permanently_stale_ticker_stays_failed(monkeypatch, tmp_path):
    """CDN 卡死的标的（实测 TMO 卡 44.5 小时）补抓也救不回，必须如实留在 failed。

    云沙箱里 yfinance 不通，这种只能弃掉；本机扫描有降级链（cboe_options v0.45.39）。
    """
    def payload(t, to, **k):
        return _payload(last_trade="2026-08-24T15:59:59") if t == "B" else _payload()

    rc, mf, day = _run_main(monkeypatch, tmp_path, payload, tickers="A,B,C")
    assert mf["tickers_ok"] == 2
    assert mf["vintage_stale"] == ["B"]
    assert "StaleVintageError" in mf["failed"]["B"]
    assert not (day / "B.json").exists(), "陈旧标的不得落盘"
    assert rc == 1


# ══════════════════════════════════════════════════════════════════
# 内外两道 vintage 校验的**组合**（v0.45.91 回归）
# ══════════════════════════════════════════════════════════════════
# 上面所有用例都 monkeypatch 掉了 `_fetch_cboe_payload`，桩子直接交回一份
# 陈旧 dict —— 于是外层 `_vintage` 命中、`StaleVintageError` 照抛、补抓照跑，
# 一路全绿。但**真实函数对同一份输入返回 None**（内层 v0.45.39 先拦下），
# `_fetch_one_ticker` 撞的是 `if not payload: raise RuntimeError(...)`，
# 落进普通失败、绕开补抓。桩造了一个生产中不可能出现的场景，
# 测的是一条不存在的路径：08-28 BILI、08-31 TMO、09-01 BILI 连中三次，
# 测试从头到尾没红过一次。
#
# 所以下面这组**只桩网络层**，让两道校验真的串起来跑。

def _cboe_body(last_trade="2026-08-26T15:59:59", price=210.955):
    """真实 CBOE 响应的外层包装（`{"data": {...}}`），喂给真 `_fetch_cboe_payload`。"""
    return json.dumps({"timestamp": "2026-08-26 20:41:33", "symbol": "NVDA",
                       "data": _payload(last_trade=last_trade, price=price)}).encode()


class _Resp:
    def __init__(self, b): self._b = b
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def _real_payload_path(monkeypatch):
    """卸掉 payload 桩，改桩网络；内层判据钉在业务日，与外层一致（收盘后口径）。"""
    import cboe_options as co
    co.invalidate_payload_cache()
    co._vintage_stats.update({"checked": 0, "stale": 0})
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-26")
    monkeypatch.setattr(co, "fetch_cboe_chain", lambda *a, **k: {"expiries": []})
    monkeypatch.setattr(co, "fetch_cboe_iv_term_structure", lambda *a, **k: [])
    monkeypatch.setattr(co, "fetch_cboe_full_chain_oi", lambda *a, **k: {})
    yield co
    co.invalidate_payload_cache()


def test_inner_guard_classified_as_stale_not_generic(_real_payload_path, monkeypatch):
    """核心回归：内层先命中时，也必须是 `StaleVintageError`。

    改动前这里抛的是 `RuntimeError: CBOE payload 为空（网络/403/符号问题）`
    —— 一条把「等 CDN 刷新就能补回」误报成「网络挂了」的错误信息。
    """
    co = _real_payload_path
    monkeypatch.setattr(co.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(_cboe_body("2026-08-25T16:00:02")))
    with pytest.raises(csf.StaleVintageError) as ei:
        csf._fetch_one_ticker("BILI", "2026-08-26")
    assert "2026-08-25" in str(ei.value)
    assert "payload 为空" not in str(ei.value), "陈旧不许再被报成网络失败"


def test_real_network_failure_still_generic(_real_payload_path, monkeypatch):
    """反向守卫：真的网络失败仍归为泛化 RuntimeError，不许被顺手改成陈旧。

    分类错到另一边同样有害——补抓会去救一个救不回来的标的。
    """
    co = _real_payload_path
    monkeypatch.setattr(co.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(co.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError) as ei:
        csf._fetch_one_ticker("BILI", "2026-08-26")
    assert not isinstance(ei.value, csf.StaleVintageError)
    assert "payload 为空" in str(ei.value)


def test_e2e_cdn_stale_ticker_is_backfilled(_real_payload_path, monkeypatch, tmp_path):
    """端到端：09-01 BILI 的真实形态，走完 main() 必须被补抓救回。

    这是本次改动唯一真正重要的断言——上面那些桩过 `_fetch_cboe_payload` 的
    补抓用例，对这条路径一个都不成立。
    """
    co = _real_payload_path
    hits = {"n": 0}

    def _net(*a, **k):
        hits["n"] += 1
        # 首轮 CDN 上还是 8/25 的文件；补抓时（大盘段之后）已刷新。
        return _Resp(_cboe_body("2026-08-25T16:00:02") if hits["n"] == 1
                     else _cboe_body())

    monkeypatch.setattr(co.urllib.request, "urlopen", _net)
    monkeypatch.setattr(csf, "_business_date", lambda: "2026-08-26")
    monkeypatch.setattr(csf.time, "sleep", lambda s: None)
    import cboe_fetcher, fear_greed
    monkeypatch.setattr(cboe_fetcher.CBOEDailyFetcher, "fetch_all", lambda self: {})
    monkeypatch.setattr(fear_greed, "get_fear_greed", lambda: {"value": 50})
    monkeypatch.setattr(sys, "argv", ["x", "--out", str(tmp_path), "--tickers", "BILI"])

    rc = csf.main()
    mf = json.load(open(tmp_path / "2026-08-26" / "manifest.json"))
    assert mf["tickers_ok"] == 1, f"补抓没生效：{mf['failed']}"
    assert mf["failed"] == {}
    assert (tmp_path / "2026-08-26" / "BILI.json").exists()
    assert hits["n"] == 2, "应当抓两次（首轮 + 补抓）"
    assert rc == 0


def test_e2e_manifest_does_not_lie_about_stale(_real_payload_path, monkeypatch, tmp_path):
    """CDN 卡死时 manifest 必须**认账**：写进 vintage_stale，而不是报空。

    09-01 的 manifest 写着 `vintage_stale: []`，而同一次运行的 stderr 里
    明写「CBOE BILI 数据陈旧」。审计字段和日志互相矛盾时，人只会信前者。
    """
    co = _real_payload_path
    monkeypatch.setattr(co.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(_cboe_body("2026-08-25T16:00:02")))
    monkeypatch.setattr(csf, "_business_date", lambda: "2026-08-26")
    monkeypatch.setattr(csf.time, "sleep", lambda s: None)
    import cboe_fetcher, fear_greed
    monkeypatch.setattr(cboe_fetcher.CBOEDailyFetcher, "fetch_all", lambda self: {})
    monkeypatch.setattr(fear_greed, "get_fear_greed", lambda: {"value": 50})
    monkeypatch.setattr(sys, "argv", ["x", "--out", str(tmp_path), "--tickers", "BILI"])

    csf.main()
    mf = json.load(open(tmp_path / "2026-08-26" / "manifest.json"))
    assert mf["vintage_stale"] == ["BILI"], "陈旧标的必须出现在审计字段里"
    assert "StaleVintageError" in mf["failed"]["BILI"]
