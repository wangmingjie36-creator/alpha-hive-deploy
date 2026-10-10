"""Alpha Bot.app（macOS 桌面启动器，v0.45.390）的守卫。

每组回答一句「谁会红」：
  · .app 壳（`TestBundle`）：Info.plist / 可执行位 / 图标齐全；启动脚本在**带空格的仓库路径**下
    真的 cd 进仓库、exec 指定的 Python、PATH 先走 /usr/local/bin；Python 不在 ⇒ 退出码 1 且留日志；
    不覆盖别人的同名 .app；
  · 启动流程（`TestLauncherFlow`）：真起一个演示服务（子进程）——首次问数据根、起服务、开页面；
    再次打开只开页面不重起；`--stop` 停得掉；端口被别人占 / 服务启动即死 / 依赖缺失 ⇒ 弹窗带原因，不干等；
    选的数据根原样传进服务进程的 `ALPHA_HIVE_HOME`；坏配置报出来，不静默当成首次启动；
  · 服务端（`TestShutdownEndpoint`）：`/api/shutdown` 同样要 `X-AlphaBot` 头，没接开关 ⇒ 400。
启动器自己的配置 / 日志都在 `$HOME/Library`，这里一律把 HOME 指到 tmp；子进程里真 Python 的 import 面不跟着挪——
conftest 的 `_pin_child_user_site` 全会话钉住 `PYTHONUSERBASE`（v0.45.426；守卫在 `test_child_user_site_pin.py`）。跑 .app 启动脚本的测试一律换假 osascript
（`_app_exe`）：真的弹模态对话框，会卡到 pytest-timeout 并把对话框留在屏幕上。
"""
from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from alphabot import __version__, launcher as LA, macos_app as MA

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    # 换 HOME 只为隔离启动器的 `~/Library`；子进程的用户 site 不跟着挪，由 conftest `_pin_child_user_site` 统一钉（v0.45.426）。
    monkeypatch.setenv("HOME", str(h))
    # 真起的演示服务用 mkdtemp 建状态目录、从不清：不圈住就漏进系统临时目录（复查时数到 83 个）
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return h


class FakeUI:
    """缺省「没有原生窗口」⇒ 走浏览器路径（老测试不变）。`window=True` ⇒ `show_window` 立即返回（＝用户关了窗），
    返回前先跑 `on_window(url, is_alive)` 让测试在「窗口开着」时取证。"""

    def __init__(self, answers=(), folders=(), window=False, on_window=None, activate_ok=True, window_error=None):
        self.answers, self.folders, self.calls = list(answers), list(folders), []
        self.window, self.on_window, self.activate_ok = window, on_window, activate_ok
        self.window_error = window_error

    def window_unavailable(self):
        return None if self.window else "测试：无原生窗口"

    def show_window(self, url, is_alive, on_quit):
        self.calls.append(("window", url))
        if self.window_error:
            raise self.window_error
        if self.on_window:
            self.on_window(url, is_alive, on_quit)

    def activate(self, pid):
        self.calls.append(("activate", pid))
        return self.activate_ok

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


def _app_exe(tmp_path, **kw):
    """生成 .app，返回 (启动脚本, 弹窗记录)。弹窗换成假 osascript：只记正文（argv 最后一项）、立即返回。
    真的那个是模态的——Mac 上测试卡到 pytest-timeout，被杀的只是 bash，对话框留在屏幕上（实测）；
    Linux 没有 osascript，CI 上从来不红。"""
    rec = tmp_path / "alerts.txt"
    osa = tmp_path / "fake-osascript"
    osa.write_text(f'#!/bin/sh\nfor a; do last=$a; done\nprintf "%s\\n" "$last" >> "{rec}"\n', encoding="utf-8")
    osa.chmod(0o755)
    app = MA.build_app(tmp_path / "Apps", osascript=str(osa), **kw)
    return app / "Contents" / "MacOS" / MA.EXECUTABLE, rec


# ── .app 壳 ────────────────────────────────────────────────────────────────

