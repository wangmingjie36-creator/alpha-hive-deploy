"""Alpha Bot.app（macOS 桌面启动器，v0.45.390）的守卫。

每组回答一句「谁会红」：
  · .app 壳（`TestBundle`）：Info.plist / 可执行位 / 图标齐全；启动脚本在**带空格的仓库路径**下
    真的 cd 进仓库、exec 指定的 Python、PATH 先走 /usr/local/bin；Python 不在 ⇒ 退出码 1 且留日志；
    不覆盖别人的同名 .app；
  · 启动流程（`TestLauncherFlow`）：真起一个演示服务（子进程）——首次问数据根、起服务、开页面；
    再次打开只开页面不重起；`--stop` 停得掉；端口被别人占 / 服务启动即死 ⇒ 弹窗带原因，不干等；
    选的数据根原样传进服务进程的 `ALPHA_HIVE_HOME`；坏配置报出来，不静默当成首次启动；
  · 服务端（`TestShutdownEndpoint`）：`/api/shutdown` 同样要 `X-AlphaBot` 头，没接开关 ⇒ 400。
启动器自己的配置 / 日志都在 `$HOME/Library`，这里一律把 HOME 指到 tmp。
"""
from __future__ import annotations

import os
import plistlib
import socket
import subprocess
import time
from pathlib import Path

import pytest

from alphabot import __version__, launcher as LA, macos_app as MA

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


class FakeUI:
    def __init__(self, answers=(), folders=()):
        self.answers, self.folders, self.calls = list(answers), list(folders), []

    def alert(self, message):
        self.calls.append(("alert", message))

    def ask(self, message, buttons, default):
        self.calls.append(("ask", message))
        return self.answers.pop(0) if self.answers else None

    def choose_folder(self, prompt):
        self.calls.append(("choose", prompt))
        return self.folders.pop(0) if self.folders else None

    def open_url(self, url):
        self.calls.append(("open", url))

    def kinds(self):
        return [c[0] for c in self.calls]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_python(tmp_path, body):
    p = tmp_path / "fakepy"
    p.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    p.chmod(0o755)
    return str(p)


# ── .app 壳 ────────────────────────────────────────────────────────────────

