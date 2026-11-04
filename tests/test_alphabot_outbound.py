"""Alpha Bot 出站方式（v0.45.713）：启动时探本机代理，通就走代理，不通看得见。

背景见 `alphabot/outbound.py` docstring：.app 启动的服务没有代理变量，yfinance（curl_cffi）不读系统代理 ⇒ 直连，
直连出口被 Yahoo 掐。本文件全离线：探活一律注入假函数；真探活函数只对本机回环端口试。
"""
from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from alphabot import outbound as ob

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:7897"


class _Probe:
    def __init__(self, result=None):
        self.result, self.calls = result, []

    def __call__(self, url):
        self.calls.append(url)
        return self.result


class TestConfigure:
    @pytest.mark.parametrize("var", ob.PROXY_VARS)
    def test_inherited_proxy_left_alone_and_not_probed(self, var):
        env = {var: "http://10.0.0.1:1"}
        p = _Probe("不该被调")
        st = ob.configure(env, probe=p)
        assert st["mode"] == "inherited" and st["proxy"] == "http://10.0.0.1:1"
        assert p.calls == [] and env == {var: "http://10.0.0.1:1"}

    @pytest.mark.parametrize("val", ["none", "NONE", " none "])
    def test_explicit_none_disables(self, val):
        env = {"ALPHA_HIVE_PROXY": val}
        p = _Probe()
        st = ob.configure(env, probe=p)
        assert st["mode"] == "disabled" and p.calls == []
        assert set(env) == {"ALPHA_HIVE_PROXY"}

    def test_probe_ok_exports_proxy_vars_with_loopback_bypass(self):
        env = {}
        p = _Probe(None)
        st = ob.configure(env, probe=p)
        assert p.calls == [ob.DEFAULT_PROXY] and st == {"mode": "proxy", "proxy": URL, "detail": st["detail"]}
        for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            assert env[k] == URL
        for k in ("NO_PROXY", "no_proxy"):
            assert set(env[k].split(",")) >= {"localhost", "127.0.0.1", "::1"}

    def test_custom_proxy_and_existing_no_proxy_kept(self):
        env = {"ALPHA_HIVE_PROXY": "http://127.0.0.1:9999", "NO_PROXY": "internal.example,localhost"}
        p = _Probe(None)
        st = ob.configure(env, probe=p)
        assert p.calls == ["http://127.0.0.1:9999"] and env["HTTPS_PROXY"] == "http://127.0.0.1:9999"
        parts = env["NO_PROXY"].split(",")
        assert parts[:2] == ["internal.example", "localhost"] and parts.count("localhost") == 1
        assert "127.0.0.1" in parts and st["mode"] == "proxy"

    def test_probe_failure_changes_nothing_and_says_why(self):
        env = {"PATH": "/usr/bin", "NO_PROXY": "x"}
        before = dict(env)
        st = ob.configure(env, probe=_Probe("端口连不上（ConnectionRefusedError）"))
        assert env == before, "探活失败不许改环境（保持原来的出站路径，失败只报出来）"
        assert st["mode"] == "unchanged" and st["proxy"] == URL
        assert "ConnectionRefusedError" in st["detail"] and "重启 Alpha Bot" in st["detail"]

    def test_empty_inherited_value_does_not_count(self):
        env = {"HTTPS_PROXY": ""}
        st = ob.configure(env, probe=_Probe(None))
        assert st["mode"] == "proxy" and env["HTTPS_PROXY"] == URL


class TestProbeProxyLocal:
    """真探活函数，只连本机回环。"""

    def test_closed_port_reports_connect_failure(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        why = ob.probe_proxy(f"http://127.0.0.1:{port}", connect_timeout=1, https_timeout=1)
        assert why is not None and why.startswith("端口连不上")

    def test_open_port_that_is_not_a_proxy_reports_tunnel_failure(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]

        def serve():
            for _ in range(2):           # 第一次是端口探测，第二次是 CONNECT：读一点就关
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                try:
                    c.settimeout(1)
                    c.recv(1024)
                except OSError:
                    pass
                c.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            why = ob.probe_proxy(f"http://127.0.0.1:{port}", connect_timeout=1, https_timeout=2)
        finally:
            srv.close()
            t.join(3)
        assert why is not None and why.startswith("HTTPS 隧道不可用"), "端口开着但不是能用的代理 ⇒ 不许判通"


class TestWiring:
    def test_main_configures_outbound_before_building_service(self, monkeypatch):
        """顺序是关键：yfinance 的会话在第一次取数时建，服务建好之后再改环境就晚了。"""
        import alphabot.__main__ as m
        import alphabot.service as service

        order = []

        class Stop(Exception):
            pass

        monkeypatch.setattr(ob, "configure", lambda: order.append("outbound") or
                            {"mode": "proxy", "proxy": URL, "detail": "x"})

        def fake_service(*a, **k):
            order.append("service")
            raise Stop

        monkeypatch.setattr(service, "AlphaBotService", fake_service)
        with pytest.raises(Stop):
            m.main(["--no-poll"])
        assert order == ["outbound", "service"]

    def test_demo_mode_does_not_probe(self, monkeypatch):
        import alphabot.__main__ as m
        import alphabot.server as server

        monkeypatch.setattr(ob, "configure", lambda: pytest.fail("演示模式不该探代理"))

        class Stop(Exception):
            pass

        seen = {}

        def fake_create_app(*a, **k):
            seen.update(k)
            raise Stop

        monkeypatch.setattr(server, "create_app", fake_create_app)
        with pytest.raises(Stop):
            m.main(["--demo", "--no-poll"])
        assert seen["outbound"] is None

    def test_meta_reports_outbound(self):
        from starlette.testclient import TestClient
        from alphabot import synthetic as syn
        from alphabot.server import create_app
        from alphabot.service import AlphaBotService
        import tempfile

        def client(outbound):
            svc = AlphaBotService(fetch_fn=syn.demo_fetch, demo=True, state_dir=tempfile.mkdtemp())
            return TestClient(create_app(svc, outbound=outbound), base_url="http://127.0.0.1:8765")

        st = {"mode": "unchanged", "proxy": URL, "detail": "代理探活失败"}
        assert client(st).get("/api/meta").json()["outbound"] == st
        assert client(None).get("/api/meta").json()["outbound"] is None

    def test_page_banner_keys_off_the_same_mode_name(self):
        """横幅只在 `unchanged` 时挂：前端写死的模式名必须与后端返回的一致（改名两边一起改）。"""
        js = (ROOT / "alphabot/static/app.js").read_text(encoding="utf-8")
        html = (ROOT / "alphabot/static/index.html").read_text(encoding="utf-8")
        st = ob.configure({}, probe=_Probe("x"))
        assert st["mode"] == "unchanged"
        assert f'ob.mode === "{st["mode"]}"' in js and 'id="outbound-banner"' in html