class TestBundle:
    def test_bundle_is_complete(self, tmp_path):
        app = MA.build_app(tmp_path / "Applications")
        c = app / "Contents"
        with open(c / "Info.plist", "rb") as f:
            info = plistlib.load(f)
        assert info["CFBundleIdentifier"] == MA.BUNDLE_ID
        assert info["CFBundleShortVersionString"] == __version__
        # 没有它，Finder 双击在 Apple 芯片上把脚本按 x86_64 起，arm64 的 numpy 载不了（0.45.399 实测）；
        # 测试从终端跑是原生 arm64，端到端那条看不出来，只能钉在这里
        assert info["LSArchitecturePriority"][0] == "arm64"
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
        exe, alerts = _app_exe(tmp_path, repo=repo, python=py)
        r = subprocess.run([str(exe)], env={**os.environ, "HOME": str(home)})
        assert not alerts.exists(), alerts.read_text(encoding="utf-8")
        assert r.returncode == 0
        cwd, args, path = out.read_text(encoding="utf-8").splitlines()
        assert Path(cwd).resolve() == repo.resolve()
        assert args == "-m alphabot.launcher"
        assert path.startswith("/usr/local/bin:")

    def test_gui_launch_without_lang_gets_utf8(self, tmp_path, home):
        """Finder / Dock 启动的环境没有 LANG：脚本得补一个 UTF-8 的，已有的不能覆盖。"""
        repo = tmp_path / "repo"
        (repo / "alphabot").mkdir(parents=True)
        (repo / "alphabot" / "launcher.py").write_text("", encoding="utf-8")
        out = tmp_path / "lang.txt"
        py = _fake_python(tmp_path, f'echo "$LANG" > "{out}"')
        exe, _ = _app_exe(tmp_path, repo=repo, python=py)
        env = {k: v for k, v in os.environ.items() if k not in ("LANG", "LC_ALL", "LC_CTYPE")}
        subprocess.run([str(exe)], env={**env, "HOME": str(home)}, check=True)
        assert out.read_text(encoding="utf-8").strip() == "en_US.UTF-8"
        subprocess.run([str(exe)], env={**env, "HOME": str(home), "LANG": "zh_CN.UTF-8"}, check=True)
        assert out.read_text(encoding="utf-8").strip() == "zh_CN.UTF-8"

    def test_dialogs_are_activated(self, tmp_path, monkeypatch):
        """osascript 是后台进程：不先 activate，对话框会压在别的窗口后面，看着像双击没反应。"""
        assert "-e 'activate'" in MA.launch_script(tmp_path, sys.executable)
        seen = []
        monkeypatch.setattr(LA.subprocess, "run", lambda cmd, **k: seen.append(cmd) or
                            subprocess.CompletedProcess(cmd, 0, "选择数据目录…\n", ""))
        ui = LA.MacUI()
        ui.alert("x")
        ui.ask("x", [LA.CANCEL, LA.CHOOSE], LA.CHOOSE)
        ui.choose_folder("x")
        assert len(seen) == 3 and all(c[c.index("on run argv") + 2] == "activate" for c in seen)

    def test_missing_python_fails_loudly(self, tmp_path, home):
        exe, alerts = _app_exe(tmp_path, python=str(tmp_path / "no-such-python"))
        r = subprocess.run([str(exe)], env={**os.environ, "HOME": str(home)})
        assert r.returncode == 1
        shown = alerts.read_text(encoding="utf-8") if alerts.exists() else "（没弹）"
        assert "找不到" in shown and "no-such-python" in shown, "双击的人看的是弹窗，不是日志"
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
        assert MA.main(["--dest", str(tmp_path / "Apps"), "--home", str(data), "--repo", str(REPO)]) == 0
        assert LA.load_config()["alpha_hive_home"] == str(data.resolve())
        assert LA.config_path().is_relative_to(home), "启动器配置没跟着 $HOME 走（被冻在 import 期了？）"