class TestBundle:
    def test_bundle_is_complete(self, tmp_path):
        app = MA.build_app(tmp_path / "Applications")
        c = app / "Contents"
        with open(c / "Info.plist", "rb") as f:
            info = plistlib.load(f)
        assert info["CFBundleIdentifier"] == MA.BUNDLE_ID
        assert info["CFBundleShortVersionString"] == __version__
        exe = c / "MacOS" / info["CFBundleExecutable"]
        assert exe.is_file() and os.access(exe, os.X_OK)
        icon = c / "Resources" / (info["CFBundleIconFile"] + ".icns")
        assert icon.read_bytes()[:4] == b"icns"
        assert subprocess.run(["bash", "-n", str(exe)]).returncode == 0
        assert [p.name for p in (tmp_path / "Applications").iterdir()] == ["Alpha Bot.app"], "临时目录没清干净"

    def test_script_cds_into_spaced_repo_and_execs_python(self, tmp_path, home):
        """仓库就在 `~/Desktop/Alpha Hive`：路径带空格（再加个引号）也得 cd 对、exec 对。"""
        repo = tmp_path / "Alpha Hive 'q'"
        (repo / "alphabot").mkdir(parents=True)
        (repo / "alphabot" / "launcher.py").write_text("", encoding="utf-8")
        out = tmp_path / "rec.txt"
        py = _fake_python(tmp_path, f'{{ pwd; echo "$@"; echo "$PATH"; }} > "{out}"')
        app = MA.build_app(tmp_path / "Apps", repo=repo, python=py)
        r = subprocess.run([str(app / "Contents" / "MacOS" / MA.EXECUTABLE)], env={**os.environ, "HOME": str(home)})
        assert r.returncode == 0
        cwd, args, path = out.read_text(encoding="utf-8").splitlines()
        assert Path(cwd).resolve() == repo.resolve()
        assert args == "-m alphabot.launcher"
        assert path.startswith("/usr/local/bin:")

    def test_missing_python_fails_loudly(self, tmp_path, home):
        app = MA.build_app(tmp_path / "Apps", python=str(tmp_path / "no-such-python"))
        r = subprocess.run([str(app / "Contents" / "MacOS" / MA.EXECUTABLE)], env={**os.environ, "HOME": str(home)})
        assert r.returncode == 1
        log = (home / "Library" / "Logs" / "Alpha Bot" / "launcher.log").read_text(encoding="utf-8")
        assert "找不到" in log and "no-such-python" in log

    def test_rebuild_replaces_own_app_but_never_foreign_one(self, tmp_path):
        dest = tmp_path / "Apps"
        MA.build_app(dest)
        MA.build_app(dest)                                   # 自己的：可以替换
        foreign = tmp_path / "Other"
        (foreign / "Alpha Bot.app" / "Contents").mkdir(parents=True)
        with open(foreign / "Alpha Bot.app" / "Contents" / "Info.plist", "wb") as f:
            plistlib.dump({"CFBundleIdentifier": "com.someone.else"}, f)
        with pytest.raises(MA.BuildError, match="不覆盖"):
            MA.build_app(foreign)
        with open(foreign / "Alpha Bot.app" / "Contents" / "Info.plist", "rb") as f:
            assert plistlib.load(f)["CFBundleIdentifier"] == "com.someone.else"

    def test_main_records_data_root(self, tmp_path, home):
        data = tmp_path / "data"
        data.mkdir()
        assert MA.main(["--dest", str(tmp_path / "Apps"), "--home", str(data)]) == 0
        assert LA.load_config()["alpha_hive_home"] == str(data.resolve())
        assert LA.config_path().is_relative_to(home), "启动器配置没跟着 $HOME 走（被冻在 import 期了？）"


# ── 启动流程 ───────────────────────────────────────────────────────────────

