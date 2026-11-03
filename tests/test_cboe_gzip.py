"""v0.45.690：`cboe_options._fetch_cboe_payload` 请求 gzip，并按 `Content-Encoding` 解压。

背景
----
此前请求头只有 User-Agent，CBOE 一直回未压缩文件。2026-10-29 curl 各测一次：SPY 5.4MB / 26 秒 → 压缩 0.7MB / 13 秒，
_SPX 12.8MB / 45 秒 → 1.66MB / 15 秒。urllib 不替我们解压，所以「加请求头」和「按响应头解压」必须一起到位。

本文件钉住
----------
1. 请求头里确实带 `Accept-Encoding: gzip`（去掉它 ⇒ 红）。
2. 压缩 / 未压缩 / `identity` 三种响应解析出的 payload 逐字节相同（不解压 ⇒ 红）。
3. 不认识的编码不当未压缩原样用：不重试、计 `bad_encoding` + `failed`、出口 None（解压错编码 / 当原文用 ⇒ 红）。
4. 看得见：计数 → scan_timing `counters.cboe` → alert_manager P2；计数缺失 ⇒ checks_skipped。

全部离线：`urllib.request.urlopen` 打桩。
"""
from __future__ import annotations

import gzip
import json
import zlib

import pytest

import alert_manager as am
import cboe_options as co
import scan_timing as stt

DAY = "2026-09-04"
_PAYLOAD = {"options": [{"option": "SPY260918C00500000", "bid": 1.25, "ask": 1.3, "iv": 0.21}],
            "current_price": 512.34, "close": 511.9, "last_trade_time": f"{DAY}T15:59:59",
            "note": "非 ASCII 也要逐字节一致：期权链"}
_BODY = json.dumps({"data": _PAYLOAD}, ensure_ascii=False).encode("utf-8")


class _Resp:
    """最小 HTTPResponse：`.read()` + `.headers.get()`（真 HTTPResponse 的 headers 是 Message，get 不分大小写）。"""

    def __init__(self, body: bytes, encoding):
        self._b = body
        self.headers = {} if encoding is None else {"Content-Encoding": encoding}

    def read(self):
        return self._b


def _install(monkeypatch, body, encoding):
    seen = []

    def _open(req, timeout=0):
        seen.append(req)
        return _Resp(body, encoding)
    monkeypatch.setattr(co.urllib.request, "urlopen", _open)
    return seen


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(co, "_SNAPSHOT_PROVIDER", None)
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: DAY)
    monkeypatch.setattr(co.time, "sleep", lambda s: None)
    co._payload_cache.clear()
    co.reset_payload_stats()
    yield
    co._payload_cache.clear()
    co.reset_payload_stats()


