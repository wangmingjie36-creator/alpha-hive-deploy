"""tests/test_fear_greed.py — Fear & Greed Index 模块测试"""

import json
import types
import pytest


# ==================== Fixtures ====================

@pytest.fixture(autouse=True)
def _clear_fear_greed_cache(tmp_path, monkeypatch):
    """清理 fear_greed 模块级缓存，隔离每个测试"""
    import fear_greed as mod
    # 指向临时目录避免跨测试缓存命中
    monkeypatch.setattr(mod, "_CACHE_PATH", tmp_path / "fear_greed.json")


def _make_fg_response(value=55, classification="Greed", ok=True, status_code=200):
    """构造 mock API 响应"""
    resp = types.SimpleNamespace()
    resp.ok = ok
    resp.status_code = status_code
    resp.json = lambda: {
        "data": [{"value": str(value), "value_classification": classification}]
    }
    return resp


# ==================== TestGetFearGreed ====================

class TestGetFearGreed:

    def test_success_real_data(self, monkeypatch):
        """mock API 返回正常数据，验证 value/classification/sentiment_score/is_real_data"""
        import fear_greed as mod
        mock_resp = _make_fg_response(72, "Greed")
        sess = types.SimpleNamespace(get=lambda *a, **kw: mock_resp)
        monkeypatch.setattr(mod, "get_session", lambda name: sess)

        result = mod.get_fear_greed()
        assert result["is_real_data"] is True
        assert result["value"] == 72
        assert result["classification"] == "Greed"
        assert 1.0 <= result["sentiment_score"] <= 10.0

    def test_extreme_fear(self, monkeypatch):
        """value=10 → Extreme Fear，score 较低"""
        import fear_greed as mod
        sess = types.SimpleNamespace(get=lambda *a, **kw: _make_fg_response(10, "Extreme Fear"))
        monkeypatch.setattr(mod, "get_session", lambda name: sess)

        result = mod.get_fear_greed()
        assert result["value"] == 10
        assert result["sentiment_score"] <= 2.0  # 10/10 = 1.0

    def test_extreme_greed(self, monkeypatch):
        """value=90 → Extreme Greed，score 较高"""
        import fear_greed as mod
        sess = types.SimpleNamespace(get=lambda *a, **kw: _make_fg_response(90, "Extreme Greed"))
        monkeypatch.setattr(mod, "get_session", lambda name: sess)

        result = mod.get_fear_greed()
        assert result["value"] == 90
        assert result["sentiment_score"] >= 8.0

    def test_api_failure_returns_default(self, monkeypatch):
        """resp.ok=False → 降级默认值"""
        import fear_greed as mod
        sess = types.SimpleNamespace(
            get=lambda *a, **kw: _make_fg_response(ok=False, status_code=500)
        )
        monkeypatch.setattr(mod, "get_session", lambda name: sess)

        result = mod.get_fear_greed()
        assert result["is_real_data"] is False
        assert result["value"] == 50

    def test_network_error_returns_default(self, monkeypatch):
        """网络异常 → 降级默认值"""
        import fear_greed as mod

        def _raise(*a, **kw):
            raise ConnectionError("timeout")

        sess = types.SimpleNamespace(get=_raise)
        monkeypatch.setattr(mod, "get_session", lambda name: sess)

        result = mod.get_fear_greed()
        assert result["is_real_data"] is False
        assert result["value"] == 50
        assert result["sentiment_score"] == 5.0

    def test_cache_hit(self, monkeypatch, tmp_path):
        """两次调用，第二次不发 HTTP → 验证缓存命中"""
        import fear_greed as mod

        call_count = {"n": 0}

        def _counting_get(*a, **kw):
            call_count["n"] += 1
            return _make_fg_response(65, "Greed")

        sess = types.SimpleNamespace(get=_counting_get)
        monkeypatch.setattr(mod, "get_session", lambda name: sess)

        r1 = mod.get_fear_greed()
        r2 = mod.get_fear_greed()
        assert r1["value"] == 65
        assert r2["value"] == 65
        assert call_count["n"] == 1  # 第二次从缓存读取

    def test_requests_missing_returns_default(self, monkeypatch):
        """requests 模块不可用 → 降级默认值"""
        import fear_greed as mod
        monkeypatch.setattr(mod, "_req", None)

        result = mod.get_fear_greed()
        assert result["is_real_data"] is False

    def test_sentiment_score_range(self, monkeypatch):
        """遍历 value 0-100，验证 score 在 [1.0, 10.0]"""
        import fear_greed as mod

        for v in range(0, 101, 10):
            # 清缓存（每次不同 value 需要重新请求）
            monkeypatch.setattr(mod, "_CACHE_PATH",
                                type(mod._CACHE_PATH)(f"/tmp/_fg_test_{v}.json"))
            sess = types.SimpleNamespace(
                get=lambda *a, v=v, **kw: _make_fg_response(v, "Test")
            )
            monkeypatch.setattr(mod, "get_session", lambda name: sess)

            result = mod.get_fear_greed()
            assert 1.0 <= result["sentiment_score"] <= 10.0, f"value={v} score={result['sentiment_score']}"

    def test_result_keys(self, monkeypatch):
        """验证返回 dict 包含所有必需 key"""
        import fear_greed as mod
        sess = types.SimpleNamespace(
            get=lambda *a, **kw: _make_fg_response(50, "Neutral")
        )
        monkeypatch.setattr(mod, "get_session", lambda name: sess)

        result = mod.get_fear_greed()
        for key in ("value", "classification", "sentiment_score", "is_real_data", "timestamp"):
            assert key in result, f"missing key: {key}"

    def test_default_result_structure(self):
        """验证 _default_result 包含正确字段"""
        import fear_greed as mod
        d = mod._default_result()
        assert d["is_real_data"] is False
        assert d["value"] == 50
        assert d["classification"] == "Neutral"
        assert d["sentiment_score"] == 5.0
        assert "timestamp" in d