class TestDataRootFollowsLauncher:
    """v0.45.439：生成器说的「双击时会怎样」与启动器同源（`LA.config_state`），环境变量不改写已有配置。

    v0.45.436 的提示只看 `alpha_hive_home`：目录没了也说「沿用」、`demo: true` 时说「会让你选」，双击结果都相反；
    且无条件把当前 shell 的 `ALPHA_HIVE_HOME` 写进配置——开发 shell 常指向沙箱，重新生成一次生产 Alpha Bot 就改读沙箱。
    """

    @staticmethod
    def _main(tmp_path, *extra):
        return MA.main(["--dest", str(tmp_path / "Apps"), "--repo", str(REPO), *extra])

    @pytest.fixture
    def data(self, tmp_path):
        d = tmp_path / "data"
        d.mkdir()
        return d

    def test_kept_data_root_is_reported_not_a_promised_prompt(self, tmp_path, home, monkeypatch, capsys, data):
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        LA.save_config({"alpha_hive_home": str(data)})
        assert self._main(tmp_path) == 0
        out = capsys.readouterr().out
        assert f"沿用启动器配置里的数据根 {data}" in out and "首次双击" not in out, out

    def test_stale_data_root_says_it_will_ask(self, tmp_path, home, monkeypatch, capsys):
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        gone = tmp_path / "moved-away"
        LA.save_config({"alpha_hive_home": str(gone)})
        assert self._main(tmp_path) == 0
        out = capsys.readouterr().out
        assert "不存在了" in out and str(gone) in out and "沿用" not in out, out
        assert LA.config_state(LA.load_config()) == ("stale", str(gone))          # 与启动器的判断一致

    def test_demo_config_says_demo(self, tmp_path, home, monkeypatch, capsys, data):
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        LA.save_config({"demo": True, "alpha_hive_home": str(data)})            # demo 优先：双击直接进演示
        assert self._main(tmp_path) == 0
        out = capsys.readouterr().out
        assert "演示模式" in out and "沿用" not in out and "首次双击" not in out, out

    def test_env_never_overwrites_an_existing_data_root(self, tmp_path, home, monkeypatch, capsys, data):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        LA.save_config({"alpha_hive_home": str(data)})
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(sandbox))
        assert self._main(tmp_path) == 0
        assert LA.load_config() == {"alpha_hive_home": str(data)}, "开发 shell 的沙箱改写了生产 Alpha Bot 的数据根"
        err = capsys.readouterr().err
        assert str(sandbox) in err and "--home" in err, "不同就要说出来，并告诉人真要换怎么换"

    def test_env_fills_only_a_first_run_config(self, tmp_path, home, monkeypatch, data):
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(data))
        assert self._main(tmp_path) == 0
        assert LA.load_config()["alpha_hive_home"] == str(data.resolve())

    def test_explicit_home_replaces_demo_too(self, tmp_path, home, monkeypatch, data):
        """只写数据根、留着 `demo: true` ⇒ 双击仍是演示（demo 优先），等于没换。"""
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        LA.save_config({"demo": True, "port": 8799})
        assert self._main(tmp_path, "--home", str(data)) == 0
        cfg = LA.load_config()
        assert LA.config_state(cfg) == ("home", str(data.resolve())) and cfg["port"] == 8799, cfg


    def test_bad_explicit_home_fails_before_touching_anything(self, tmp_path, home, monkeypatch, capsys, data):
        """`--home` 打错：生成前就退出、返回 1；.app 不生成、配置不动（v0.45.439 先换 .app 再警告、返回 0）。"""
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        LA.save_config({"alpha_hive_home": str(data)})
        typo = tmp_path / "typo"
        assert self._main(tmp_path, "--home", str(typo)) == 1
        assert not (tmp_path / "Apps").exists() and LA.load_config() == {"alpha_hive_home": str(data)}
        assert str(typo) in capsys.readouterr().err


