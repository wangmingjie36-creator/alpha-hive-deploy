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
from pathlib import Path, PurePath

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

    def notice(self, message):
        self.calls.append(("notice", message))

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


def _fake_code_dir(repo):
    """一份「可用的代码目录」：文件清单只取 `MA.GENERATOR_FILES`（判据唯一真相，v0.45.458），测试里不另抄一份。"""
    for rel in MA.GENERATOR_FILES:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("", encoding="utf-8")
    return repo


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
        _fake_code_dir(repo)
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
        _fake_code_dir(repo)
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


    def test_explicit_home_vanishing_after_validation_is_still_a_failure(self, tmp_path, home, monkeypatch, data):
        """生成前核对过、写配置时目录却没了（被删 / 挪走）：显式要求没做成，返回 1，不当警告。"""
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        assert MA._record_data_root(tmp_path / "gone-in-between") == 1
        assert MA._record_data_root(data) == 0 and LA.load_config()["alpha_hive_home"] == str(data.resolve())

    def test_bad_explicit_home_fails_before_touching_anything(self, tmp_path, home, monkeypatch, capsys, data):
        """`--home` 打错：生成前就退出、返回 1；.app 不生成、配置不动（v0.45.439 先换 .app 再警告、返回 0）。"""
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        LA.save_config({"alpha_hive_home": str(data)})
        typo = tmp_path / "typo"
        assert self._main(tmp_path, "--home", str(typo)) == 1
        assert not (tmp_path / "Apps").exists() and LA.load_config() == {"alpha_hive_home": str(data)}
        assert str(typo) in capsys.readouterr().err


    def test_relative_explicit_home_is_refused(self, tmp_path, home, monkeypatch, capsys, data):
        """数据根写进配置、双击时在别的目录下解析 ⇒ `--home` 必须绝对；拒绝时什么都不改（v0.45.456）。"""
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        monkeypatch.chdir(tmp_path)
        assert self._main(tmp_path, "--home", "data") == 1
        assert not (tmp_path / "Apps").exists() and LA.config_state(LA.load_config())[0] == "first"
        assert "绝对路径" in capsys.readouterr().err

    def test_relative_env_home_is_not_recorded(self, tmp_path, home, monkeypatch, capsys, data):
        """顺手记的环境变量同理：相对就不记、说明为什么，但不算失败（不是人明确要求的事）。"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ALPHA_HIVE_HOME", "data")
        assert self._main(tmp_path) == 0
        assert LA.config_state(LA.load_config())[0] == "first"
        assert "相对路径" in capsys.readouterr().err

    @pytest.mark.parametrize("flag", ["--dest", "--python", "--home", "--repo"])
    def test_empty_value_is_rejected_not_defaulted(self, tmp_path, home, monkeypatch, capsys, data, flag):
        """显式给空值（脚本里 `--python "$PY"` 而 PY 为空）是无效输入，不是「没给」（v0.45.456 曾悄悄换成缺省）。"""
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        LA.save_config({"alpha_hive_home": str(data)})
        argv = ["--dest", str(tmp_path / "Apps"), "--repo", str(REPO)]
        if flag in argv:
            argv[argv.index(flag) + 1] = ""
        else:
            argv += [flag, ""]
        assert MA.main(argv) == 1
        assert not (tmp_path / "Apps").exists() and LA.load_config() == {"alpha_hive_home": str(data)}
        assert f"{flag} 给了空值" in capsys.readouterr().err

    def test_generation_states_the_interpreter_written(self, tmp_path, home, monkeypatch, capsys):
        """写进 .app 的解释器当场说出来；现在不存在 / 不可执行就当场警告，不等到双击（v0.45.458）。"""
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        ghost = tmp_path / "no" / "python3"
        assert self._main(tmp_path, "--python", str(ghost)) == 0
        cap = capsys.readouterr()
        assert f"写进 .app 的解释器：{ghost}" in cap.out and "现在不能执行（不存在）" in cap.err, cap
        assert self._main(tmp_path, "--python", str(tmp_path)) == 0      # 目录：X_OK 为真，458 不警告（v0.45.461）
        cap = capsys.readouterr()
        assert "现在不能执行（不是文件" in cap.err, cap
        assert self._main(tmp_path, "--python", sys.executable) == 0
        cap = capsys.readouterr()
        assert f"写进 .app 的解释器：{sys.executable}" in cap.out and "不能执行" not in cap.err, cap

    def test_literal_tilde_env_home_is_not_expanded(self, tmp_path, home, monkeypatch, capsys):
        """环境里字面的 `~/x`（plist / 加了引号的 export 都不展开）：扫描、MCP 按字面读 `./~/x`。458 自己 expanduser，
        在这里算成「绝对、与配置相同」、首次还记下 `$HOME/x`——与扫描读的不是一个目录，分歧被藏住（v0.45.461）。"""
        (home / "d").mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", "~/d")
        assert self._main(tmp_path) == 0
        err = capsys.readouterr().err
        assert "相对路径" in err and "不会被展开" in err, err
        assert LA.config_state(LA.load_config())[0] == "first", "字面 ~ 不该被展开后记进配置"
        LA.save_config({"alpha_hive_home": str(home / "d")})
        assert self._main(tmp_path) == 0
        err = capsys.readouterr().err
        assert "不会被展开" in err and "与启动器配置不同" not in err, err

    def test_explicit_home_silences_the_env_note(self, tmp_path, home, monkeypatch, capsys, data):
        """给了 --home，环境变量根本不参与：不许再提示「环境变量是相对路径、要设数据根用 --home」（自相矛盾）。"""
        monkeypatch.setenv("ALPHA_HIVE_HOME", "relative/dir")
        assert self._main(tmp_path, "--home", str(data)) == 0
        assert "ALPHA_HIVE_HOME" not in capsys.readouterr().err
        assert LA.config_state(LA.load_config()) == ("home", str(data.resolve()))

    def test_relative_env_home_is_never_compared(self, tmp_path, home, monkeypatch, capsys, data):
        """相对的 ALPHA_HIVE_HOME 整个不参与：v0.45.456 在「与配置比较」那支仍按当前 cwd 解析，
        `make -C 克隆` 下就误报「与启动器配置不同」。"""
        LA.save_config({"alpha_hive_home": str(data)})
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)                  # 在这里解析 "data" 会得到另一个目录
        monkeypatch.setenv("ALPHA_HIVE_HOME", "data")
        assert self._main(tmp_path) == 0
        err = capsys.readouterr().err
        assert "相对路径" in err and "与启动器配置不同" not in err, err
        assert LA.load_config() == {"alpha_hive_home": str(data)}

    def test_empty_env_home_counts_as_unset(self, tmp_path, home, monkeypatch, capsys):
        """空串按未设（与 hive_logger.resolve_data_root 同口径），不报相对路径、也不记。"""
        monkeypatch.setenv("ALPHA_HIVE_HOME", "")
        assert self._main(tmp_path) == 0
        assert "ALPHA_HIVE_HOME" not in capsys.readouterr().err
        assert LA.config_state(LA.load_config())[0] == "first"


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
        """拷的文件清单取 `MA.GENERATOR_FILES`（判据唯一真相）+ 图标：生成器在目标里 import 什么，假克隆就有什么。"""
        pkg = root / "alphabot"
        (pkg / "macos").mkdir(parents=True)
        for rel in (*MA.GENERATOR_FILES, "alphabot/macos/AlphaBot.icns"):
            shutil.copy2(REPO / rel, root / rel)
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
        # 认的是 import 来源核对那句（再委托闸也会挡住，但那是另一道防线——各自要有牙）
        assert "委托 import 到的生成器在" in err and str(other.resolve()) in err and str(clone.resolve()) in err, err
        assert not (tmp_path / "Apps").exists(), "import 到别处的生成器还是生成了 .app"

    def test_delegated_run_never_delegates_again(self, tmp_path, home, monkeypatch, capfd):
        """目标若认定自己不是 `--repo`（这里把它的 `_repo_root` 改错），没有这道闸就一层层委托到被杀（变异实测 61 s 超时）。"""
        clone = self._clone_at(tmp_path / "alpha-hive-prod", monkeypatch, mark=False)
        src = (clone / "alphabot" / "macos_app.py").read_text(encoding="utf-8")
        body = "    return Path(__file__).resolve().parent.parent\n"
        assert src.count(body) == 1
        (clone / "alphabot" / "macos_app.py").write_text(
            src.replace(body, f"    return Path({str(tmp_path / 'confused')!r})\n"), encoding="utf-8")
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        assert MA.main(["--dest", str(tmp_path / "Apps")]) == 1
        err = capfd.readouterr().err
        assert "已是被委托的那次" in err and not (tmp_path / "Apps").exists(), err

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

    def test_handoff_itself_keeps_the_callers_cwd(self, tmp_path, home, monkeypatch):
        """两道防线各自要有牙：绕过 main 的绝对化，直接交给目标一个相对 `--dest`，也得落在调用者 cwd 下。"""
        clone = self._clone_at(tmp_path / "alpha-hive-prod", monkeypatch)
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        rc = MA.run_target_generator(clone.resolve(), ["--repo", str(clone.resolve()), "--dest", "Apps",
                                                       "--python", sys.executable])
        assert rc == 0 and (work / "Apps" / f"{MA.APP_NAME}.app").is_dir() and not (clone / "Apps").exists()

    def test_handoff_forwards_only_what_was_given(self, tmp_path, home, monkeypatch):
        """委托方只转发人明确给了的参数：缺省由目标定；要持久写下的值原样转发、由目标按它的规则核对（v0.45.456）。"""
        clone = self._clone_at(tmp_path / "alpha-hive-prod", monkeypatch)
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        monkeypatch.chdir(tmp_path)
        seen = []
        monkeypatch.setattr(MA, "run_target_generator", lambda repo, argv: seen.append((repo, argv)) or 0)
        assert MA.main([]) == 0
        assert seen[-1] == (clone.resolve(), ["--repo", str(clone.resolve())]), "没给的参数也被委托方填了缺省"
        assert MA.main(["--dest", "Apps", "--python", "venv/bin/python3", "--home", "data"]) == 0
        argv = seen[-1][1]
        vals = {argv[i]: argv[i + 1] for i in range(0, len(argv), 2)}
        assert vals["--dest"] == str(tmp_path / "Apps"), argv            # 当场用：按本进程 cwd 定死
        assert vals["--python"] == "venv/bin/python3" and vals["--home"] == "data", argv   # 持久值：原样，目标判

    def test_handoff_does_not_judge_launcher_config(self, tmp_path, home, monkeypatch, capfd):
        """配置读不读得了是 launcher 的语义，归目标那一版：委托方那版读不了，不许在委托前就替目标拒绝（v0.45.456）。"""
        clone = self._clone_at(tmp_path / "alpha-hive-prod", monkeypatch)
        with open(clone / "alphabot" / "launcher.py", "a", encoding="utf-8") as f:
            f.write("\n\ndef load_config():          # 目标这一版认得任何配置\n    return {}\n")
        LA.config_path().parent.mkdir(parents=True, exist_ok=True)
        LA.config_path().write_text("{本版读不了", encoding="utf-8")
        with pytest.raises(LA.LauncherError):
            LA.load_config()                                          # 正对照：委托方这一版确实读不了
        data = tmp_path / "data"
        data.mkdir()
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        rc = MA.main(["--dest", str(tmp_path / "Apps"), "--home", str(data)])
        assert rc == 0, capfd.readouterr().err
        assert (tmp_path / "Apps" / f"{MA.APP_NAME}.app").is_dir()

    @pytest.mark.parametrize("bad", ["venv/bin/python3", "python3"])
    def test_relative_or_bare_python_is_refused(self, tmp_path, home, monkeypatch, capsys, bad):
        """`PY=` 双击时才解析，那时 cwd / PATH 都不同；裸 `python3` 还可能是系统 3.9（CLAUDE.md）⇒ 拒绝，不猜。"""
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        monkeypatch.chdir(tmp_path)
        rc = MA.main(["--dest", str(tmp_path / "Apps"), "--repo", str(REPO), "--python", bad])
        assert rc == 1 and not (tmp_path / "Apps").exists()
        err = capsys.readouterr().err
        assert "绝对路径" in err and bad in err, err

    def test_python_rule_is_enforced_by_the_target_too(self, tmp_path, home, monkeypatch, capfd):
        """委托时相对 `--python` 原样转发 ⇒ 由目标按它的规则拒（委托方不预判，也不放过）。"""
        self._clone_at(tmp_path / "alpha-hive-prod", monkeypatch)
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        assert MA.main(["--dest", str(tmp_path / "Apps"), "--python", "venv/bin/python3"]) == 1
        assert "绝对路径" in capfd.readouterr().err and not (tmp_path / "Apps").exists()

    @pytest.mark.parametrize("keep", ["alphabot/launcher.py", "alphabot/macos_app.py"])
    def test_half_clone_on_the_default_path_is_reported_plainly(self, tmp_path, home, monkeypatch, capfd, keep):
        """缺省路径与 `--repo` 共用同一判据（`MA.generator_dir_problem`）：半截克隆在缺省路径下也直白拒绝，
        不再「判可用 ⇒ 委托 ⇒ 子进程 import 失败甩 traceback」（v0.45.458）。"""
        import production_clone
        half = tmp_path / "alpha-hive-prod"
        (half / keep).parent.mkdir(parents=True)
        (half / keep).write_text("", encoding="utf-8")
        monkeypatch.setattr(production_clone, "default_dest", lambda: half)
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        assert MA.main(["--dest", str(tmp_path / "Apps")]) == 1
        err = capfd.readouterr().err
        missing = next(f for f in MA.GENERATOR_FILES if f != keep)
        assert missing in err and "production_clone.py setup" in err and "Traceback" not in err, err
        assert not (tmp_path / "Apps").exists()

    def test_generator_files_cover_every_repo_module_the_generator_imports(self):
        """判据要覆盖生成器在目标里 import 的每个仓库模块（含函数体内的延迟 import）：漏一个 ⇒ 缺它的目录被判可用、
        委托后子进程甩 traceback（v0.45.461 实测：新 import 的 production_clone / hive_logger 不在 458 的清单里）。"""
        import ast
        tree = ast.parse((REPO / "alphabot" / "macos_app.py").read_text(encoding="utf-8"))
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                mods.add(node.module)
                mods.update(f"{node.module}.{a.name}" for a in node.names)
        needed = set()
        for m in mods:
            rel = m.replace(".", "/")
            for cand in (f"{rel}.py", f"{rel}/__init__.py"):
                if (REPO / cand).is_file():
                    needed.add(cand)
        assert "production_clone.py" in needed and "alphabot/launcher.py" in needed, needed   # 扫描本身没失明
        assert needed <= set(MA.GENERATOR_FILES), needed - set(MA.GENERATOR_FILES)

    @pytest.mark.parametrize("kw, msg", [({"repo": ""}, "repo 给了空值"), ({"repo": "  "}, "repo 给了空值"),
                                         ({"python": ""}, "绝对路径"), ({"python": "python3"}, "绝对路径")])
    def test_build_app_enforces_the_input_rules_itself(self, tmp_path, monkeypatch, kw, msg):
        """`build_app` 是写进 .app 的唯一出口：显式空值拒绝、解释器须绝对——不只在命令行入口（v0.45.461）。
        458 让 `repo=""` 成了「当前目录」：cwd 恰好是一份代码目录时，静默生成一个指向它的 .app（这里就让 cwd 是）。"""
        monkeypatch.chdir(_fake_code_dir(tmp_path / "code"))
        with pytest.raises(MA.BuildError, match=msg):
            MA.build_app(tmp_path / "Apps", **kw)
        assert not (tmp_path / "Apps").exists()

    def test_one_criterion_for_all_three_callers(self, tmp_path):
        """缺省克隆、`--repo`、`build_app` 对同一个半截目录给同一结论（判据只在一处）。"""
        half = tmp_path / "half"
        (half / "alphabot").mkdir(parents=True)
        (half / "alphabot" / "launcher.py").write_text("", encoding="utf-8")
        problem = MA.generator_dir_problem(half)
        assert problem and "alphabot/macos_app.py" in problem
        with pytest.raises(MA.BuildError, match="macos_app.py"):
            MA._check_generator_dir(half)
        with pytest.raises(MA.BuildError, match="macos_app.py"):
            MA.build_app(tmp_path / "Apps", repo=half)

    @pytest.mark.parametrize("kind", ["missing", "not-a-repo"])
    def test_repo_typo_is_reported_plainly(self, tmp_path, home, monkeypatch, capfd, kind):
        """v0.45.451 删预检后，`--repo` 拼错被报成「import 被重定向 / PYTHONPATH？」（误诊）或甩 traceback。"""
        target = tmp_path / "alpha-hive-prd"
        if kind == "not-a-repo":
            target.mkdir()
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        monkeypatch.chdir(REPO)                       # 最坏情形：调用者 cwd 正好是一份检出（import 会落到它）
        assert MA.main(["--dest", str(tmp_path / "Apps"), "--repo", str(target)]) == 1
        err = capfd.readouterr().err
        assert ("不存在" if kind == "missing" else "不是 Alpha Hive 代码目录") in err, err
        assert "import" not in err and "Traceback" not in err, err


#: 真跑 make 时子进程只继承这些（PYTHONUSERBASE：conftest 钉的用户 site，换 HOME 后靠它找 numpy 等）
_MAKE_ENV_KEEP = ("PATH", "PYTHONUSERBASE", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")


class TestCanonicalMakeEntry:
    """真跑推荐入口 `make -C <仓库> alphabot-app`（v0.45.456）：此前只测 `MA.main`，Makefile 目标、`-m` 从仓库根
    import、`default_repo()==_repo_root()` 的本地分支、make 变量传参都从没被执行过。

    不造假克隆：把临时 HOME 下的 `alpha-hive-prod` 软链到本仓库，`production_clone.default_dest()` 就指向它；
    跑生成器的解释器经 `ALPHABOT_GEN_PY` 换成当前解释器（CI 上没有 /usr/local/bin/python3）。
    """

    @staticmethod
    def _make(home, *make_vars, env_extra=None):
        if not (home / "alpha-hive-prod").is_symlink():     # 同一测试里可连跑两次（「照提醒做」再生成一遍）
            (home / "alpha-hive-prod").symlink_to(REPO, target_is_directory=True)
        # 白名单：只带 make 与生成器真正要的（v0.45.458）。此前从 os.environ 减掉 ALPHA_HIVE_HOME / PYTHON（黑名单），
        # 外层 make 的命令行变量经 MAKEFLAGS 照样漏进子 make（`make test PYTHON=…` 时 origin 成了 command line）
        env = {k: os.environ[k] for k in _MAKE_ENV_KEEP if k in os.environ}
        env["HOME"] = str(home)
        env.update(env_extra or {})
        return subprocess.run(["make", "-s", "-C", str(REPO), "alphabot-app", f"ALPHABOT_GEN_PY={sys.executable}",
                               *make_vars], env=env, capture_output=True, text=True, timeout=120)

    @staticmethod
    def _py_in_app(home):
        exe = (home / "Applications" / f"{MA.APP_NAME}.app" / "Contents" / "MacOS" / MA.EXECUTABLE)
        line = next(ln for ln in exe.read_text(encoding="utf-8").splitlines() if ln.startswith("PY="))
        return shlex.split(line[len("PY="):])[0]

    def test_environment_python_is_ignored(self, home):
        """make 把同名环境变量导入成 make 变量；`export PYTHON=python3`（node-gyp 等要求）不许让推荐入口失败（v0.45.458）。"""
        r = self._make(home, env_extra={"PYTHON": "python3"})
        assert r.returncode == 0, r.stderr
        assert self._py_in_app(home) == MA.DEFAULT_PYTHON, "环境里的 PYTHON 被当成了 --python"
        assert "忽略环境变量 PYTHON=python3" in r.stdout + r.stderr, "忽略要说出来"

    def test_helper_is_hermetic_against_ambient_make_flags(self, home, monkeypatch):
        """守的是**测试辅助** `_make`，不是产品：跑测试的 make（`make test PYTHON=…`）经 MAKEFLAGS 带下来的变量不许改变
        本类结论（白名单 `_MAKE_ENV_KEEP`）。产品在同一情形下的行为见下一条。"""
        monkeypatch.setenv("MAKEFLAGS", " -- PYTHON=python3")
        r = self._make(home)
        assert r.returncode == 0, r.stderr
        assert self._py_in_app(home) == MA.DEFAULT_PYTHON

    def test_outer_make_command_line_python_is_checked_not_ignored(self, home):
        """外层 make 命令行上的 PYTHON 经 MAKEFLAGS 传下来，origin 是 command line：那是人写的值，照收照核
        （裸名字照样拒、不生成），不当环境变量悄悄忽略（v0.45.461 写进 Makefile 注释的行为，这里钉住）。"""
        r = self._make(home, env_extra={"MAKEFLAGS": " -- PYTHON=python3"})
        assert r.returncode != 0 and "绝对路径" in r.stderr, r.stderr
        assert "忽略环境变量" not in r.stdout + r.stderr
        assert not (home / "Applications").exists()

    def test_command_line_python_wins_over_environment(self, home):
        r = self._make(home, f"PYTHON={sys.executable}", env_extra={"PYTHON": "python3"})
        assert r.returncode == 0, r.stderr
        assert self._py_in_app(home) == sys.executable

    def test_entry_builds_from_the_clone_itself(self, home):
        r = self._make(home, f"PYTHON={sys.executable}")
        assert r.returncode == 0, r.stderr
        app = home / "Applications" / f"{MA.APP_NAME}.app"
        exe = (app / "Contents" / "MacOS" / MA.EXECUTABLE).read_text(encoding="utf-8")
        repo_line = next(ln for ln in exe.splitlines() if ln.startswith("REPO="))
        assert Path(shlex.split(repo_line[len("REPO="):])[0]).resolve() == REPO
        assert "整次生成交给" not in r.stderr, "推荐入口就在克隆里，不该再委托"

    def test_entry_rejects_relative_python(self, home):
        """`make -C` 先切了目录：相对 PYTHON 若照解析，会被当成克隆下的路径（v0.45.451 的形状换了入口）。"""
        r = self._make(home, "PYTHON=./venv/bin/python3")
        assert r.returncode != 0 and "绝对路径" in r.stderr, r.stderr
        assert not (home / "Applications").exists()


class TestLaunchScriptHints:
    """v0.45.439：进不去代码目录时的处理办法按代码目录在哪给。436 起缺省是生产克隆（不在「桌面」下），
    弹窗却只教人开「桌面文件夹」权限、重跑 make（克隆丢了时那只会报错）。"""

    def test_clone_outside_desktop_gets_rebuild_hint_not_tcc(self, tmp_path, home):
        script = MA.launch_script(home / "alpha-hive-prod", sys.executable)
        assert "production_clone.py setup" in script and "桌面文件夹" not in script

    def test_repo_under_desktop_keeps_tcc_hint(self, tmp_path, home):
        assert "桌面文件夹" in MA.launch_script(home / "Desktop" / "Alpha Hive", sys.executable)

    @pytest.mark.parametrize("py", [MA.DEFAULT_PYTHON, "/opt/my py/bin/python3", "/opt/a(b)&c/python3"])
    @pytest.mark.parametrize("where", ["alpha-hive-prod", "Desktop/Alpha Hive", "Desktop/it's \"$x\" `y`"])
    def test_hint_is_safe_inside_the_double_quoted_alert(self, home, where, py):
        """解释器 v0.45.498 起也进这些提示（重新生成要沿用它）：带空格 / 括号的照样不许生出 " $ ` \\。"""
        for hint in (MA._repo_unusable_hint(home / where, py), MA._python_fix_hint(home / where)):
            assert not any(c in hint for c in '"$`\\'), hint

    @pytest.mark.parametrize("where", ["alpha-hive-prod", "dev"])
    def test_regen_commands_in_the_shell_keep_the_recorded_interpreter(self, home, where):
        """壳头注释与「代码目录挪了」那句也是「怎么重新生成」：照抄不许把自定义解释器换回缺省（v0.45.498；461 起就漏）。
        缺省解释器 ⇒ 命令里不写（推荐入口生成的就是它）——已装的缺省 .app 内容因此不变，不会为这次修复多提醒一次。"""
        if where == "alpha-hive-prod":
            (home / where).symlink_to(REPO, target_is_directory=True)
        repo = (home / where) if where == "alpha-hive-prod" else _fake_code_dir(home / where)
        custom = "/opt/my py/bin/python3"
        script = MA.launch_script(repo, custom)
        flag = "PYTHON=" if where == "alpha-hive-prod" else "--python "
        expected = flag + shlex.quote(custom)
        regen_cmds = [ln for ln in script.splitlines() if "重新生成（" in ln and "新解释器" not in ln]
        assert regen_cmds and all(expected in ln for ln in regen_cmds), regen_cmds
        default = MA.launch_script(repo, MA.DEFAULT_PYTHON)
        default_cmds = "".join(ln for ln in default.splitlines() if "重新生成（" in ln and "新解释器" not in ln)
        assert default_cmds and "PYTHON=" not in default_cmds and "--python" not in default_cmds, default_cmds

    @pytest.mark.parametrize("ch", list(MA.PY_UNSAFE_CHARS))
    def test_interpreter_with_shell_special_chars_is_refused(self, home, ch):
        """解释器要进双引号提示、给人抄的命令与 make 配方的 "$(PYTHON)"：这些字符在那里会被解释或截断 ⇒ 生成前就拒。"""
        with pytest.raises(MA.BuildError, match="解释器路径"):
            MA.build_app(home / "Applications", repo=REPO, python=f"/opt/x{ch}y/python3")
        assert not (home / "Applications").exists()

    @pytest.mark.parametrize("where", ["alpha-hive-prod", "Desktop/Alpha Hive"])
    def test_every_regen_command_in_the_shell_comes_from_one_place(self, home, where):
        """壳里不再有 `make alphabot-app` 这种不带 `-C` 的写法（在随便哪个目录敲都找不到 Makefile），换解释器的写法
        带绝对路径占位（v0.45.461）。出现的每一处都是 `canonical_entry` / 生成器直跑，没有第三种。"""
        script = MA.launch_script(home / where, sys.executable)
        assert script.count("alphabot-app") == script.count(MA.canonical_entry()), script
        if where == "alpha-hive-prod":
            assert f"{MA.canonical_entry()} PYTHON=/" in script
            assert "--repo" not in script
        else:
            assert "-m alphabot.macos_app --repo . --python /" in script and "alphabot-app" not in script

    def test_missing_launcher_shows_the_repo_hint(self, tmp_path, home):
        """代码目录在、却缺 launcher.py：处理办法与「进不去」同一出处（458 写的是「在新位置重跑 make alphabot-app」）。"""
        clone = _fake_code_dir(home / "alpha-hive-prod")
        exe, alerts = _app_exe(tmp_path, repo=clone, python=sys.executable)
        (clone / "alphabot" / "launcher.py").unlink()
        r = subprocess.run([str(exe)], env={**os.environ, "HOME": str(home)})
        assert r.returncode == 1
        shown = alerts.read_text(encoding="utf-8")
        assert "没有 alphabot/launcher.py" in shown and "production_clone.py setup" in shown, shown

    def test_cd_failure_shows_the_hint(self, tmp_path, home):
        missing = home / "alpha-hive-prod"                    # 克隆被删：真跑一次壳，弹窗里得有重建办法
        _fake_code_dir(missing)
        exe, alerts = _app_exe(tmp_path, repo=missing, python=sys.executable)
        shutil.rmtree(missing)
        r = subprocess.run([str(exe)], env={**os.environ, "HOME": str(home)})
        assert r.returncode == 1
        shown = alerts.read_text(encoding="utf-8")
        assert "进不去代码目录" in shown and "production_clone.py setup" in shown, shown