class TestRequestHeader:

    def test_request_asks_for_gzip(self, monkeypatch):
        """变红的变异：请求头去掉 Accept-Encoding。"""
        seen = _install(monkeypatch, gzip.compress(_BODY), "gzip")
        assert co._fetch_cboe_payload("SPY", 5) is not None
        (req,) = seen
        # urllib.Request 把头名规范成首字母大写（Accept-encoding），按不分大小写取
        hdrs = {k.lower(): v for k, v in req.header_items()}
        assert hdrs.get("accept-encoding") == "gzip"
        assert hdrs.get("user-agent") == "Mozilla/5.0", "原有的 User-Agent 不能丢"

    def test_shared_header_dict_is_not_mutated(self, monkeypatch):
        """Request 拿到的是副本：别让某次请求改写模块级常量。"""
        _install(monkeypatch, _BODY, None)
        co._fetch_cboe_payload("SPY", 5)
        assert co._CBOE_HEADERS == {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"}


class TestDecoding:

    @pytest.mark.parametrize("body,encoding,compressed", [
        (_BODY, None, 0),                       # 对端不压缩、不带头
        (_BODY, "identity", 0),
        (gzip.compress(_BODY), "gzip", 1),
        (gzip.compress(_BODY), " GZIP ", 1),    # 大小写 / 空白不计
        (gzip.compress(_BODY), "x-gzip", 1),
    ])
    def test_payload_byte_identical(self, monkeypatch, body, encoding, compressed):
        """变红的变异：收到 gzip 不解压（json.loads 吃不了压缩字节 ⇒ 三次重试后 None）。"""
        _install(monkeypatch, body, encoding)
        got = co._fetch_cboe_payload("SPY", 5)
        assert got is not None
        assert json.dumps(got, ensure_ascii=False, sort_keys=True).encode() == \
            json.dumps(_PAYLOAD, ensure_ascii=False, sort_keys=True).encode()
        s = co.payload_stats()
        assert s["compressed"] == compressed and s["fetches"] == 1 and s["failed"] == 0

    def test_decode_helper_roundtrip(self):
        assert co._decode_cboe_body(gzip.compress(_BODY), "gzip") == (_BODY, True)
        assert co._decode_cboe_body(_BODY, None) == (_BODY, False)
        assert co._decode_cboe_body(_BODY, "") == (_BODY, False)

    def test_fake_response_without_headers_is_identity(self):
        """既有测试的假响应（io.BytesIO）没有 .headers：当未压缩，别让它们全炸。"""
        import io
        assert co._response_encoding(io.BytesIO(b"")) is None


class TestUnknownEncoding:

    @pytest.mark.parametrize("encoding,body", [
        ("br", b"\x8b\x05\x80" + _BODY),
        ("deflate", zlib.compress(_BODY)),
        ("compress", _BODY),                     # 内容碰巧可解析也不许当原文用
    ])
    def test_unknown_encoding_fails_visibly_without_retry(self, monkeypatch, encoding, body):
        """变红的变异：不认识的编码原样当未压缩用 / 当 gzip 解 / 进兜底 except 重试三次。"""
        seen = _install(monkeypatch, body, encoding)
        assert co._fetch_cboe_payload("SPY", 5, retries=3) is None
        s = co.payload_stats()
        assert s["bad_encoding"] == 1 and s["failed"] == 1
        assert len(seen) == 1 and s["fetches"] == 1, "编码不对不是瞬时故障，不该重试"
        assert "SPY" not in co._payload_cache

    def test_helper_raises(self):
        with pytest.raises(co.CboeContentEncodingError):
            co._decode_cboe_body(_BODY, "br")

    def test_corrupt_gzip_is_a_retryable_failure(self, monkeypatch):
        """头说 gzip、体坏了（截断传输）：按瞬时故障走原来的重试，不算编码问题。"""
        seen = _install(monkeypatch, gzip.compress(_BODY)[:40], "gzip")
        assert co._fetch_cboe_payload("SPY", 5, retries=3) is None
        s = co.payload_stats()
        assert len(seen) == 3 and s["bad_encoding"] == 0 and s["failed"] == 1


def _analyze(tmp_path, cboe_counter):
    counters = {} if cboe_counter is None else {"cboe": cboe_counter}
    status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
              "scan_timing": {"counters": counters, "extra": {"gh_pages": {"success": True}},
                              "production_sync": {"outcome": "up_to_date"}}}
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    a = am.AlertAnalyzer(report_dir=tmp_path)
    a.analyze(p)
    return [x for x in a.alerts if "Content-Encoding" in x.message], a


class TestVisibility:

    def test_counter_reaches_scan_timing(self, monkeypatch):
        _install(monkeypatch, _BODY, "br")
        co._fetch_cboe_payload("SPY", 5)
        assert stt.counters()["cboe"]["bad_encoding"] == 1

    def test_bad_encoding_is_p2(self, tmp_path):
        """变红的变异：analyze() 不调 `_check_cboe_content_encoding`。"""
        hits, _ = _analyze(tmp_path, {**co.payload_stats(), "bad_encoding": 2})
        (h,) = hits
        assert h.level == am.AlertLevel.MEDIUM and "2 次" in h.message

    def test_zero_is_silent_and_missing_is_skipped(self, tmp_path):
        hits, a = _analyze(tmp_path, co.payload_stats())
        assert hits == [] and not any("CBOE 响应编码" in s for s in a.checks_skipped)
        hits, a = _analyze(tmp_path, {"hits": 0, "fetches": 0})   # 旧版计数，没有这个键
        assert hits == [] and any("CBOE 响应编码" in s for s in a.checks_skipped)


# ───────────────────────────── v0.45.692：其余四处 CBOE 下载经 fetch_cboe_bytes
_CSV = b"DATE,OPEN,HIGH,LOW,CLOSE\n10/30/2026,17.1,18.0,16.9,17.44\n"
_SKEW = b"DATE,SKEW\n10/30/2026,147.02\n"
_QUOTE = json.dumps({"data": {"symbol": "^VIX", "current_price": 17.44,
                              "last_trade_time": "2026-10-30T16:15:00"}}).encode()


