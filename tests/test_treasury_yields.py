"""美国财政部日度收益率曲线（v0.45.60）

`fred_macro` 的 10Y/5Y/2Y 原本走 yfinance `^TNX`/`^FVX`。2026-08-27 全天
687 次 429 把它们连同整块宏观打成 `data_source: "fallback"`，报告里的
`treasury_10y: 4.5` 是兜底常量而非观测值。

选财政部而不是 FRED 的理由是**发布时刻**：FRED 的 DGS10 转发的正是这份数据，
但晚一天（实测 2026-08-28 查询时 FRED 最新 08-26、财政部已有 08-27）。
扫描在当日 17:00 ET 跑，等不到 FRED。
"""

import pytest

import treasury_yields as ty


@pytest.fixture(autouse=True)
def _clean():
    ty.clear_cache()
    yield
    ty.clear_cache()


_XML = """<feed><entry><content><m:properties>
<d:NEW_DATE>2026-08-26T00:00:00</d:NEW_DATE>
<d:BC_2YEAR>4.19</d:BC_2YEAR><d:BC_5YEAR>4.37</d:BC_5YEAR><d:BC_10YEAR>4.66</d:BC_10YEAR>
</m:properties></content></entry><entry><content><m:properties>
<d:NEW_DATE>2026-08-27T00:00:00</d:NEW_DATE>
<d:BC_2YEAR>4.20</d:BC_2YEAR><d:BC_5YEAR>4.38</d:BC_5YEAR><d:BC_10YEAR>4.67</d:BC_10YEAR>
</m:properties></content></entry></feed>"""


class TestParse:
    def test_extracts_all_tenors(self):
        rows = ty._parse(_XML)
        assert set(rows) == {"2026-08-26", "2026-08-27"}
        assert rows["2026-08-27"] == {"y2": 4.20, "y5": 4.38, "y10": 4.67}

    def test_rejects_out_of_range(self):
        """哨兵/错位值不得进入。负利率时代美债 par yield 也没到 -1%。"""
        bad = _XML.replace("<d:BC_10YEAR>4.67</d:BC_10YEAR>",
                           "<d:BC_10YEAR>46.7</d:BC_10YEAR>")
        rows = ty._parse(bad)
        assert "y10" not in rows["2026-08-27"], "46.7% 应被区间检查挡下"
        assert rows["2026-08-27"]["y2"] == 4.20, "同一行其余期限不该连坐"

    def test_empty_xml_yields_nothing(self):
        assert ty._parse("<feed></feed>") == {}


class TestGetYieldCurve:
    def _patch(self, monkeypatch, xml=_XML):
        monkeypatch.setattr(ty, "_fetch_month", lambda *a, **k: xml)

    def test_latest_when_no_date(self, monkeypatch):
        self._patch(monkeypatch)
        r = ty.get_yield_curve()
        assert r["date"] == "2026-08-27" and r["is_latest"] is True
        assert r["y10"] == 4.67 and r["source"] == "treasury_gov"

    def test_specific_date(self, monkeypatch):
        self._patch(monkeypatch)
        r = ty.get_yield_curve("2026-08-26")
        assert r["y10"] == 4.66 and r["is_latest"] is False

    def test_missing_date_returns_none_not_nearest(self, monkeypatch):
        """周末/假日/尚未发布 → None，**绝不**悄悄给前一交易日。

        补跑要的是那一天的值；给前一天的值而不说，就是伪造。
        """
        self._patch(monkeypatch)
        assert ty.get_yield_curve("2026-08-29") is None

    def test_html_response_is_not_data(self, monkeypatch):
        """返回登录页/错误页时不得当成空曲线。"""
        self._patch(monkeypatch, "<!DOCTYPE html><html><body>nope</body></html>")
        assert ty.get_yield_curve() is None

    def test_fetch_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(ty, "_fetch_month", lambda *a, **k: None)
        assert ty.get_yield_curve() is None

    def test_schema_change_returns_none_not_garbage(self, monkeypatch):
        """标签改名 → 解析出 0 条 → 报不可得，而不是返回空 dict 让下游当成 0。"""
        self._patch(monkeypatch, "<feed><entry><d:FOO>1</d:FOO></entry></feed>")
        assert ty.get_yield_curve() is None

    def test_caches_within_ttl(self, monkeypatch):
        calls = {"n": 0}

        def _f(*a, **k):
            calls["n"] += 1
            return _XML

        monkeypatch.setattr(ty, "_fetch_month", _f)
        ty.get_yield_curve()
        ty.get_yield_curve("2026-08-26")     # 同月，应复用
        assert calls["n"] == 1


class TestRetry:
    def test_retries_then_succeeds(self, monkeypatch):
        """一次瞬时断连的代价是 3 个字段掉回 yfinance —— 值得重试。"""
        monkeypatch.setattr(ty.time, "sleep", lambda *_: None)
        state = {"n": 0}

        def _open(*a, **k):
            state["n"] += 1
            if state["n"] < 3:
                raise ConnectionError("Remote end closed connection")
            return _XML.encode()

        import http_gate
        monkeypatch.setattr(http_gate, "urlopen_gated", _open)
        assert ty._fetch_month("202608") is not None
        assert state["n"] == 3

    def test_gives_up_and_reports_none(self, monkeypatch):
        monkeypatch.setattr(ty.time, "sleep", lambda *_: None)
        import http_gate

        def _boom(*a, **k):
            raise ConnectionError("down")

        monkeypatch.setattr(http_gate, "urlopen_gated", _boom)
        assert ty._fetch_month("202608") is None