class TestDefaultRepoIsProductionClone:
    """`.app` 缺省 cd 进生产克隆（v0.45.436）；整次生成交给克隆**自己的** `main`（v0.45.451）。

    436 之前缺省 = 生成器所在检出 ⇒ 在开发检出 / worktree 里 `make alphabot-app` 会把 .app 无声指回一份再没人快进
    （或随时被删）的代码。436 只改了 REPO；439 只委托了出壳——数据根仍按委托方那版判断、委托方去概括子进程报错、
    读壳文本核对，且子进程换了 cwd（相对 `--dest` 被建进克隆）。451 把整段交出去、不换 cwd、只核对 import 来源。
    子进程直接写 fd ⇒ 本类看输出用 `capfd`。
    """

    @staticmethod
    def _script_var(apps: Path, var: str) -> str:
        exe = apps / f"{MA.APP_NAME}.app" / "Contents" / "MacOS" / MA.EXECUTABLE
        line = next(ln for ln in exe.read_text(encoding="utf-8").splitlines() if ln.startswith(f"{var}="))
        return shlex.split(line[len(var) + 1:])[0]

    @staticmethod
    def _version_in_plist(apps: Path) -> str:
        with open(apps / f"{MA.APP_NAME}.app" / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f)["CFBundleShortVersionString"]

    @staticmethod
    def _copy_generator(root: Path, version: str) -> Path:
        pkg = root / "alphabot"
        (pkg / "macos").mkdir(parents=True)
        for rel in ("macos_app.py", "launcher.py", "macos/AlphaBot.icns"):
            shutil.copy2(REPO / "alphabot" / rel, pkg / rel)
        (pkg / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        return pkg

    def _clone_at(self, path: Path, monkeypatch, *, macos_app: str = None, mark: bool = True) -> Path:
        """真能跑的假克隆：拷本检出的生成器进去，改版本号，并给它的数据根提示打上「【克隆】」——
        委托若没发生（或只交出去一半），版本号 / 提示就会是本检出的。"""
        import production_clone
        pkg = self._copy_generator(path, "0.0.0+clone")
        if mark:
            src = (pkg / "macos_app.py").read_text(encoding="utf-8")
            assert src.count('f"沿用启动器配置里的数据根 {home}"') == 1
            (pkg / "macos_app.py").write_text(
                src.replace('f"沿用启动器配置里的数据根 {home}"', 'f"【克隆】沿用启动器配置里的数据根 {home}"'), encoding="utf-8")
        if macos_app is not None:
            (pkg / "macos_app.py").write_text(macos_app, encoding="utf-8")
        monkeypatch.setattr(production_clone, "default_dest", lambda: path)
        return path

    def test_default_hands_the_whole_run_to_the_clone(self, tmp_path, home, monkeypatch, capfd):
        clone = self._clone_at(tmp_path / "alpha-hive-prod", monkeypatch)
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        data = tmp_path / "data"
        data.mkdir()
        LA.save_config({"alpha_hive_home": str(data)})
        rc = MA.main(["--dest", str(tmp_path / "Apps")])
        out = capfd.readouterr().out
        assert rc == 0, out
        got = Path(self._script_var(tmp_path / "Apps", "REPO"))
        assert got == clone.resolve() and got != REPO, got        # 正对照：确实不是生成器所在检出
        assert self._version_in_plist(tmp_path / "Apps") == "0.0.0+clone" != __version__, "壳出自委托方，不是克隆"
        assert f"【克隆】沿用启动器配置里的数据根 {data}" in out, "数据根仍由委托方那版处理（只交出去一半）"
        assert out.count("沿用启动器配置里的数据根") == 1, out

    def test_missing_clone_refuses_instead_of_falling_back(self, tmp_path, home, monkeypatch, capfd):
        import production_clone
        gone = tmp_path / "no-such-clone"
        monkeypatch.setattr(production_clone, "default_dest", lambda: gone)
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        assert MA.main(["--dest", str(tmp_path / "Apps")]) == 1
        assert not (tmp_path / "Apps").exists(), "克隆不在还是生成了 .app"
        err = capfd.readouterr().err
        assert str(gone) in err and "production_clone.py setup" in err and "--repo" in err, err

    def test_explicit_repo_wins_over_the_clone(self, tmp_path, home, monkeypatch):
        self._clone_at(tmp_path / "alpha-hive-prod", monkeypatch)
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        assert MA.main(["--dest", str(tmp_path / "Apps"), "--repo", str(REPO)]) == 0
        assert Path(self._script_var(tmp_path / "Apps", "REPO")) == REPO
        assert self._version_in_plist(tmp_path / "Apps") == __version__

    def test_target_output_and_exit_code_pass_through(self, tmp_path, home, monkeypatch, capfd):
        """委托方不再概括子进程的报错：stdout 有输出时，stderr 里真正的错误行照样原样到人眼前。"""
        target = ("import sys\nDELEGATE_PROTOCOL = 1\n"
                  "def main(argv):\n"
                  "    print('stdout noise from target')\n"
                  "    print('target-side error: the real reason', file=sys.stderr)\n"
                  "    return 3\n")
        self._clone_at(tmp_path / "alpha-hive-prod", monkeypatch, macos_app=target, mark=False)
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        assert MA.main(["--dest", str(tmp_path / "Apps")]) == 3
        cap = capfd.readouterr()
        assert "target-side error: the real reason" in cap.err and "stdout noise from target" in cap.out, cap

    def test_old_target_is_refused_not_guessed(self, tmp_path, home, monkeypatch, capfd):
        """目标还不支持整段委托（v0.45.451 之前）⇒ 明说并给出在那份代码里直接跑的命令，不去猜它的参数。"""
        old = "def build_app(dest, **kw):\n    raise SystemExit('old generator must not be called')\n"
        clone = self._clone_at(tmp_path / "alpha-hive-prod", monkeypatch, macos_app=old, mark=False)
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        assert MA.main(["--dest", str(tmp_path / "Apps")]) != 0
        err = capfd.readouterr().err
        assert "太旧" in err and f"make -C {clone.resolve()} alphabot-app" in err, err
        assert "must not be called" not in err and not (tmp_path / "Apps").exists()

    def test_import_redirected_away_from_the_target_is_caught(self, tmp_path, home, monkeypatch, capfd):
        """核对的是 import 来源（真正的风险），不读产物文本：目标的包把 `__path__` 指去别处 ⇒ 必须红。"""
        clone = self._clone_at(tmp_path / "alpha-hive-prod", monkeypatch, mark=False)
        other = tmp_path / "elsewhere"
        self._copy_generator(other, "0.0.0+elsewhere")
        (clone / "alphabot" / "__init__.py").write_text(
            f'__version__ = "0.0.0+clone"\n__path__[:] = [{str(other / "alphabot")!r}]\n', encoding="utf-8")
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        assert MA.main(["--dest", str(tmp_path / "Apps")]) != 0
        err = capfd.readouterr().err
        assert str(other.resolve()) in err and str(clone.resolve()) in err, err
        assert not (tmp_path / "Apps").exists(), "import 到别处的生成器还是生成了 .app"

    def test_relative_dest_lands_in_the_callers_cwd(self, tmp_path, home, monkeypatch):
        """v0.45.439 委托时换了 cwd：`--dest ./Apps` 被建进了克隆。"""
        clone = self._clone_at(tmp_path / "alpha-hive-prod", monkeypatch)
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        assert MA.main(["--dest", "Apps"]) == 0
        assert (work / "Apps" / f"{MA.APP_NAME}.app").is_dir()
        assert not (clone / "Apps").exists(), "相对 --dest 被建进了克隆"

    def test_relative_python_is_written_absolute(self, tmp_path, home, monkeypatch):
        """壳在双击时才解析 `PY`，那时 cwd 早已不是敲命令的地方 ⇒ 入口就定成绝对路径。"""
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        monkeypatch.chdir(tmp_path)
        assert MA.main(["--dest", str(tmp_path / "Apps"), "--repo", str(REPO), "--python", "venv/bin/python3"]) == 0
        assert self._script_var(tmp_path / "Apps", "PY") == str(tmp_path / "venv" / "bin" / "python3")

    def test_bare_python_not_on_path_is_refused(self, tmp_path, home, monkeypatch, capsys):
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        rc = MA.main(["--dest", str(tmp_path / "Apps"), "--repo", str(REPO), "--python", "no-such-python-xyz"])
        assert rc == 1 and not (tmp_path / "Apps").exists()
        assert "no-such-python-xyz" in capsys.readouterr().err


class TestLaunchScriptHints:
    """v0.45.439：进不去代码目录时的处理办法按代码目录在哪给。436 起缺省是生产克隆（不在「桌面」下），
    弹窗却只教人开「桌面文件夹」权限、重跑 make（克隆丢了时那只会报错）。"""

    def test_clone_outside_desktop_gets_rebuild_hint_not_tcc(self, tmp_path, home):
        script = MA.launch_script(home / "alpha-hive-prod", sys.executable)
        assert "production_clone.py setup" in script and "桌面文件夹" not in script

    def test_repo_under_desktop_keeps_tcc_hint(self, tmp_path, home):
        assert "桌面文件夹" in MA.launch_script(home / "Desktop" / "Alpha Hive", sys.executable)

    @pytest.mark.parametrize("where", ["alpha-hive-prod", "Desktop/Alpha Hive"])
    def test_hint_is_safe_inside_the_double_quoted_alert(self, home, where):
        hint = MA._cd_failure_hint(home / where)
        assert not any(c in hint for c in '"$`\\'), hint

    def test_cd_failure_shows_the_hint(self, tmp_path, home):
        missing = home / "alpha-hive-prod"                    # 克隆被删：真跑一次壳，弹窗里得有重建办法
        missing.mkdir()
        (missing / "alphabot").mkdir()
        (missing / "alphabot" / "launcher.py").write_text("", encoding="utf-8")
        exe, alerts = _app_exe(tmp_path, repo=missing, python=sys.executable)
        shutil.rmtree(missing)
        r = subprocess.run([str(exe)], env={**os.environ, "HOME": str(home)})
        assert r.returncode == 1
        shown = alerts.read_text(encoding="utf-8")
        assert "进不去代码目录" in shown and "production_clone.py setup" in shown, shown


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

    def test_missing_dependency_names_the_module(self, tmp_path, home):
        """依赖真缺了（starlette / uvicorn 没装）⇒ 服务 import 即死：弹窗要带出缺的是哪个模块，不干等超时。
        0.45.397 之前 `test_demo_start_reuse_and_stop` 在 Mac 上意外走的就是这条路；这里有意走：
        真解释器加 `-S -E`（不加载 site-packages、不读 PYTHON* 环境）⇒ 在哪台机器上都是「依赖全缺」。"""
        py = _fake_python(tmp_path, f'exec "{sys.executable}" -S -E "$@"')
        ui = FakeUI(answers=[LA.DEMO])
        t0 = time.monotonic()
        with pytest.raises(LA.LauncherError) as ei:
            LA.run(ui, port=_free_port(), python=py)
        assert time.monotonic() - t0 < 10, "服务已死还在干等超时"
        assert "ModuleNotFoundError: No module named" in str(ei.value)
        assert "open" not in ui.kinds()

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

    def test_finder_psn_argument_is_ignored(self, home, monkeypatch):
        """Finder 有时给 .app 传 `-psn_0_NNN`：不能让 argparse exit 2（只进日志、不弹窗 ⇒ 双击没反应）。"""
        monkeypatch.setattr(LA, "spawn_server", lambda *a, **k: pytest.fail("取消了还起服务"))
        ui = FakeUI(answers=[LA.CANCEL])
        assert LA.main(["-psn_0_1234567", "--port", str(_free_port())], ui=ui) == 0
        assert ui.kinds() == ["ask"]

    def test_non_http_listener_counts_as_other(self, home):
        """端口上是个不说 HTTP 的服务（BadStatusLine 不是 OSError）：要报「被别的程序占着」，不是启动器崩溃。"""
        import threading
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()

        def junk():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                with c:
                    c.recv(1024)
                    c.sendall(b"SSH-2.0-OpenSSH_9.0\r\n")
        threading.Thread(target=junk, daemon=True).start()
        port = srv.getsockname()[1]
        try:
            assert LA.probe(port)["state"] == "other"
            ui = FakeUI()
            assert LA.main(["--port", str(port)], ui=ui) == 1
        finally:
            srv.close()
        assert ui.kinds() == ["alert"] and "被别的程序占着" in ui.calls[0][1]

    def test_terminal_commands_report_errors_without_traceback(self, home, capsys):
        """`--stop` 停止请求失败、`--reset` 遇坏配置：打印原因 + 退出码 1，不甩 traceback。"""
        import http.server
        import json
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"app": "alphabot", "can_shutdown": True}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.send_response(500)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            assert LA.main(["--stop", "--port", str(srv.server_address[1])]) == 1
        finally:
            srv.shutdown()
        assert "停止请求失败" in capsys.readouterr().err
        LA.config_path().parent.mkdir(parents=True)
        LA.config_path().write_text("{not json", encoding="utf-8")
        assert LA.main(["--reset"]) == 1
        assert "launcher.json" in capsys.readouterr().err

    def test_reset_forgets_root(self, tmp_path, home):
        LA.save_config({"alpha_hive_home": str(tmp_path), "port": 9999})
        assert LA.main(["--reset"]) == 0
        assert LA.load_config() == {"port": 9999}

    def test_reset_also_forgets_demo(self, tmp_path, home):
        """demo 优先于数据根（`config_state`）：只清数据根，下次双击仍直接进演示，「重新选」不会发生（v0.45.439）。"""
        LA.save_config({"demo": True, "alpha_hive_home": str(tmp_path), "port": 9999})
        assert LA.main(["--reset"]) == 0
        assert LA.config_state(LA.load_config()) == ("first", None)

    @pytest.mark.parametrize("cfg,state", [
        ({}, "first"),
        ({"demo": True, "alpha_hive_home": "<tmp>"}, "demo"),
        ({"alpha_hive_home": "<tmp>"}, "home"),
        ({"alpha_hive_home": "<missing>"}, "stale"),
    ])
    def test_config_state_matches_what_resolve_mode_does(self, tmp_path, home, cfg, state):
        """生成器的提示读 `config_state`，双击读 `resolve_mode`：两者必须同一个结论。"""
        sub = {"<tmp>": str(tmp_path), "<missing>": str(tmp_path / "missing")}
        cfg = {k: sub.get(v, v) if isinstance(v, str) else v for k, v in cfg.items()}
        assert LA.config_state(cfg)[0] == state
        ui = FakeUI(answers=[])                       # 会问 ⇒ ask 拿到 None ⇒ 取消
        got = LA.resolve_mode(cfg, ui)
        asked = "ask" in ui.kinds()
        assert asked == (state in ("first", "stale")), (state, ui.calls)
        if state == "demo":
            assert got == {"demo": True}
        if state == "home":
            assert got == {"demo": False, "home": str(tmp_path)}