def _router(monkeypatch, bodies, encoding):
    """按 URL 尾巴回不同的体；encoding=gzip 时压缩后回。记下每个 Request。"""
    seen = []

    def _open(req, timeout=0):
        seen.append(req)
        name = req.full_url.rsplit("/", 1)[-1]
        body = bodies[name]
        return _Resp(gzip.compress(body) if encoding == "gzip" else body, encoding)
    monkeypatch.setattr(co.urllib.request, "urlopen", _open)
    return seen


def _asks_gzip(req):
    return {k.lower(): v for k, v in req.header_items()}.get("accept-encoding") == "gzip"


class TestFetchCboeBytes:

    def test_header_and_decode(self, monkeypatch):
        seen = _router(monkeypatch, {"X.csv": _CSV}, "gzip")
        assert co.fetch_cboe_bytes("https://cdn.cboe.com/X.csv", 5) == _CSV
        assert _asks_gzip(seen[0])
        assert co.payload_stats()["compressed"] == 0, "compressed 只数期权链 payload，别把别的下载混进来"

    def test_unknown_encoding_counted_at_source_and_raised(self, monkeypatch):
        """变红的变异：源头不计 bad_encoding（调用方都吞异常，那样 P2 就永远不响）。"""
        _router(monkeypatch, {"X.csv": _CSV}, "br")
        with pytest.raises(co.CboeContentEncodingError):
            co.fetch_cboe_bytes("https://cdn.cboe.com/X.csv", 5)
        s = co.payload_stats()
        assert s["bad_encoding"] == 1 and s["failed"] == 0, "failed 是期权链 payload 的口径，这里不计"


class TestCboeVix:

    @pytest.mark.parametrize("encoding", [None, "gzip"])
    def test_history_csv_same_text(self, monkeypatch, encoding):
        import cboe_vix
        seen = _router(monkeypatch, {"VIX_History.csv": _CSV}, encoding)
        assert cboe_vix._download() == _CSV.decode()
        assert _asks_gzip(seen[0])

    @pytest.mark.parametrize("encoding", [None, "gzip"])
    def test_quote_same_data(self, monkeypatch, encoding):
        import cboe_vix
        seen = _router(monkeypatch, {"_VIX.json": _QUOTE}, encoding)
        assert cboe_vix._download_quote() == json.loads(_QUOTE)["data"]
        assert _asks_gzip(seen[0])

    def test_unknown_encoding_is_none_and_counted(self, monkeypatch):
        import cboe_vix
        _router(monkeypatch, {"VIX_History.csv": _CSV, "_VIX.json": _QUOTE}, "br")
        assert cboe_vix._download() is None and cboe_vix._download_quote() is None
        assert co.payload_stats()["bad_encoding"] == 2


class TestCboeFetcherIndexCsv:

    @pytest.mark.parametrize("encoding", [None, "gzip"])
    def test_same_text(self, monkeypatch, encoding):
        import cboe_fetcher
        seen = _router(monkeypatch, {"SKEW_History.csv": _SKEW}, encoding)
        assert cboe_fetcher._download_cboe_index_csv("SKEW") == _SKEW.decode()
        assert _asks_gzip(seen[0])

    def test_unknown_encoding_not_retried(self, monkeypatch):
        """变红的变异：编码异常落进兜底 except 重试三次。"""
        import cboe_fetcher
        monkeypatch.setattr(cboe_fetcher.time, "sleep", lambda s: None)
        seen = _router(monkeypatch, {"SKEW_History.csv": _SKEW}, "br")
        assert cboe_fetcher._download_cboe_index_csv("SKEW") is None
        assert len(seen) == 1 and co.payload_stats()["bad_encoding"] == 1


class TestCloseCorrection:

    def test_gzip_and_plain_give_same_closes(self, monkeypatch):
        import close_correction as cc
        results = []
        for encoding in (None, "gzip"):
            seen = _router(monkeypatch, {"SPY.json": _BODY}, encoding)
            v = {}
            results.append((cc.cboe_official_closes(["SPY"], verdicts=v), v))
            assert _asks_gzip(seen[0])
        assert results[0] == results[1]
        assert results[0][0] == {"SPY": {DAY: 511.9}}, "夹具得真能解析出收盘，比较才有意义"

    def test_unknown_encoding_is_fetch_failed(self, monkeypatch):
        import close_correction as cc
        _router(monkeypatch, {"SPY.json": _BODY}, "br")
        v = {}
        assert cc.cboe_official_closes(["SPY"], verdicts=v) == {}
        assert v.get(cc._CBOE_FETCH_FAILED) == 1 and co.payload_stats()["bad_encoding"] == 1