class TestInterpreterCheck:
    """「写进 .app 的解释器现在能不能 exec」只有一条判据（v0.45.461）：生成时的 `interpreter_problem` 与壳里的
    `SHELL_PY_USABLE`。458 两边各写一份、都只看执行位——目录也算可执行，`PYTHON=/usr/local/bin` 两道都放行。"""

    @staticmethod
    def _cases(tmp_path):
        good = Path(_fake_python(tmp_path, "exit 0"))
        plain = tmp_path / "plain"
        plain.write_text("", encoding="utf-8")
        (tmp_path / "to-good").symlink_to(good)
        (tmp_path / "to-dir").symlink_to(tmp_path, target_is_directory=True)
        (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
        return {"exec-file": (good, True), "symlink-to-exec-file": (tmp_path / "to-good", True),
                "missing": (tmp_path / "nowhere", False), "dir": (tmp_path, False),
                "symlink-to-dir": (tmp_path / "to-dir", False), "not-executable": (plain, False),
                "dangling-symlink": (tmp_path / "dangling", False)}

    def test_python_and_shell_give_the_same_verdict(self, tmp_path):
        for name, (path, usable) in self._cases(tmp_path).items():
            py_ok = MA.interpreter_problem(str(path)) is None
            sh_ok = subprocess.run(["bash", "-c", f'PY="$1"; {MA.SHELL_PY_USABLE}', "_", str(path)]).returncode == 0
            assert (py_ok, sh_ok) == (usable, usable), name

    def test_directory_interpreter_alerts_on_double_click(self, tmp_path, home):
        exe, alerts = _app_exe(tmp_path, python=str(tmp_path))
        r = subprocess.run([str(exe)], env={**os.environ, "HOME": str(home)})
        assert r.returncode == 1
        shown = alerts.read_text(encoding="utf-8") if alerts.exists() else "（没弹）"
        assert "找不到可执行的" in shown and "是目录" in shown, shown

    def test_exec_failure_alerts_instead_of_dying_silently(self, tmp_path, home):
        """过了预检、exec 仍失败（格式 / 架构不对）：bash 缺省直接退出、错误只进日志——双击的人什么也看不到。"""
        junk = tmp_path / "junk-python"
        junk.write_bytes(b"\x00\x01\x02\x03not-a-binary\x00")
        junk.chmod(0o755)
        exe, alerts = _app_exe(tmp_path, repo=_fake_code_dir(tmp_path / "code"), python=str(junk))
        r = subprocess.run([str(exe)], env={**os.environ, "HOME": str(home)})
        assert r.returncode == 126
        shown = alerts.read_text(encoding="utf-8") if alerts.exists() else "（没弹）"
        assert f"执行 {junk} 失败" in shown and "launcher.log" in shown, shown


class TestShellDrift:
    """已装 .app 的启动壳过时 ⇒ 启动时不挡启动地提醒一次（v0.45.487）。壳在生成那一刻写死，v0.45.436~461 每次改壳都要
    人记得重新生成；`alphabot.__version__` 从 09-30 起没动过，版本号判不了 ⇒ 比内容：用壳里记下的输入在内存里重生成。"""

    @staticmethod
    def _install(home, repo=REPO):
        return MA.build_app(home / "Applications", repo=repo, python=sys.executable)

    @staticmethod
    def _edit(app, old="exit 126", new="exit 125"):
        exe = app / "Contents" / "MacOS" / MA.EXECUTABLE
        exe.write_text(exe.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")

    def test_freshly_generated_app_has_no_drift(self, home):
        app = self._install(home)
        d = MA.installed_drift(app)
        assert d["parts"] == [] and Path(d["repo"]) == REPO and d["python"] == sys.executable, d
        assert LA.check_app_shell() is None

    def test_each_part_is_detected(self, home):
        app = self._install(home)
        self._edit(app)
        assert MA.installed_drift(app)["parts"] == ["启动脚本"]
        with open(app / "Contents" / "Info.plist", "rb") as f:
            info = plistlib.load(f)
        info["CFBundleShortVersionString"] = "0.0.1"
        with open(app / "Contents" / "Info.plist", "wb") as f:
            plistlib.dump(info, f)
        (app / "Contents" / "Resources" / "AlphaBot.icns").write_bytes(b"icns-old")
        assert MA.installed_drift(app)["parts"] == ["启动脚本", "Info.plist", "图标"]

    def test_code_moving_on_is_what_gets_detected(self, home, monkeypatch):
        """真实场景是反过来的：装着的不动，代码往前走了（生成器改了壳）——同样要报。"""
        app = self._install(home)
        real = MA.launch_script
        monkeypatch.setattr(MA, "launch_script", lambda repo, python, *a, **k: real(repo, python, *a, **k) + "# 新版壳\n")
        assert MA.installed_drift(app)["parts"] == ["启动脚本"]

    @staticmethod
    def _launch(ui):
        """`run()` 里的两段：启动开头算、成功交出窗口 / 页面时弹（v0.45.498）。"""
        LA.announce_app_shell(ui, LA.check_app_shell())

    @staticmethod
    def _command_in(msg):
        hint = msg.split("要换上新的：", 1)[1].rsplit("，之后重新打开", 1)[0]
        return shlex.split(hint.split("执行 ", 1)[1])

    def test_drift_notifies_once_with_the_regen_command(self, home, capsys):
        app = self._install(home)
        self._edit(app)
        ui = FakeUI()
        self._launch(ui)
        assert ui.kinds() == ["notice"], ui.calls
        msg = ui.calls[0][1]
        assert "启动壳比代码旧" in msg and "启动脚本" in msg and MA.regen_hint(REPO, sys.executable) in msg, msg
        self._launch(ui)                                    # 同一处差异：只记日志，不再弹
        assert ui.kinds() == ["notice"]
        assert capsys.readouterr().out.count("启动壳过时") == 2, "每次启动都要记日志"

    @staticmethod
    def _custom_python(tmp_path):
        """一个非缺省、带空格的解释器路径（软链到真解释器）：照提醒做之后 `.app` 记的必须还是它。"""
        py = tmp_path / "my py" / "python3"
        py.parent.mkdir()
        py.symlink_to(sys.executable)
        return str(py)

    def test_following_the_notice_keeps_the_interpreter_direct_generator(self, home, tmp_path, monkeypatch):
        """照提醒里的命令**真跑一遍**（开发检出那种：生成器直跑）：新壳的 `PY=` 不变、差异消失（v0.45.498）。
        487 的提醒不带解释器：照做就把自定义解释器换回 /usr/local/bin/python3。"""
        custom = self._custom_python(tmp_path)
        app = MA.build_app(home / "Applications", repo=REPO, python=custom)
        self._edit(app)
        ui = FakeUI()
        self._launch(ui)
        cmd = self._command_in(ui.calls[0][1])
        assert cmd[:3] == [MA.DEFAULT_PYTHON, "-m", "alphabot.macos_app"], cmd
        monkeypatch.chdir(REPO)                             # 「在那份代码目录里执行」
        assert MA.main(cmd[3:]) == 0
        assert TestCanonicalMakeEntry._py_in_app(home) == custom
        assert MA.installed_drift(app)["parts"] == [] and LA.check_app_shell() is None

    def test_following_the_notice_keeps_the_interpreter_canonical_entry(self, home, tmp_path):
        """推荐入口那种（生产克隆）：提醒给的 `make -C … alphabot-app PYTHON=…` 真跑 make，新壳的 `PY=` 不变。"""
        custom = self._custom_python(tmp_path)
        r = TestCanonicalMakeEntry._make(home, f"PYTHON={custom}")
        assert r.returncode == 0, r.stderr
        app = home / "Applications" / f"{MA.APP_NAME}.app"
        self._edit(app)
        ui = FakeUI()
        self._launch(ui)
        cmd = self._command_in(ui.calls[0][1])
        assert cmd[:4] == MA.canonical_entry().split() and cmd[4:] == [f"PYTHON={custom}"], cmd
        r = TestCanonicalMakeEntry._make(home, *cmd[4:])
        assert r.returncode == 0, r.stderr
        assert TestCanonicalMakeEntry._py_in_app(home) == custom
        assert MA.installed_drift(app)["parts"] == [] and LA.check_app_shell() is None

    def test_a_new_difference_notifies_again(self, home):
        app = self._install(home)
        self._edit(app)
        ui = FakeUI()
        self._launch(ui)
        self._edit(app, "exit 125", "exit 124")             # 又变了一处 ⇒ 指纹变了
        self._launch(ui)
        assert ui.kinds() == ["notice", "notice"]

    def test_code_moving_on_again_notifies_again(self, home, monkeypatch):
        """装着的不动、代码又往前走了一步 ⇒ 是新的差异，要再提醒（指纹得同时摘要「应生成的」那一端）。"""
        self._install(home)
        real = MA.launch_script
        ui = FakeUI()
        for tag in ("v2", "v3"):
            monkeypatch.setattr(MA, "launch_script",
                                lambda repo, python, *a, _t=tag, **k: real(repo, python, *a, **k) + f"# {_t}\n")
            self._launch(ui)
        assert ui.kinds() == ["notice", "notice"]

    def test_app_for_another_code_dir_is_not_judged_here(self, home, tmp_path, capsys):
        self._install(home, repo=_fake_code_dir(tmp_path / "other"))
        ui = FakeUI()
        self._launch(ui)
        assert ui.calls == [] and "指向别的代码目录" in capsys.readouterr().out

    def test_no_app_installed_is_silent(self, home, capsys):
        ui = FakeUI()
        self._launch(ui)
        assert ui.calls == [] and capsys.readouterr().out == ""

    def test_check_failure_is_logged_never_blocks(self, home, capsys):
        foreign = home / "Applications" / f"{MA.APP_NAME}.app" / "Contents"
        foreign.mkdir(parents=True)
        with open(foreign / "Info.plist", "wb") as f:
            plistlib.dump({"CFBundleIdentifier": "com.someone.else"}, f)
        ui = FakeUI()
        self._launch(ui)
        assert ui.calls == [] and "启动壳核对失败" in capsys.readouterr().out

    _DRIFT = {"message": "启动壳比代码旧（测试）", "fingerprint": "f", "parts": ["启动脚本"]}

    @staticmethod
    def _serve(monkeypatch, state="free"):
        """不真起服务：`probe` 给定状态，`spawn_server` / `wait_ready` 打桩——这组只看提醒落在流程的哪一步。
        `from_app: False` ⇒ 关窗不去停服务；调用方另给空闲端口：两道都为了碰不到本机真在跑的 Alpha Bot。"""
        monkeypatch.setattr(LA, "probe", lambda *a, **k: {
            "state": state, "detail": "别人的程序", "info": {"version": "t", "demo": True, "from_app": False}})
        monkeypatch.setattr(LA, "spawn_server", lambda *a, **k: (type("P", (), {"pid": 1})(), PurePath("server.log")))
        monkeypatch.setattr(LA, "wait_ready", lambda *a, **k: None)

    @pytest.mark.parametrize("path, ui_kw, state, expected", [
        ("窗口已开 ⇒ 调到前面", {"window": True}, "free", ["activate", "notice"]),
        ("服务已在跑 ⇒ 开页面", {}, "alphabot", ["notice", "open"]),
        ("首次启动 ⇒ 先选（演示）再起服务", {"answers": [LA.DEMO]}, "free", ["ask", "notice", "open"]),
        ("服务已在跑 ⇒ 开原生窗口", {"window": True}, "alphabot", ["notice", "window"]),
    ])
    def test_notice_comes_when_the_launch_is_handed_over(self, home, monkeypatch, path, ui_kw, state, expected):
        """弹在**启动已成功、交出窗口 / 页面**那一刻（v0.45.498）：选数据根对话框在它之前结束，不叠在一起。
        487 在启动一开始就弹 ⇒ 这里的顺序会是 notice 打头。原生窗口在本进程里阻塞到关窗 ⇒ 必须在交出之前弹。"""
        monkeypatch.setattr(LA, "check_app_shell", lambda: dict(self._DRIFT))
        monkeypatch.setattr(LA, "window_owner", lambda: 4242 if path.startswith("窗口已开") else None)
        self._serve(monkeypatch, state)
        ui = FakeUI(**ui_kw)
        assert LA.main(["--port", str(_free_port())], ui=ui) == 0, path
        assert ui.kinds() == expected, (path, ui.calls)

    @pytest.mark.parametrize("why, ui_kw, state", [
        ("端口被别人占", {}, "other"),
        ("首次启动时取消", {"answers": [LA.CANCEL]}, "free"),
    ])
    def test_no_notice_when_the_launch_does_not_happen(self, home, monkeypatch, why, ui_kw, state):
        """启动没成（失败弹窗 / 用户取消）⇒ 不弹、也不记「已提醒」：下次成功启动再提（487 会先弹它、再弹失败）。"""
        monkeypatch.setattr(LA, "check_app_shell", lambda: dict(self._DRIFT))
        self._serve(monkeypatch, state)
        ui = FakeUI(**ui_kw)
        LA.main(["--port", str(_free_port())], ui=ui)
        assert "notice" not in ui.kinds(), (why, ui.calls)
        assert not LA._shell_drift_seen_path().exists(), why

    @pytest.mark.parametrize("step", ["installed_drift", "regen_hint", "notice", "record"])
    def test_any_failure_in_the_reminder_never_blocks_the_launch(self, home, monkeypatch, capsys, step):
        """提醒是附加功能：四步里哪一步抛，启动都照常完成、只记日志（487 只包住了 installed_drift，其余三步一抛就是
        「启动器出错」弹窗、页面打不开）。没弹成 / 没记下 ⇒ 不算已提醒，下次再提。"""
        self._edit(self._install(home))
        boom = RuntimeError(f"测试：{step} 坏了")

        def _raise(*a, **k):
            raise boom
        if step in ("installed_drift", "regen_hint"):
            monkeypatch.setattr(MA, step, _raise)
        elif step == "record":
            import hive_logger
            monkeypatch.setattr(hive_logger, "atomic_json_write", _raise)

        class _UI(FakeUI):
            def notice(self, message):
                super().notice(message)
                if step == "notice":
                    raise boom
        self._serve(monkeypatch, "alphabot")
        ui = _UI()
        assert LA.main(["--port", str(_free_port())], ui=ui) == 0, step
        assert "open" in ui.kinds() and "alert" not in ui.kinds(), (step, ui.calls)
        assert "不影响启动" in capsys.readouterr().out, step
        assert not LA._shell_drift_seen_path().exists(), step

    def test_mac_notice_does_not_wait_and_shows_after_the_window(self, monkeypatch):
        """真 MacUI：起独立的 osascript 就走（不等用户点）；先 delay 再 activate，免得被随后开的窗口压住。"""
        started = []

        class _P:
            def __init__(self, cmd, **kw):
                started.append((cmd, kw))

            def wait(self, *a, **k):
                raise AssertionError("提醒不许等用户点")
        monkeypatch.setattr(LA.subprocess, "Popen", _P)
        LA.MacUI().notice("消息")
        (cmd, kw), = started
        script = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-e"]
        assert script.index(f"delay {LA.NOTICE_DELAY_SEC}") < script.index("activate"), script
        assert cmd[-1] == "消息" and kw.get("start_new_session") is True


class TestRegenHints:
    """「怎么重新生成 .app」给人的命令只从 `regen_hint` / `canonical_entry` 出（v0.45.461）。"""

    def test_canonical_entry_targets_the_production_clone(self, home):
        import production_clone
        cmd = MA.canonical_entry().split()
        assert cmd[:2] == ["make", "-C"] and cmd[3] == "alphabot-app", cmd
        assert Path(os.path.expanduser(cmd[2])) == production_clone.default_dest()

    def test_help_page_shows_the_canonical_entry(self, home):
        """帮助页是静态 JS，取不到 Python 常量：在这里核对它抄的就是推荐入口（改入口而忘了帮助页 ⇒ 红）。"""
        help_js = (REPO / "alphabot" / "static" / "pages" / "help.js").read_text(encoding="utf-8")
        assert MA.canonical_entry() in help_js
        assert "PYTHON=/绝对路径" in help_js

    def test_other_checkout_gets_the_direct_generator_command(self, tmp_path, home):
        hint = MA.regen_hint(tmp_path / "dev", python="/x/python3")
        assert hint.endswith("-m alphabot.macos_app --repo . --python /x/python3") and "make" not in hint, hint


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
