"""`conftest._offline_transport` 的自证：三种传输各堵一次，外加三条对照（v0.45.133）。

为什么需要这个文件：闸把请求挡在传输层之后，生产代码会走**和真离线一样**的降级
分支——也就是说闸失效时，症状是「测试又开始偷偷打外网」，而不是任何测试变红。
没有这里的断言，闸坏了没人会知道。

⚠️ 三条「被挡下」的测试用 `offline_gate_blocked` 出口 fixture 免去 teardown 判红：
闸的测试必然要触发闸。不要改成把本文件加进 `_KNOWN_NETWORK_REACHERS`。
"""

import pytest

_EXTERNAL = "https://cdn.cboe.com/api/global/delayed_quotes/options/NVDA.json"


class TestEachTransportIsBlocked:
    """六个已知取数源只用了三种传输，逐一堵住。"""

    def test_urllib_urlopen(self, offline_gate_blocked):
        """CBOE payload / CBOE VIX / http_gate(AV+Finnhub) / vixcentral 走这条。"""
        import urllib.request
        with pytest.raises(OSError, match="测试默认离线"):
            urllib.request.urlopen(_EXTERNAL, timeout=5)
        assert offline_gate_blocked() == ["urlopen cdn.cboe.com"]

    def test_requests_session(self, offline_gate_blocked):
        """reddit_sentiment 走这条（Slack 另有专闸 _block_slack）。"""
        import requests
        with pytest.raises(OSError, match="测试默认离线"):
            requests.get("https://api.github.com", timeout=5)
        assert offline_gate_blocked() == ["requests.get api.github.com"]

    def test_curl_cffi_session(self, offline_gate_blocked):
        """yfinance 1.2 走这条 —— libcurl 在 C 层开 socket，钩 socket 看不见它。"""
        import yfinance as yf
        with pytest.raises(Exception):
            yf.Ticker("NVDA").info
        assert any(b.startswith("curl.") for b in offline_gate_blocked()), \
            offline_gate_blocked()


class TestTheGateDoesNotOverreach:
    """三条对照。缺了它们，一个「拦下一切」的恒真闸也会全绿。"""

    def test_localhost_is_not_blocked(self):
        """本机地址照常放行 —— 不能按 socket 地址判断，本机出网走 127.0.0.1 代理，
        按地址放行等于全放；所以闸设在拿得到真实 URL 的库级 API 上。"""
        import urllib.request
        with pytest.raises(OSError) as exc:
            urllib.request.urlopen("http://localhost:1/", timeout=1)
        assert "测试默认离线" not in str(exc.value), "本机请求被闸误挡了"

    def test_a_plain_test_does_not_trip_the_guard(self):
        """不出网的测试不该被判红（否则 teardown 断言恒真）。"""
        assert 1 + 1 == 2

    @pytest.mark.network
    def test_network_marked_tests_are_exempt(self):
        """带 marker 的测试不受闸约束 —— 它们的意图就是打真外网
        （实测 `-m network` 那 24 个测试确实出网 168 次，marker 是准的）。

        只比身份、不真发请求。这条同时是**生成器 fixture 的回归**：豁免分支里
        写 `return` 而不是 `yield; return`，pytest 会报 `did not yield a value`，
        把这 24 个测试全打死 —— 而 CI 用 `-m "not network"` 摘掉它们，**照样全绿**。
        ⚠️ 也就是说这条回归只有本地跑得到，CI 抓不住。
        """
        import urllib.request
        assert not getattr(urllib.request.urlopen, "_offline_gated", False)