class TestLauncherFlow:
    def test_demo_start_reuse_and_stop(self, home, monkeypatch):
        """真起服务。顺带：用户环境里的 http_proxy 不许把回环探测送去代理。"""
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        for k in ("no_proxy", "NO_PROXY"):          # 否则回环本来就被放行，这条没牙（实测）
            monkeypatch.delenv(k, raising=False)
        port = _free_port()
        ui = FakeUI(answers=[LA.DEMO])
        try:
            assert LA.main(["--port", str(port)], ui=ui) == 0
            assert ui.kinds() == ["ask", "open"]
            p = LA.probe(port)
            assert p["state"] == "alphabot" and p["info"]["demo"] is True and p["info"]["can_shutdown"]
            assert "演示" not in str(LA.load_config()), "演示模式不该写进配置（下次还要能选真数据根）"

            ui2 = FakeUI()
            assert LA.main(["--port", str(port)], ui=ui2) == 0
            assert ui2.kinds() == ["open"], "已在运行 ⇒ 只开页面，不再问、不重起"
            assert LA.probe(port)["info"]["pid"] == p["info"]["pid"]
        finally:
            LA.main(["--stop", "--port", str(port)])
        deadline = time.monotonic() + 15
        while LA.probe(port)["state"] != "free" and time.monotonic() < deadline:
            time.sleep(0.2)
        assert LA.probe(port)["state"] == "free", "--stop 之后服务还在"

    def test_port_taken_by_something_else(self, home, monkeypatch):
        import http.server
        import threading
        srv = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        monkeypatch.setattr(LA, "spawn_server", lambda *a, **k: pytest.fail("端口被占还去起服务"))
        ui = FakeUI()
        try:
            assert LA.main(["--port", str(srv.server_address[1])], ui=ui) == 1
        finally:
            srv.shutdown()
        assert ui.kinds() == ["alert"] and "被别的程序占着" in ui.calls[0][1]

    def test_server_dying_at_start_is_reported_with_log(self, tmp_path, home):
        LA.save_config({"alpha_hive_home": str(tmp_path)})
        py = _fake_python(tmp_path, "echo boom-marker-7; exit 3")
        ui = FakeUI()
        t0 = time.monotonic()
        with pytest.raises(LA.LauncherError) as ei:
            LA.run(ui, port=_free_port(), python=py)
        assert time.monotonic() - t0 < 5, "服务已死还在干等超时"
        assert "退出码 3" in str(ei.value) and "boom-marker-7" in str(ei.value)
        assert "open" not in ui.kinds()

    def test_chosen_root_reaches_server_env_and_is_remembered(self, tmp_path, home, monkeypatch):
        data = tmp_path / "data root"
        data.mkdir()
        out = tmp_path / "env.txt"
        py = _fake_python(tmp_path, f'echo "$ALPHA_HIVE_HOME|$@" > "{out}"; exit 0')
        ui = FakeUI(answers=[LA.CHOOSE], folders=[str(data) + "/"])
        with pytest.raises(LA.LauncherError, match="退出码 0"):
            LA.run(ui, port=_free_port(), python=py)
        env_home, args = out.read_text(encoding="utf-8").strip().split("|")
        assert env_home == str(data)
        assert "--demo" not in args
        assert LA.load_config()["alpha_hive_home"] == str(data)
        # 记住之后不再问
        ui2 = FakeUI()
        with pytest.raises(LA.LauncherError):
            LA.run(ui2, port=_free_port(), python=py)
        assert "ask" not in ui2.kinds()

    def test_cancel_starts_nothing(self, home, monkeypatch):
        monkeypatch.setattr(LA, "spawn_server", lambda *a, **k: pytest.fail("取消了还起服务"))
        ui = FakeUI(answers=[LA.CANCEL])
        assert LA.run(ui, port=_free_port()) == 0
        assert not LA.config_path().exists()

    def test_vanished_root_asks_again(self, tmp_path, home, monkeypatch):
        LA.save_config({"alpha_hive_home": str(tmp_path / "gone")})
        ui = FakeUI(answers=[LA.CANCEL])
        assert LA.resolve_mode(LA.load_config(), ui) is None
        assert "不存在" in ui.calls[0][1]

    def test_corrupt_config_is_reported_not_ignored(self, home):
        LA.config_path().parent.mkdir(parents=True)
        LA.config_path().write_text("{not json", encoding="utf-8")
        ui = FakeUI()
        assert LA.main(["--port", str(_free_port())], ui=ui) == 1
        assert ui.kinds() == ["alert"] and "launcher.json" in ui.calls[0][1]

    def test_reset_forgets_root(self, tmp_path, home):
        LA.save_config({"alpha_hive_home": str(tmp_path), "port": 9999})
        assert LA.main(["--reset"]) == 0
        assert LA.load_config() == {"port": 9999}


# ── 服务端停止开关 ─────────────────────────────────────────────────────────

class TestShutdownEndpoint:
    def _client(self, on_shutdown):
        from starlette.testclient import TestClient
        from alphabot import synthetic as syn
        from alphabot.server import create_app
        from alphabot.service import AlphaBotService
        svc = AlphaBotService(fetch_fn=syn.demo_fetch, demo=True)
        return TestClient(create_app(svc, on_shutdown=on_shutdown), base_url="http://127.0.0.1:8765")

    def test_requires_header_and_calls_hook_once(self):
        calls = []
        c = self._client(lambda: calls.append(1))
        assert c.post("/api/shutdown").status_code == 403
        assert calls == []
        r = c.post("/api/shutdown", headers={"X-AlphaBot": "1"})
        assert r.status_code == 200 and calls == [1]
        ping = c.get("/api/ping").json()
        assert ping["app"] == "alphabot" and ping["can_shutdown"] is True and ping["pid"] == os.getpid()

    def test_without_hook_is_refused_and_hidden(self):
        c = self._client(None)
        assert c.post("/api/shutdown", headers={"X-AlphaBot": "1"}).status_code == 400
        assert c.get("/api/meta").json()["can_shutdown"] is False