# ── 原生窗口（v0.45.407）───────────────────────────────────────────────────

def _wait_state(port, state, timeout=15.0):
    deadline = time.monotonic() + timeout
    while LA.probe(port)["state"] != state and time.monotonic() < deadline:
        time.sleep(0.2)
    return LA.probe(port)["state"]


class TestNativeWindow:
    """真起演示服务；窗口用 FakeUI（关窗＝`show_window` 返回）。真 pywebview 窗口只能在 Mac 上人工 / 探针验。"""

    def test_window_shows_app_started_server_and_close_stops_it(self, home):
        port = _free_port()
        seen = {}

        def while_open(url, is_alive, on_quit):
            seen["alive"] = is_alive()
            seen["ping"] = LA.probe(port)["info"]
            pid = LA._window_pid_path()
            seen["pidfile"] = pid.read_text(encoding="utf-8") if pid.exists() else None

        ui = FakeUI(answers=[LA.DEMO], window=True, on_window=while_open)
        try:
            assert LA.main(["--port", str(port)], ui=ui) == 0
            assert ui.kinds() == ["ask", "window"], "开的是窗口，不是浏览器"
            assert seen["alive"] is True and seen["ping"]["from_app"] is True, "服务要带 --from-app 起"
            assert seen["pidfile"] == str(os.getpid()), "窗口开着时要登记 pid（第二次双击靠它）"
            assert not LA._window_pid_path().exists(), "关窗后 pid 登记没清"
            assert _wait_state(port, "free") == "free", "关窗后 .app 起的服务还在"
        finally:                                   # 收尸放在断言之后：先停了就测不出「关窗不停」
            if LA.probe(port)["state"] == "alphabot":
                LA.stop_running(port)

    def test_cmd_q_stops_server_once(self, home, capsys):
        """⌘Q：Cocoa 直接 exit，`show_window` 不返回——收尾只能靠 on_quit。这里在「窗口开着」时调它，
        要求那一刻服务就停了；之后正常返回再走一遍收尾也只停一次（幂等）。"""
        port = _free_port()
        seen = {}

        def cmd_q(url, is_alive, on_quit):
            on_quit()
            seen["after_quit"] = _wait_state(port, "free")

        ui = FakeUI(answers=[LA.DEMO], window=True, on_window=cmd_q)
        try:
            assert LA.main(["--port", str(port)], ui=ui) == 0
            assert seen["after_quit"] == "free", "⌘Q 之后 .app 起的服务还在"
            out = capsys.readouterr().out
            assert out.count("窗口已关") == 1, f"收尾走了不止一次：{out}"
            assert not LA._window_pid_path().exists()
        finally:
            if LA.probe(port)["state"] == "alphabot":
                LA.stop_running(port)

    def test_window_error_falls_back_to_browser_and_keeps_server(self, home, monkeypatch):
        """pywebview 能 import 但窗口起不来：不能留个没窗口的孤儿服务、也不能弹「启动器出错」了事——退回浏览器。"""
        monkeypatch.setattr(LA, "probe", lambda *a, **k: {"state": "alphabot", "info": {"from_app": True}})
        monkeypatch.setattr(LA, "stop_running", lambda *a, **k: pytest.fail("窗口没起来却去停服务"))
        ui = FakeUI(window=True, window_error=RuntimeError("no WindowServer"))
        assert LA.main(["--port", str(_free_port())], ui=ui) == 0
        assert ui.kinds() == ["window", "open"]
        assert not LA._window_pid_path().exists(), "窗口没起来，pid 登记要撤掉"

    def test_busy_server_is_not_taken_for_gone(self, home, monkeypatch):
        """探测超时（服务忙）≠ 服务没了：只有端口空了才让窗口自关。"""
        states = {"s": "other"}
        monkeypatch.setattr(LA, "probe", lambda *a, **k: {"state": states["s"], "info": {"from_app": False}})
        seen = []

        def check(url, is_alive, on_quit):
            seen.append(is_alive())
            states["s"] = "free"
            seen.append(is_alive())

        states["s"] = "alphabot"
        ui = FakeUI(window=True, on_window=lambda u, a, q: (states.update(s="other"), check(u, a, q)))
        assert LA.main(["--port", str(_free_port())], ui=ui) == 0
        assert seen == [True, False]

    def test_close_leaves_terminal_started_server_running(self, home):
        """终端里 `make alphabot` 起的服务（没有 --from-app）：开窗口看它可以，关窗不许把它停了。"""
        port = _free_port()
        proc = subprocess.Popen([sys.executable, "-m", "alphabot", "--port", str(port), "--demo", "--no-poll"],
                                cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            assert _wait_state(port, "alphabot", 30) == "alphabot"
            assert LA.probe(port)["info"]["from_app"] is False
            ui = FakeUI(window=True)
            assert LA.main(["--port", str(port)], ui=ui) == 0
            assert ui.kinds() == ["window"]
            time.sleep(0.5)
            assert LA.probe(port)["state"] == "alphabot", "关窗把终端起的服务停了"
        finally:
            LA.stop_running(port)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_second_launch_brings_existing_window_forward(self, home, monkeypatch):
        owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "alphabot.launcher"])
        try:
            LA._window_pid_path().parent.mkdir(parents=True)
            LA._window_pid_path().write_text(str(owner.pid), encoding="utf-8")
            monkeypatch.setattr(LA, "spawn_server", lambda *a, **k: pytest.fail("窗口已开着还去起服务"))
            ui = FakeUI(window=True)
            assert LA.run(ui, port=_free_port()) == 0
            assert ui.calls == [("activate", owner.pid)]
            # 调不到前面 ⇒ 不能「双击没反应」：照常往下走（这里端口空、用户取消）
            ui2 = FakeUI(window=True, activate_ok=False, answers=[LA.CANCEL])
            assert LA.run(ui2, port=_free_port()) == 0
            assert ui2.kinds() == ["activate", "ask"]
        finally:
            owner.kill()
            owner.wait()

    def test_stale_or_reused_window_pid_is_ignored(self, home, monkeypatch):
        LA._window_pid_path().parent.mkdir(parents=True)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        LA._window_pid_path().write_text(str(dead.pid), encoding="utf-8")
        assert LA.window_owner() is None, "进程已退出"
        LA._window_pid_path().write_text(str(os.getppid()), encoding="utf-8")
        assert LA.window_owner() is None, "pid 被别的进程（不是 alphabot.launcher）占着"
        LA._window_pid_path().write_text("garbage", encoding="utf-8")
        assert LA.window_owner() is None

    def test_long_command_line_survives_piped_ps_width(self, home, monkeypatch):
        """v0.45.412：procps 的 ps 管道输出本不限宽，但环境里设了 COLUMNS 就按它截（src/ps/global.c），
        `-ww` 压过 COLUMNS（parser.c）。CI 上确实被截了（owner 命令行 99 列、标记从第 83 列起；不带 `-ww` 三次
        全红、带了转绿；COLUMNS 是谁设的未查明）⇒ 开着的窗口被当成没有、第二次双击去问数据根。
        macOS 的 BSD ps 管道输出恒不限宽（实测连 COLUMNS 都不理），本机真跑永远红不了
        ⇒ 用按 procps 源码行事的假 ps + `COLUMNS=80` 钉住。"""
        ci_cmdline = ("/opt/hostedtoolcache/Python/3.11.16/x64/bin/python "
                      "-c import time; time.sleep(60) alphabot.launcher")     # 10-04 CI 上 owner 的真实形状
        assert ci_cmdline.index("alphabot.launcher") >= 80, "夹具自检：标记要真在 80 列之外，否则这条绿不算数"
        real_run, asked = subprocess.run, []

        def procps_like(cmd, *a, **k):
            if not str(cmd[0]).endswith("ps"):
                return real_run(cmd, *a, **k)
            asked.append(list(cmd))
            cols = (k.get("env") or os.environ).get("COLUMNS")
            unlimited = "-ww" in cmd or cmd.count("-w") >= 2 or not cols
            out = ci_cmdline if unlimited else ci_cmdline[:int(cols)]
            return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

        monkeypatch.setenv("COLUMNS", "80")

        owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            LA._window_pid_path().parent.mkdir(parents=True)
            LA._window_pid_path().write_text(str(owner.pid), encoding="utf-8")
            monkeypatch.setattr(subprocess, "run", procps_like)
            assert LA.window_owner() == owner.pid, "ps 输出被截断后认不出开着的窗口——要带 -ww"
            assert asked, "window_owner 没调 ps：假 ps 没接上，这条绿不算数"
        finally:
            owner.kill()
            owner.wait()

    def test_no_native_window_falls_back_to_browser_and_says_why(self, home, monkeypatch, capsys):
        monkeypatch.setattr(LA, "probe", lambda *a, **k: {"state": "alphabot", "info": {"from_app": False}})
        ui = FakeUI()
        assert LA.main(["--port", str(_free_port())], ui=ui) == 0
        assert ui.kinds() == ["open"]
        assert "原生窗口不可用（测试：无原生窗口）" in capsys.readouterr().out, "退回浏览器要在日志里说为什么"
        ui2 = FakeUI(window=True)
        assert LA.main(["--browser", "--port", str(_free_port())], ui=ui2) == 0
        assert ui2.kinds() == ["open"], "--browser 要能绕开窗口"

    def test_missing_pywebview_is_reported_not_raised(self, monkeypatch):
        from alphabot import window
        monkeypatch.setitem(sys.modules, "webview", None)          # import webview ⇒ ImportError
        assert "webview" in (window.unavailable_reason() or "")

    def test_window_closes_itself_when_server_goes_away(self):
        """页面「停止服务」⇒ 服务没了 ⇒ 窗口自己关；一次探测失败不算（连续 2 次）；停表即退出。"""
        import threading
        from alphabot import window
        seq, gone = [True, False, True, False, False, True], []
        window.watch_server(lambda: seq.pop(0), lambda: gone.append(1), threading.Event(), interval=0.001)
        assert gone == [1] and seq == [True], "要在第二次连续探不到时关（单次失败不算），关完就停"
        stop = threading.Event()
        stop.set()
        window.watch_server(lambda: False, lambda: pytest.fail("停表后还在关窗"), stop, interval=0.001)


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
        assert ping["from_app"] is False, "缺省不是 .app 起的（关窗不该停它）"

    def test_without_hook_is_refused_and_hidden(self):
        c = self._client(None)
        assert c.post("/api/shutdown", headers={"X-AlphaBot": "1"}).status_code == 400
        assert c.get("/api/meta").json()["can_shutdown"] is False
