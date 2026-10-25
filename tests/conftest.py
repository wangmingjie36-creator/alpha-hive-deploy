"""
Alpha Hive 测试 fixtures - 共享 mock 数据 + 隔离数据库
"""

import datetime
import time
import sys
import os
import pathlib
import site
import threading
import weakref
import pytest

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ==================== 环境隔离 ====================

# ==================== cwd / sys.path 隔离 ====================

@pytest.fixture(autouse=True, scope="session")
def _empty_cwd_between_tests(request, tmp_path_factory):
    """测试之间（含 module / class / session 级 fixture 的 setup 与 teardown）cwd 停在一个会话级空目录（v0.45.237）。

    v0.45.224 在每条测试结束后把 cwd 还原到**调用目录** —— 于是高于函数级的 fixture 都在调用目录
    （平常就是仓库根）里 setup，里面的 cwd 相对读取照绿。实测：module 级 fixture 读 `Path("config.py")`
    ⇒ `exists()=True`、cwd=调用目录。会话结束才回调用目录。
    """
    d = tmp_path_factory.mktemp("cwd_between_tests")
    request.config._alpha_hive_between_tests_cwd = str(d)
    os.chdir(d)
    yield d
    os.chdir(request.config.invocation_params.dir)


@pytest.fixture(autouse=True)
def _isolate_cwd_and_sys_path(_empty_cwd_between_tests, tmp_path, monkeypatch):
    """每条测试在**自己的空目录**里跑；结束后 cwd 回会话级空目录、`sys.path` 还原（v0.45.224 起）。

    **为什么是空目录，不只是「结束时还原」**：cwd 相对的读取有一半写法静态扫描看不见 ——
    参数化变量 `Path(path)`、循环变量 `Path(name)`、`subprocess.run([…, "x.py"])` 不给 cwd。
    v0.45.224 从空目录跑全套（先修好收集期 chdir 之后）实测出 9 条，
    还有生产代码 `CBOEDailyFetcher()` 的相对默认值 `cache/cboe_daily` 在 cwd 里建目录
    （它不读 `ALPHA_HIVE_CACHE_DIR`；在主 checkout 起 pytest 就建进主 checkout）。

    **为什么还原 `sys.path`**：`weekly_optimizer.py` 在函数体里
    `sys.path.insert(0, str(ALPHAHIVE_DIR))`，ALPHAHIVE_DIR 写死 `~/Desktop/Alpha Hive`
    （worktree 里就是主 checkout），从不拿掉（全套插了 40 次）。此后任何函数体内 `import X`、
    只要 X 还没 import 过，拿的就是主 checkout 的 X：141 个顶层模块里 120 个会这样解析。
    实测把 worktree 的 `economic_calendar_watch._release_date_to_quarter` 改坏：单跑它的测试
    4 failed，先跑一条 weekly_optimizer 测试再跑 ⇒ **6 passed**。根治在生产代码。

    **为什么 chdir 走 monkeypatch**：autouse fixture 的 setup 顺序是**按名字字母序**，不是定义顺序
    （`--setup-plan` 实测：`_block_llm_api` 排第一、先实例化 monkeypatch ⇒ monkeypatch 最后 teardown）。
    v0.45.224 在 teardown 里手工 `os.chdir(调用目录)`，测试若自己 `monkeypatch.chdir`，撤销排在后面，
    cwd 停在该测试的 `_cwd`（实测；v0.45.224 docstring 说顺序「不定」—— 错，是确定地输）。
    `monkeypatch.chdir` 撤销时回到**本测试第一次经它 chdir 之前**的目录（即会话级空目录），
    与谁先 teardown 无关；测试里裸 `os.chdir` 的泄漏也一并被它撤销。
    """
    path = list(sys.path)
    run_dir = tmp_path / "_cwd"
    run_dir.mkdir()
    monkeypatch.chdir(run_dir)
    yield
    sys.path[:] = path


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """所有测试自动使用临时目录，防止污染生产数据库"""
    monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path))
    monkeypatch.setenv("ALPHA_HIVE_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ALPHA_HIVE_CHROMA_PATH", str(tmp_path / "test_chroma"))
    monkeypatch.setenv("ALPHA_HIVE_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("ALPHA_HIVE_CACHE_DIR", str(tmp_path / "cache"))
    # v0.41.3 第二层防线：显式禁用期权快照写盘。曾发生 pytest 的 mock 期权链
    # 经 OptionsAgent.analyze() 写进生产 cache/options_snapshot_NVDA_*.json
    # （data_quality 标 real），被当日正式扫描按"快照命中"复用进日报
    monkeypatch.setenv("OPTIONS_SNAPSHOT_DISABLE", "1")
    # v0.45.145 同款第二层防线：禁止测试把模型写进 ml_model_history/。
    # 当年 `HGBModel.save_model` 的默认文件名是**相对路径**，tests/ 里约 12 处
    # `svc.train_model()` 不传 tmp 路径 ⇒ 在主 checkout 跑 pytest 会往 cwd 写
    # ml_model.json，本模块会顺手快照它（v0.45.570 起 save_model 没有缺省路径、
    # train_model 也不再存盘，但显式传路径的测试照样会触发快照）。而 ml_model_history/ 当时是 git 跟踪 +
    # 在自动提交白名单里的 ⇒ 夹具模型会被当成生产模型提交推送。
    # ml_model_guard 自己也在 pytest 下默认关闭（两层），这里再显式关一次。
    monkeypatch.setenv("ALPHA_HIVE_MODEL_SNAPSHOT_DISABLE", "1")


@pytest.fixture(autouse=True, scope="session")
def _pin_child_user_site():
    """测试换 HOME 不挪子进程的用户 site-packages：全会话钉一次 `PYTHONUSERBASE`（v0.45.426）。

    子进程里的真 Python 启动时按 `$HOME` 推用户 site；本机 numpy / pytest / jinja2 / starlette / httpx /
    anyio **只**装在用户 site。测试为隔离按 HOME 求的默认位置（`~/Library`、`~/.claude/scripts`、
    `~/.alpha_hive_*`）把 HOME 指到 tmp——各文件各自在换（v0.45.426 时 13 个，且还在涨）⇒ 起的真解释器丢掉这半边依赖、
    import 即死，红的是夹具不是被测代码；且只在 Mac 红，CI（依赖在系统 site）永远绿。
    v0.45.397 在 alphabot 的 `home` 夹具里钉过一次；「换 HOME 时记得顺手钉」写在注释里不会被执行，
    下一个换 HOME 的文件照样会忘 ⇒ 收到这里，唯一出处。

    取值在会话开始时（任何测试 / 夹具换 HOME 之前），且 `site.getuserbase()` 返回本进程缓存的值、
    不随当前 HOME 变 ⇒ 与谁先换 HOME 无关。不换 HOME 的测试拿到的是同一个值，等于没钉。
    不掩盖生产问题：生产代码从不给子进程改 HOME（只改 `ALPHA_HIVE_HOME`），launchd / .app 拿的是真 HOME。

    管不到：从零构造 env、不继承 `os.environ` 的子进程——那类测试要的就是「只有这几个变量」。
    v0.45.426 时带沙箱 HOME 的只有 `test_scan_catchup`（照 launchd 现造环境），那里的 python 是不执行的桩。
    守卫：`tests/test_child_user_site_pin.py`（删掉本夹具在任何机器上都红，含 CI）。
    """
    prev = os.environ.get("PYTHONUSERBASE")
    os.environ["PYTHONUSERBASE"] = site.getuserbase()
    yield
    if prev is None:
        os.environ.pop("PYTHONUSERBASE", None)
    else:
        os.environ["PYTHONUSERBASE"] = prev


# ==================== 禁止测试调用真实 Anthropic API ====================

@pytest.fixture(autouse=True)
def _block_llm_api(monkeypatch):
    """禁止所有测试调用真实 Anthropic API，避免消耗余额。

    llm_service 内置 _disabled 标志：设为 True 后 is_available() 返回 False，
    所有 Agent 自动降级到规则引擎模式。monkeypatch 在每个测试结束后自动恢复。
    """
    import llm_service
    monkeypatch.setattr(llm_service, "_disabled", True)
    # 同时清除已缓存的 client，防止之前初始化的 client 被复用
    monkeypatch.setattr(llm_service, "_client", None)


# ==================== 禁止测试调用真实宏观数据源 ====================

@pytest.fixture(autouse=True)
def _block_same_day_macro(monkeypatch):
    """默认关掉 v0.45.60 的当日宏观取数层（财政部 + Finnhub）。

    为什么必须有这道闸：conftest 一直只 mock 了 yfinance（见下方
    `mock_yfinance`），而 v0.45.60 新增的取数层**绕过 yfinance 直接打外网**。
    接上当天就有两个后果：

      · `test_yield_curve_inverted` 红了 —— 测试构造的倒挂曲线被真实的
        2026-08-27 数据（10Y 4.67 / 2Y 4.20，normal）盖掉
      · 整个套件开始打真网络：变慢、变脆、且在离线环境下不可用

    加数据源时必须同时确认「测试里它被关掉了吗」—— 与 `http_gate` docstring
    记过的那条教训同形（「加源之前先确认闸门覆盖它」）。

    需要验证这一层的测试自己 monkeypatch `_same_day_macro_data`，
    那会覆盖本 fixture。
    """
    try:
        import fred_macro
    except Exception:  # pragma: no cover - 模块不可得时无需拦
        pass
    else:
        monkeypatch.setattr(fred_macro, "_same_day_macro_data",
                            lambda as_of=None: ({}, {}))

    # v0.45.61：Twelve Data 同理。配上 key 后 `calculate_iv_rv_spread` 会先走它，
    # 于是 `test_iv_rv_reports_unavailable_not_zero`（构造"取不到数"场景）被
    # 真实数据盖掉 —— **同一个错误的第三次**：
    #   ① v0.45.56 加 yf_gate 时                → 已由 mock_yfinance 覆盖
    #   ② v0.45.60 加财政部/Finnhub 宏观层时    → 漏了，本 fixture 上半段补的
    #   ③ v0.45.61 加 Twelve Data 时            → 又漏了，这段补的
    # 教训固定下来：**新增任何外部数据源，同一个 commit 里必须在这里加一行。**
    # 需要验证它的测试自己 monkeypatch，会覆盖本 fixture。
    try:
        import twelve_data
    except Exception:  # pragma: no cover
        return
    monkeypatch.setattr(twelve_data, "api_key", lambda: "")


@pytest.fixture(autouse=True)
def _no_leaked_macro_snapshot():
    """测试结束时 `fred_macro` 的宏观快照必须已卸载；没卸 ⇒ 卸掉并让**留下它的那条**报错（v0.45.366）。

    为什么要有：快照是进程级全局，v0.45.366 起 GuardBee 拿 `get_macro_snapshot()` 判「这次是补跑」
    （期限结构读快照、FOMC 按目标日数）。一条测试忘了卸，之后同进程里每条调 Guard 的测试都**静默**
    跑在补跑口径下——绿不绿取决于执行顺序。实测 `test_vix_same_day` 的 `_offline` 只在 setup 卸、
    teardown 不卸，靠下一条的 setup 兜着才没出事。

    只看已导入的模块（没导入过就不可能装过），不为此 import fred_macro。
    """
    yield
    fm = sys.modules.get("fred_macro")
    snap = fm.get_macro_snapshot() if fm is not None else None
    if snap is not None:
        fm.set_macro_snapshot(None)
        pytest.fail(f"测试结束时宏观快照仍装着（{snap.get('date')}）：请在 teardown 里 "
                    "`fred_macro.set_macro_snapshot(None)`——它会让之后的 GuardBee 静默跑在补跑口径下",
                    pytrace=False)


# ==================== 全局离线闸（传输层）====================

#: 允许**伸手取数**的测试模块白名单。**v0.45.136 起为空，并且应当保持为空。**
#:
#: v0.45.133 建表时有 10 个模块欠债；v0.45.136 逐个补了显式源桩
#: （各模块自己的 `_offline_sources` fixture + 本文件的可复用桩），债已还清。
#:
#: ⚠️ **只能缩短，不能加长。** 新测试伸手取网时，正确做法是给它补一个源桩，
#: 或者——如果它的意图**就是**打真外网——给它加 `@pytest.mark.network`。
#: 往这里加模块等于把「测试不确定」变成「测试不确定但没人管」。
#: 空表也是有意义的：它让 `_offline_transport` 的 teardown 对**每一个**模块生效。
_KNOWN_NETWORK_REACHERS: set = set()

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "", None}


def _host_of(url) -> str:
    """从 str / urllib Request 里取主机名；取不到当作外网（宁可误挡不可漏放）。"""
    from urllib.parse import urlparse
    raw = getattr(url, "full_url", None) or getattr(url, "url", None) or url
    try:
        return (urlparse(str(raw)).hostname or "").lower()
    except Exception:  # pragma: no cover
        return "<unparseable>"


class _OfflineInTests(OSError):
    """故意继承 OSError —— 真离线抛的 gaierror / ConnectionRefusedError 都是它的
    子类，生产代码的 `except OSError` / `except requests...ConnectionError`
    因此会走**和真离线一模一样**的降级分支。若换成 RuntimeError，接得住真离线的
    代码会接不住它，测出来的就不是「离线行为」而是「探针行为」——实测差别很大：
    同一批测试 RuntimeError 下 9 红、OSError 下 1 红。"""


@pytest.fixture(autouse=True)
def _offline_transport(request, monkeypatch):
    """整套测试默认离线。**挡在传输层，不逐个源打桩**（v0.45.133）。

    背景：双层探针实测，CI 选择集里有 12 个文件在偷偷出网（修完 Slack 后剩 10 个、
    110 次）。它们分属**六个**源——`cboe_options._fetch_cboe_payload`、
    yfinance（经 `yf_gate`）、`cboe_vix._download`、`reddit_sentiment._fetch_ranking`、
    `http_gate.urlopen_gated`（AlphaVantage / Finnhub）、
    `vix_term_structure._get_vx_futures`——但只用了**三种传输**：

        urllib.request.urlopen          ← CBOE payload / CBOE VIX / http_gate / vixcentral
        requests.Session.request        ← reddit_sentiment（及当年的 Slack 发送，v0.45.575 已删）
        curl_cffi.Session.request       ← yfinance 1.2（libcurl 在 C 层开 socket）

    本文件已有的四条「关掉某个源」的 fixture（`_block_llm_api` /
    `_block_same_day_macro` 的财政部+Finnhub / Twelve Data / `_block_slack`（v0.45.575 已删））
    都是源级的，而它们的 docstring 里那句**「新增任何外部数据源，同一个 commit
    里必须在这里加一行」已经失败四次**（v0.45.56 / .60 / .61，以及这次的六个源）。
    传输层只有三个入口且几乎不变，新增数据源自动被罩住——这是那条政策本身的修法。

    ⚠️ 主机名白名单只放行 localhost：**不能按 socket 地址判断**，因为本机出网
    走 127.0.0.1 的代理，按地址放行等于全放。所以闸设在**库级 API**（拿得到真实
    URL），而不是 `socket.connect`。

    ⚠️ 带 `network` / `integration` 标记的测试不受本闸约束——它们的意图就是打真
    外网（实测 `-m network` 那 24 个测试确实出网 168 次，marker 是准的）。

    观测点：任何被挡下的请求都会记账，teardown 比对 `_KNOWN_NETWORK_REACHERS`；
    表外模块一伸手立刻判红。没有它，将来新增的取数支路会被静默挡下、
    悄悄走降级分支——那正是 CLAUDE.md「这个失败，下游怎么知道？」要治的形状。
    """
    if (request.node.get_closest_marker("network")
            or request.node.get_closest_marker("integration")):
        # ⚠️ 必须 yield 后再 return —— 本 fixture 是生成器 fixture，
        # 豁免分支里直接 return 会让 pytest 报 `did not yield a value`，
        # 把**恰好是 CI 用 -m "not network" 摘掉的那 24 个测试**全打死（CI 照样全绿）。
        yield
        return

    blocked = []

    def _deny(kind, url):
        host = _host_of(url)
        if host in _LOCAL_HOSTS:
            return None                      # 本机服务照常
        blocked.append(f"{kind} {host}")
        raise _OfflineInTests(
            f"测试默认离线，已挡下 {kind} → {host}"
            "（tests/conftest.py::_offline_transport）")

    import urllib.request
    _real_urlopen = urllib.request.urlopen

    def _gated_urlopen(url, *a, **k):
        _deny("urlopen", url)
        return _real_urlopen(url, *a, **k)

    _gated_urlopen._offline_gated = True
    monkeypatch.setattr(urllib.request, "urlopen", _gated_urlopen)

    try:
        import requests.sessions as _rs
    except ImportError:  # pragma: no cover
        pass
    else:
        _real_req = _rs.Session.request

        def _gated_request(self, method, url, *a, **k):
            _deny(f"requests.{method}", url)
            return _real_req(self, method, url, *a, **k)

        _gated_request._offline_gated = True
        monkeypatch.setattr(_rs.Session, "request", _gated_request)

    try:
        from curl_cffi import requests as _curl
    except ImportError:  # pragma: no cover
        pass
    else:
        _real_curl = _curl.Session.request

        def _gated_curl(self, method, url, *a, **k):
            _deny(f"curl.{method}", url)
            return _real_curl(self, method, url, *a, **k)

        _gated_curl._offline_gated = True
        monkeypatch.setattr(_curl.Session, "request", _gated_curl)

    request.node._offline_blocked = blocked

    yield

    module = os.path.basename(str(request.node.fspath))
    if getattr(request.node, "_offline_expect_blocks", False):
        return                               # 闸自身的测试，见 offline_gate_blocked
    if blocked and module not in _KNOWN_NETWORK_REACHERS:
        raise AssertionError(
            f"{module} 伸手取外网了：{sorted(set(blocked))}。\n"
            "整套测试默认离线（tests/conftest.py::_offline_transport）。"
            "请给这条测试补一个显式的源桩（参考 tests/test_quote_set.py::_offline），"
            "而不是把模块加进 _KNOWN_NETWORK_REACHERS —— 那张表只能缩短。\n"
            "若这条测试的意图**就是**打真外网，给它加 @pytest.mark.network。")


@pytest.fixture(autouse=True)
def _block_git_transport_probe(request, monkeypatch):
    """`git_transport_probe`（v0.45.351）用**裸 socket**（getaddrinfo + TCP connect）探
    github.com:22 / ssh.github.com:443——上面 `_offline_transport` 挡在 urllib / requests /
    curl_cffi 库级 API 上，**挡不住它**。而探测在每次 gh-pages 推送失败时都会跑，
    本仓好几条测试（竞态重试、离线序列）会走到那条路径 ⇒ 不钉死就会真去连 GitHub、
    每次最多卡 20 秒。

    钉成抛 `_OfflineInTests`（OSError 子类，与真断网同一条降级路径），并记账：
    需要测探测本身的用例在测试体里 `monkeypatch.setattr` 覆盖这两个函数即可
    （函数级 monkeypatch 晚于本 autouse fixture 生效）。`network` 标记的用例不钉。
    """
    if request.node.get_closest_marker("network"):
        yield
        return
    import git_transport_probe as _gtp
    calls = []

    def _deny_resolve(host, port):
        calls.append(("resolve", host, port))
        raise _OfflineInTests(f"测试默认离线：git_transport_probe 解析 {host}（tests/conftest.py）")

    def _deny_connect(family, sockaddr, timeout):
        calls.append(("connect", sockaddr))
        raise _OfflineInTests(f"测试默认离线：git_transport_probe 连接 {sockaddr}（tests/conftest.py）")

    monkeypatch.setattr(_gtp, "_resolve", _deny_resolve)
    monkeypatch.setattr(_gtp, "_tcp_connect", _deny_connect)
    request.node._git_probe_calls = calls
    yield


#: 当前测试的快照出网闸状态：`calls` 记被拦的调用（供闸自身的测试核对），`allow_real` 只给 network 标记的用例
_SNAPSHOT_FETCH_GATE = {"calls": [], "allow_real": False}


@pytest.fixture(scope="session", autouse=True)
def _cloud_snapshot_fetch_gate():
    """`cloud_snapshot_loader._git_fetch`（v0.45.466）在 **git 子进程**里出网——上面两道（库级 API / 裸 socket）
    都挡不住。补跑入口 `_snapshot_ctx`、每日观测 `lag_summary`、入场价回填都会走到它，而缺省仓库就是本检出
    （origin = GitHub）。

    **session 级装、整个会话不撤**（v0.45.466 二次检查）：每日观测跑在后台线程里，线程可能活过起它的那条测试——
    函数级 monkeypatch 在 teardown 撤掉闸之后它再拉，就真出网了（test_production_sync 的 main 用例实测会这样）。
    只放行 origin 是**本地路径**的仓库（测试自己建的裸仓库：fetch 不出网，测试要真跑它）；其余抛 `_OfflineInTests`
    （与真断网同一条降级路径）并记账。`network` 标记的用例经 `_SNAPSHOT_FETCH_GATE["allow_real"]` 放行。
    """
    import subprocess
    import cloud_snapshot_loader as _csl
    real = _csl._git_fetch

    def _gated(repo, timeout):
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo,
                             capture_output=True, text=True).stdout.strip()
        if _SNAPSHOT_FETCH_GATE["allow_real"] or (url and (os.path.isabs(url) or url.startswith("file://"))):
            return real(repo, timeout)
        _SNAPSHOT_FETCH_GATE["calls"].append((repo, url))
        raise _OfflineInTests(f"测试默认离线：cloud_snapshot_loader 要从 {url or '（无 origin）'} "
                              "拉 cloud-snapshots（tests/conftest.py）")

    _csl._git_fetch = _gated
    yield
    _csl._git_fetch = real


@pytest.fixture(autouse=True)
def _cloud_snapshot_per_test_state(request, monkeypatch):
    """每条测试清空本进程的刷新 / 告警记录（免得上一条刷新过，下一条的「本地陈旧还是云端没有」跟着变），
    并在收尾时**等本条测试起的每日观测线程做完**：活过测试的线程会把刷新记录写进下一条测试的表里。"""
    import cloud_snapshot_loader as _csl
    monkeypatch.setattr(_csl, "_REFRESHED", {})
    monkeypatch.setattr(_csl, "_WARNED", set())
    calls: list = []
    monkeypatch.setitem(_SNAPSHOT_FETCH_GATE, "calls", calls)
    monkeypatch.setitem(_SNAPSHOT_FETCH_GATE, "allow_real", bool(request.node.get_closest_marker("network")))
    request.node._snapshot_fetch_calls = calls
    yield
    adr = sys.modules.get("alpha_hive_daily_report")
    if adr is None:
        return                      # 没导入过日报 ⇒ 不可能起过观测线程
    name = adr._CloudSnapshotObservation.THREAD_NAME
    for t in threading.enumerate():
        if t.name == name:
            t.join(15)
            assert not t.is_alive(), f"每日观测线程 {name} 活过了起它的测试（15 秒没做完）——它会污染后面的测试"


# ==================== 可复用的显式源桩（各模块 opt-in）====================
#
# `_offline_transport` 是**兜底**：它让取数失败、行为与真离线一致，但生产代码
# 仍会一路走到传输层才被挡下。下面这些是**显式源桩**：在源头就返回该源自己
# 文档承诺的「取不到」形态，测试因此**由构造保证确定**，而不是靠一张网兜住。
#
# 用法——在测试模块里声明需要哪几个：
#
#     @pytest.fixture(autouse=True)
#     def _offline_sources(stub_cboe_payload, stub_yfinance):
#         """本文件会走到 CBOE 全链与 yfinance 降级，显式钉死。"""
#
# ⚠️ 桩的返回值**必须是该源自己的「取不到」契约**，不能随手挑一个看着合理的值
#    —— 挑出来的默认值会让下游误以为掌握了信息（[[alpha-hive-silent-degradation]]
#    v0.45.3 那条判据）。每个桩下面都注了契约出处。
# ⚠️ 需要真行为的测试在自己函数体里再 setattr 一次即可覆盖（fixture 先跑，后设的赢）。


@pytest.fixture
def stub_cboe_payload(monkeypatch):
    """`cboe_options._fetch_cboe_payload` → None。

    契约：模块 docstring「失败一律返回 None（调用方据此再降级到样本数据）」。
    这也正是 `test_quote_set.py` / `TestUnavailableShapeIsUniform` 早就手写的那个桩。
    """
    import cboe_options
    monkeypatch.setattr(cboe_options, "_fetch_cboe_payload", lambda *a, **k: None)


@pytest.fixture
def stub_cboe_vix(monkeypatch):
    """`cboe_vix._download` → None。

    契约：函数 docstring「失败返回 None（不抛，让调用方走缓存）」。
    ⚠️ 走缓存是刻意保留的既有行为：`cboe_vix` 的缓存是
    `Path(__file__).parent / "cache"`（仓库本地目录，**不受 ALPHA_HIVE_CACHE_DIR
    隔离**），本机热、CI 冷。桩只负责不出网，不改缓存语义。

    v0.45.357：同时钉 `cboe_vix._download_quote` → None（延迟报价，契约同上）。
    ⚠️ 这一条**只在美东 16:15 之后的交易日**才会被走到（之前 `get_vix_session_close`
    直接回 `before_close`、不出网）—— 不钉它，同一批测试下午跑红、上午跑绿。
    """
    import cboe_vix
    monkeypatch.setattr(cboe_vix, "_download", lambda: None)
    monkeypatch.setattr(cboe_vix, "_download_quote", lambda: None)


@pytest.fixture
def stub_vixcentral(monkeypatch):
    """`vix_term_structure._get_vx_futures` → []。契约：函数失败分支 `return []`。"""
    import vix_term_structure
    monkeypatch.setattr(vix_term_structure, "_get_vx_futures", lambda: [])


@pytest.fixture
def stub_reddit(monkeypatch):
    """`RedditSentimentClient._fetch_ranking` → []。

    契约：函数内 `if requests is None: return []`（拿不到就是空排名）。
    """
    import reddit_sentiment
    monkeypatch.setattr(reddit_sentiment.RedditSentimentClient, "_fetch_ranking",
                        lambda self, filter_name="all-stocks": [])


@pytest.fixture
def stub_http_gate(monkeypatch):
    """`http_gate.urlopen_gated` → 抛 OSError（AlphaVantage / Finnhub 走它）。

    契约：它是 `urllib.request.urlopen` 的串行化版本，失败就是抛——
    `data_pipeline` 的两个 Source 都用 try/except 接住并降级。
    ⚠️ 必须 patch `http_gate` 模块上的名字：两个调用方都是**函数内局部 import**
    （`from http_gate import urlopen_gated`），每次调用重新取模块属性，所以打得中；
    若改成模块顶层 `from ... import`，这个桩就会失效（v0.45.72 那条教训）。
    """
    import http_gate
    monkeypatch.setattr(http_gate, "urlopen_gated",
                        _raise_offline("http_gate.urlopen_gated"))


@pytest.fixture
def stub_fred(monkeypatch):
    """`fred_macro._load_fred_key` → `""`（于是 FRED 整段被跳过）。

    契约：`_fetch_macro_data` 里写的就是 `if fred_key:` —— **没有 key 就不取
    FRED**，`fred_data` 保持 `{}`。所以这里钉的是"拿不到 key"，不是伪造一份
    观测；下游看到的形态与真实无 key 环境完全一致。

    ⚠️ 钉 key 而不是钉 `_fetch_fred_series`，因为 `_load_fred_key` 读的是
    `~/.alpha_hive_fred_key`（**真实 home**，不受 `ALPHA_HIVE_HOME` 隔离）与
    `FRED_API_KEY` 环境变量。于是同一条测试在有 key 的开发机上打 FRED、
    在 CI 上不打 —— 两台机器跑的是两条分支。钉死它顺带把这个环境依赖也去掉了。
    """
    import fred_macro
    monkeypatch.setattr(fred_macro, "_load_fred_key", lambda: "")


def _raise_offline(what):
    def _f(*_a, **_k):
        raise _OfflineInTests(f"{what} 在测试里被显式钉死（tests/conftest.py 源桩）")
    return _f


class _OfflineTicker:
    """`yfinance.Ticker` 的离线替身。

    ⚠️ **必须是 class，不能是函数**：`yf_gate.install()` 用
    `type("Ticker", (yfinance.Ticker,), ...)` 继承它，还会
    `isinstance(getattr(base, name), property)` 判断哪些要按 property 重建。
    换成函数会让 `install()` 直接 TypeError —— 而 `ensure()` 散布在
    options_analyzer / fred_macro / dashboard_renderer / market_intelligence
    四个热点函数入口，测试里随时会被调到。

    所以这里把 `yf_gate` 会包的 3 个方法 + 4 个 property 都按原形态提供，
    另用 `__getattr__` 兜住 `fast_info` 之类没列出的属性。
    """

    def __init__(self, *_a, **_k):
        pass

    def history(self, *_a, **_k):
        raise _OfflineInTests("yfinance Ticker.history 在测试里被显式钉死")

    def option_chain(self, *_a, **_k):
        raise _OfflineInTests("yfinance Ticker.option_chain 在测试里被显式钉死")

    def get_earnings_dates(self, *_a, **_k):
        raise _OfflineInTests("yfinance Ticker.get_earnings_dates 在测试里被显式钉死")

    @property
    def info(self):
        raise _OfflineInTests("yfinance Ticker.info 在测试里被显式钉死")

    @property
    def calendar(self):
        raise _OfflineInTests("yfinance Ticker.calendar 在测试里被显式钉死")

    @property
    def options(self):
        raise _OfflineInTests("yfinance Ticker.options 在测试里被显式钉死")

    @property
    def news(self):
        raise _OfflineInTests("yfinance Ticker.news 在测试里被显式钉死")

    def __getattr__(self, name):
        if name.startswith("_"):            # dunder / 私有走正常 AttributeError
            raise AttributeError(name)
        raise _OfflineInTests(f"yfinance Ticker.{name} 在测试里被显式钉死")


@pytest.fixture
def stub_yfinance(monkeypatch):
    """`yfinance.Ticker` / `yfinance.download` 钉成离线。

    全仓已核实无 `from yfinance import X`，所以改模块属性即覆盖所有调用点
    （这也正是 `yf_gate` 选择 patch 模块本身而非逐处接线的理由）。
    """
    import yfinance
    monkeypatch.setattr(yfinance, "Ticker", _OfflineTicker)
    monkeypatch.setattr(yfinance, "download", _raise_offline("yfinance.download"))

    # v0.45.524：yfinance 被钉成**立即**失败后，消费它的模块照样按生产退避节奏重试，
    # 每次 analyze 白睡 2×(0.7+1.4) 秒（整套里十几条恰好 4.2 秒的测试就是这个）。
    # 只换**点名**的接缝（`tests/_retry_seams.py`），不碰全局 `time.sleep`；
    # 生产配置下的退避节奏由 tests/test_retry_backoff_seams.py 钉住。
    # 返回值是记录 [(模块, 秒)]——想断言退避的测试直接拿来用；
    # 要真睡的测试在函数体里把 `_retry_sleep` 再 setattr 回去即可（后设的赢）。
    import importlib
    from tests._retry_seams import YF_RETRY_SLEEP_SEAMS
    slept = []
    for name in YF_RETRY_SLEEP_SEAMS:
        mod = importlib.import_module(name)
        assert callable(getattr(mod, "_retry_sleep", None)), f"{name} 缺退避接缝 _retry_sleep"
        monkeypatch.setattr(mod, "_retry_sleep", lambda s, _n=name: slept.append((_n, s)))
    return slept


@pytest.fixture
def offline_gate_blocked(request):
    """给**闸自身的测试**用：拿到本条测试被拦下的记录，并免去 teardown 判红。

    闸的测试必然要触发闸，否则证明不了它在工作。用这个出口，而不是把测试文件
    塞进 `_KNOWN_NETWORK_REACHERS` —— 那张表的含义是「还没还的债」，
    把闸的自证混进去会让债务表说谎，也会让它永远缩不到空。
    """
    request.node._offline_expect_blocks = True
    yield lambda: list(getattr(request.node, "_offline_blocked", []))


# ==================== 禁止测试碰 Slack ====================
# v0.45.575：原 `_block_slack`（v0.45.131，三道闸：假 token / 掐 webhook 探测 / 记录器会话）随
# `slack_report_notifier` 一起删除——它打桩的对象不存在了，仓库里已没有任何能发 Slack 的代码。
# 现在的防线：① 静态守卫 `tests/test_slack_send_whitelist.py`（白名单为空，R1–R6 + 墓碑认出任何重新引入的发送）；
# ② 本文件 `_offline_transport` 在传输层挡住一切出网（含 slack.com / hooks.slack.com）。


# ==================== cwd / sys.path 不许跨测试泄漏 ====================

def pytest_collection_finish(session):
    """记下收集结束时的 cwd 与相对 sys.path 项（v0.45.224）。

    收集期被 import 的模块若在 import 时 chdir，下面的逐测试还原管不到（它从第一条测试才开始记）。
    断言在 `test_reads_own_checkout.py::TestProcessStateStaysPut`。
    """
    session.config._alpha_hive_cwd_after_collection = os.getcwd()
    session.config._alpha_hive_relative_sys_path = [p for p in sys.path if not os.path.isabs(p)]


# ==================== weekly_optimizer 生产库隔离 ====================

@pytest.fixture(autouse=True)
def _isolate_weekly_optimizer_db(tmp_path, monkeypatch):
    """把 weekly_optimizer.PHEROMONE_DB_PATH 指向不存在的临时路径（v0.45.86）。

    （数据根迁移阶段 5 前置起它是默认 None 的覆盖钩子，缺省经 `_pheromone_db_path()`
    解析 `PATHS.db`，已会跟 `_isolate_env` 走；本夹具仍保留，因为测试依赖的是
    「库**不存在**」这一语义，而沙箱里的库可能被同一测试的其他代码建出来。
    此前该常量写死 `~/Desktop/Alpha Hive/pheromone.db`，完全不走隔离。）
    v0.45.86 起 Track A 会用它
    覆盖快照的 T+7 价格（见 weekly_optimizer._load_close_t7_map）——测试
    构造的 (ticker,date) 在生产库里查无匹配，会被误判"没有干净价格"整批
    丢弃，而不是真的在测原本要测的语义。指向不存在的路径，让查表函数走
    "库不存在→保留旧值"分支，把 actual_price_t7 的控制权还给测试自己构造
    的快照。**加数据源时必须同时确认测试里它被关掉了吗**——与本文件里
    `_block_same_day_macro` 记的同一条教训。需要单独验证覆盖行为的测试
    自己 monkeypatch 一个真实存在、有数据的临时库，会覆盖本 fixture。
    """
    try:
        import weekly_optimizer
    except Exception:  # pragma: no cover - 模块不可得时无需隔离
        return
    monkeypatch.setattr(weekly_optimizer, "PHEROMONE_DB_PATH",
                        tmp_path / "_no_such_pheromone.db", raising=False)


# ==================== paper_portfolio 生产状态隔离 ====================

_PP_STATE_FILES = ("positions.jsonl", "closed_trades.jsonl",
                   "equity_curve.jsonl", "meta.json")


def _pp_state_digest(path):
    """文件内容指纹；不存在给 'MISSING'（删除也算改动，不能悄悄放过）。"""
    import hashlib
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except (OSError, FileNotFoundError):
        return "MISSING"


@pytest.fixture(autouse=True, scope="session")
def _real_paper_portfolio_state_dir(pytestconfig):
    """生产 `paper_portfolio_state/` 的真身——在**任何 function 级隔离动手之前**求值一次（v0.45.303）。

    事故：`_isolate_paper_portfolio_state` 原先用 `_pp.STATE_DIR` 当「真身」，可 `STATE_DIR` 是
    `paper_portfolio` 的 **import 期常量**（`_base_dir()` 读 `PATHS.home` ⇒ `ALPHA_HIVE_HOME`）。
    若本进程里它的**首次导入**恰好发生在那个 function 级夹具内——此刻 `_isolate_env` 已把
    `ALPHA_HIVE_HOME` 指向 tmp——`STATE_DIR` 就被绑成沙箱，「真身」与新建的沙箱成了同一个目录：
    ① 测试往沙箱写任何状态文件都被误报「写到了生产」；② 更糟的是守卫**名义上在、实际盯的是沙箱自己**，
    真写穿生产它也看不见。整套/整文件跑不触发（约 10 个模块顶层就导入 `paper_portfolio`，收集期
    环境还是真的），只在「单条/子集 + 夹具内首次导入」时露出——**依赖导入顺序**。

    修法：真身不再从 `paper_portfolio` 读，而在**会话开始时**由生产自己的解析器 `PATHS.home` 求一次。
    为什么放在会话级夹具而不是别处：
      · 不能放 conftest 顶层 / `pytest_configure`——那里 import `hive_logger` 会改变 v0.45.239
        日志隔离守卫所依赖的时序（见 `pytest_configure` docstring 末句）；
      · pytest 保证**高 scope 夹具先于低 scope 夹具实例化**，故它早于 function 级 `_isolate_env`
        改环境变量——这靠的是 scope 语义，不是夹具名字母序的实现巧合。
    为什么用 `PATHS.home` 而不是「仓库根」（`__file__` 锚点）：状态目录是**数据**，跟 `ALPHA_HIVE_HOME`
    走（数据根迁移阶段 5 后指向 `~/alpha-hive-data`）；拿代码锚点推它，迁移后守卫会静默盯着旧路径。
    `PATHS.home` 是纯读（不建目录），此处不会给生产目录增加任何写入。

    **一致性自检**：若收集期已有人导入了 `paper_portfolio`（此刻没有任何测试动过它，
    `STATE_DIR` 还是收集期环境下的 import 期值），它必须与「同一推导在收集期环境下的值」一致——否则「本夹具对真身的推导」
    与「`paper_portfolio` 自己的推导」漂移了（例如有人把状态目录改名/迁走），守卫在盯过期路径。
    漂移就在这里红，而不是让守卫悄悄失明。未被收集期导入时无从比较（不假装比较过），
    该情形由 `tests/test_paper_portfolio_guard_identity.py` 在子进程里正面核对。

    v0.45.422：收集期环境不再是「真环境」——`pytest_configure` 先把 `ALPHA_HIVE_HOME` 换成会话沙箱
    （数据根缺省改成 `~/alpha-hive-data` 后，收集期冻住的常量否则会指向生产）。所以真身改按**调用 pytest 时**的
    环境求（v0.45.498 起与真实数据根闸同一份推导 `_root_data_guard.production_data_root`，其内只调生产规则 `resolve_data_root`）；自检比的是 `PATHS.home`（此刻 = 收集期的会话沙箱）下的同一推导。
    顺带：此前交互 shell 不设该变量，真身其实落在代码检出里的冻结副本上——守卫一直盯着旧数据，不是生产。
    """
    from hive_logger import PATHS
    # 「生产在哪」与真实数据根闸同一份推导（v0.45.498：此前这里自调 resolve_data_root，相对值下盯随 cwd 漂移的目录）
    real = pathlib.Path(_paper_portfolio_real_dir(pytestconfig._alpha_hive_invocation_env))
    imported = sys.modules.get("paper_portfolio")
    if imported is not None:
        frozen_expected = pathlib.Path(PATHS.home) / "paper_portfolio_state"
        assert imported.STATE_DIR == frozen_expected, (
            f"守卫对「生产状态目录」的推导与 paper_portfolio 自己的推导不一致：\n"
            f"  本夹具的推导（PATHS.home / 'paper_portfolio_state'，收集期环境）= {frozen_expected}\n"
            f"  paper_portfolio.STATE_DIR（收集期 import 期值）              = {imported.STATE_DIR}\n"
            "守卫盯的是过期路径 ⇒ 真写穿生产它也看不见。改 `_real_paper_portfolio_state_dir` "
            "使之与 `paper_portfolio.STATE_DIR` 的定义同步（或反之），不要在这里放行。")
    return real


def _paper_portfolio_real_dir(invocation_env) -> str:
    """paper_portfolio 状态目录的真身 = 测试眼里的生产数据根（`_root_data_guard.production_data_root`）下那一格。"""
    env = dict(invocation_env)                        # `_root_data_guard` 在本文件后面顶层导入，调用时已在
    return os.path.join(_root_data_guard.production_data_root(env, env.get("HOME") or os.path.expanduser("~")),
                        "paper_portfolio_state")


@pytest.fixture(autouse=True)
def _isolate_paper_portfolio_state(tmp_path, monkeypatch, _real_paper_portfolio_state_dir, pytestconfig):
    """把 paper_portfolio 的四个状态文件 + SNAPSHOT_DIR 全局重绑到 tmp，并核对状态文件真身没被动过。

    v0.45.104。事故：一个只想验「run_for_date 入口会拒绝错模式」的测试，
    monkeypatch 了 `_load_meta` / `_append_jsonl` / `_save_meta` 却漏了路径本身，
    于是跑到收尾那两行——
        `_write_jsonl(EQUITY_FILE, ...)` / `_write_jsonl(POSITIONS_FILE, ...)`
    ——**整体重写**了生产状态：equity_curve 93 行 → 1 行、positions 13 行 → 0。
    `_append_jsonl` 打桩挡不住它，因为收尾走的根本不是 append。

    两道防线，缺一不可：
      ① 四个路径全局默认指向 tmp —— 漏绑一个就出事，所以不靠各测试自觉；
      ② teardown 比对真身指纹 —— 兜住任何绕过全局的写法（直接用 STATE_DIR、
         subprocess 调 CLI 等）。没有②的话，将来有人加一条新写入路径，
         ①会静默失效而没人知道。
    需要读真实状态的测试自己 monkeypatch 回去即可覆盖①（②仍会看着它）。

    v0.45.274：`SNAPSHOT_DIR` 一并重绑（此前只读不写，见 `paper_portfolio.py` 该常量
    上方注释与 `test_paths_not_frozen_at_import.py::KNOWN` 里的旧结论「无写入 ⇒ 后果
    是测试读到 checkout 的真实快照，不是写穿」——那句话本身没错，但漏算了一层：
    `experiments/fg_exposure_gate_forward_test.py::run()` 把「能读到真实快照」当成
    「有真实前瞻样本」的信号，读到就会用 `paper_portfolio.run_replay()` **重放**那些
    真实历史日期，重放需要真实 OHLC，`_PRICE_CACHE` 里没有就会真的去打 yfinance——
    "只读"的后果链会一路淌到网络层。`ic_rerun_readiness.main()`
    （`TestCarriedByReadiness` 系列测试在跑）不分场景地调用
    `fg_exposure_gate_forward_status()`，而后者不接受调用方传路径、只能走这个模块级
    常量，所以漏绑它 = 这些测试在全套里必然伸手摸生产快照目录。
    没有 SNAPSHOT_DIR 的测试自己按需要 `monkeypatch.setattr(_pp, "SNAPSHOT_DIR", ...)`
    覆盖即可（`test_fg_exposure_gate_forward_test.py` 已经这么做）。

    v0.45.303：「真身」**不再读 `_pp.STATE_DIR`**，改吃会话级夹具 `_real_paper_portfolio_state_dir`。
    `STATE_DIR` 是 import 期常量，`paper_portfolio` 若首次导入就发生在本夹具内（`_isolate_env` 已把
    `ALPHA_HIVE_HOME` 指向 tmp），它会被冻成沙箱路径，`real` 与下面的 `sandbox` 成了同一个目录——
    守卫盯着沙箱自己，单条测试写沙箱被误报为写穿生产，真写穿生产反而看不见。
    ⚠️ 别改回 `_pp.STATE_DIR`：回归测试 `tests/test_paper_portfolio_guard_identity.py` 会红。

    v0.45.577：写沙箱生效且真身落在被禁写的根下时，②（teardown 指纹比对）**不再判红**——测试进程树写不进去
    （内核拒，被吞的写由会话级 `_guard_by_write_sandbox` 从统一日志里逐条定位报红），指纹变化只可能来自沙箱外的写入者：
    14:00 每日扫描收尾就写这四个文件，v0.45.573 漏改这里 ⇒ 扫描时段全量里会有一串逐条 ERROR。①（全局重绑）照旧。
    """
    try:
        import paper_portfolio as _pp
    except Exception:  # pragma: no cover - 模块不可得时无需隔离
        return

    real = {n: _real_paper_portfolio_state_dir / n for n in _PP_STATE_FILES}
    before = {n: _pp_state_digest(p) for n, p in real.items()}

    sandbox = tmp_path / "paper_portfolio_state"
    sandbox.mkdir(exist_ok=True)
    # 嵌套在 `sandbox` 下面，不直接用 `tmp_path / "report_snapshots"`——那个字面量
    # 会跟测试自己声明的同名 fixture（如 `test_outcomes_fetcher.py::snap_dir`）撞名，
    # 撞上时后者 `d.mkdir()`（不带 exist_ok）会因为目录已存在而 FileExistsError
    # （v0.45.274 实测：换这个名字前，19/20 条 `test_outcomes_fetcher.py` 测试撞红）。
    # 不需要 mkdir：`SNAPSHOT_DIR` 只被 `.glob()` 读，从不写；`Path.glob()` 对不存在的
    # 目录直接返回空迭代器，不会抛异常——这正是我们想要的"没有真实快照"效果。
    sandbox_snapshots = sandbox / "_snapshot_sandbox"
    monkeypatch.setattr(_pp, "SNAPSHOT_DIR", sandbox_snapshots, raising=False)
    monkeypatch.setattr(_pp, "STATE_DIR", sandbox, raising=False)
    monkeypatch.setattr(_pp, "POSITIONS_FILE", sandbox / "positions.jsonl")
    monkeypatch.setattr(_pp, "CLOSED_FILE", sandbox / "closed_trades.jsonl")
    monkeypatch.setattr(_pp, "EQUITY_FILE", sandbox / "equity_curve.jsonl")
    monkeypatch.setattr(_pp, "META_FILE", sandbox / "meta.json")
    # v0.45.416：拆股记录缓存的键是 (ticker, entry_date, **真实**今天)——同一次 pytest 里两个测试用同一个
    # (标的, 入场日) 喂不同的假世界（GAPX / 08-10 就有两份），前一个查到的拆股会原样漏给后一个，结果取决于执行顺序。
    monkeypatch.setattr(_pp, "_SPLIT_EVENTS_CACHE", {}, raising=False)

    yield

    if _write_sandbox.covers(getattr(pytestconfig, "_alpha_hive_write_sandbox", None), _real_paper_portfolio_state_dir):
        return          # 内核护着：变化只可能来自沙箱外（会话末尾的终端摘要会列出数据根变化）
    touched = [n for n, p in real.items() if _pp_state_digest(p) != before[n]]
    assert not touched, (
        f"测试写到了**生产** paper_portfolio_state/：{touched}。"
        "四个路径全局已默认指向 tmp，还能改到真身说明有绕过它们的写入路径——"
        "去把那条路径也接到模块级常量上，不要在这里放行。")


@pytest.fixture(autouse=True)
def _isolate_feedback_loop_close_t7_db(tmp_path, monkeypatch):
    """把 feedback_loop.PHEROMONE_DB_PATH 指向不存在的临时路径（v0.45.87）。

    v0.45.87 把 close_t7 干净口径的实现从 weekly_optimizer.py 挪到了
    feedback_loop.py，供 `BacktestAnalyzer(clean_t7=True)` 的全部消费者
    共用——包括这个模块自己缺省的 PHEROMONE_DB_PATH
    (`Path(__file__).parent / "pheromone.db"`，即真实生产库路径)。
    测试若构造 `BacktestAnalyzer(clean_t7=True)` 而不显式覆盖
    close_t7_db_path，会在本机真实生产库里查询——与
    `_isolate_weekly_optimizer_db` 记的是同一条教训（加数据源时必须同时
    确认测试里它被关掉了吗）。指向不存在的路径，让查表函数走
    "库不存在→保留旧值"分支。需要单独验证覆盖行为的测试自己 monkeypatch
    一个真实存在、有数据的临时库（或显式传 close_t7_db_path），会覆盖/
    绕开本 fixture。
    """
    try:
        import feedback_loop
    except Exception:  # pragma: no cover - 模块不可得时无需隔离
        return
    monkeypatch.setattr(feedback_loop, "PHEROMONE_DB_PATH",
                        tmp_path / "_no_such_pheromone.db", raising=False)


@pytest.fixture(autouse=True)
def _fg_sub_status_in_process(monkeypatch):
    """进程内调 `ic_rerun_readiness.main()` 时，F&G 子状态留在本进程算（v0.45.392）。

    生产里 `main()` 把 F&G 子状态放进**子进程**（带时间预算，见该模块 docstring）。可本仓进程内调 `main()`
    的测试（`TestCarriedByReadiness` ×2、`TestEtaAndExitCodes`、`TestOkPath` …）全靠进程内的东西：
    monkeypatch 打桩 `fg_exposure_gate_forward_status`、上面 `_isolate_paper_portfolio_state` 沙箱化的
    `SNAPSHOT_DIR`、`_offline_transport` 出网闸——**一样都到不了子进程**。不置这个钩子，那些桩会被无声绕过，
    子进程还可能读真实快照、真去打 yfinance（出网闸管不到它）。

    子进程路径本身由 `tests/test_ic_rerun_fg_budget.py` 显式 `setattr(rr, "_FG_IN_PROCESS", False)` 测
    （假子进程 + 真子进程两层），真实 CLI 子进程测试（`test_step_contract_ic_rerun.py::TestRealCli`）
    不经本夹具、走的就是生产路径。
    """
    try:
        import ic_rerun_readiness
    except Exception:  # pragma: no cover - 模块不可得时无需处理
        return
    monkeypatch.setattr(ic_rerun_readiness, "_FG_IN_PROCESS", True)


# ==================== Mock 股票数据 ====================

MOCK_STOCK_DATA = {
    "NVDA": {
        "price": 142.50,
        "momentum_5d": 3.2,
        "avg_volume": 45_000_000,
        "volume_ratio": 1.4,
        "volatility_20d": 38.5,
    },
    "TSLA": {
        "price": 340.00,
        "momentum_5d": -1.8,
        "avg_volume": 80_000_000,
        "volume_ratio": 0.9,
        "volatility_20d": 52.0,
    },
    "VKTX": {
        "price": 48.20,
        "momentum_5d": 7.5,
        "avg_volume": 3_000_000,
        "volume_ratio": 2.1,
        "volatility_20d": 65.0,
    },
}


@pytest.fixture
def mock_stock_data(monkeypatch):
    """Mock yfinance 数据，避免测试中调用外部 API"""
    def _mock_fetch(ticker, target_date=None):
        return MOCK_STOCK_DATA.get(ticker, MOCK_STOCK_DATA["NVDA"])

    from swarm_agents import cache as _swarm_cache
    monkeypatch.setattr(_swarm_cache, "_fetch_stock_data", _mock_fetch)
    return MOCK_STOCK_DATA


# ==================== PheromoneBoard fixture ====================

@pytest.fixture
def board():
    """创建干净的信息素板"""
    from pheromone_board import PheromoneBoard
    b = PheromoneBoard()
    yield b
    b.clear()


# ==================== Agent fixtures ====================

@pytest.fixture
def all_agents(board, mock_stock_data):
    """实例化 6 个 Agent（使用 mock 数据）"""
    from swarm_agents import (
        ScoutBeeNova, OracleBeeEcho, BuzzBeeWhisper,
        ChronosBeeHorizon, RivalBeeVanguard, GuardBeeSentinel,
    )
    return {
        "scout": ScoutBeeNova(board),
        "oracle": OracleBeeEcho(board),
        "buzz": BuzzBeeWhisper(board),
        "chronos": ChronosBeeHorizon(board),
        "rival": RivalBeeVanguard(board),
        "guard": GuardBeeSentinel(board),
    }


@pytest.fixture
def bear_bee(board, mock_stock_data):
    """创建 BearBeeContrarian（Phase-2 看空对冲蜂）"""
    from swarm_agents import BearBeeContrarian
    return BearBeeContrarian(board)


@pytest.fixture
def queen(board):
    """创建 QueenDistiller"""
    from swarm_agents import QueenDistiller
    return QueenDistiller(board)


# ==================== MemoryStore fixture ====================

@pytest.fixture
def memory_store(tmp_path):
    """创建隔离的 MemoryStore（使用临时 DB）"""
    from memory_store import MemoryStore
    ms = MemoryStore(db_path=str(tmp_path / "test_memory.db"))
    yield ms
    ms.close()


@pytest.fixture(autouse=True)
def _fast_yfinance_limiter(monkeypatch):
    """测试期间把 yfinance 限流速率调到不产生等待（v0.45.56）。

    生产速率是 0.5 req/s（2s 一个令牌）—— 那是给真实 Yahoo 配额用的。
    单测里 yfinance 全被 mock，令牌等待没有任何语义，只是纯粹的墙钟浪费：
    `test_dashboard_renderer` 逐票渲染 30 只标的，实测从秒级涨到 **56s**，
    在全量跑里直接撞 60s 超时。

    ⚠️ 这里只改**速率**，不改任何行为。真正验证限流生效的
    `tests/test_yf_gate.py::TestGateBehaviour::test_throttles` 自带慢桶，
    不依赖这个全局值 —— 否则那条测试会被本 fixture 架空成永真。
    """
    try:
        import resilience
        from resilience import RateLimiter
    except ImportError:  # pragma: no cover
        return
    fast = RateLimiter(rate=10_000.0, burst=1000)
    monkeypatch.setattr(resilience, "yfinance_limiter", fast, raising=False)

    # `from resilience import yfinance_limiter` 的模块**各自持有一份绑定**，
    # 只改 resilience 上的名字够不到它们 —— 必须逐个替换。
    # （这正是 yf_gate 选择 patch yfinance 模块本身、而非逐处接线的原因：
    #   全仓已核实无 `from yfinance import X`，所以那边不存在这个问题。）
    import importlib
    for _mod in ("swarm_agents.cache", "earnings_watcher", "yf_gate"):
        try:
            _m = importlib.import_module(_mod)
        except ImportError:  # pragma: no cover
            continue
        if hasattr(_m, "yfinance_limiter"):
            monkeypatch.setattr(_m, "yfinance_limiter", fast)
        if _mod == "yf_gate":
            monkeypatch.setattr(_m, "_bucket", fast, raising=False)


# ==================== 熔断器状态不许跨测试泄漏（v0.45.344）====================

# 按「类」枚举，不按「实例名单」：两个熔断器类在构造时把自己登记进各自的 WeakSet，
# 这里只需知道类在哪个模块。新增熔断器（模块级、单例里、测试自建）零改动即被覆盖。
_BREAKER_CLASSES = (("resilience", "CircuitBreaker"),
                    ("data_pipeline", "ObservableCircuitBreaker"))
_BREAKER_RESET_TIMEOUT_S = 2.0
# 已知卡死的熔断器 → 最早发现它的测试。再遇到只试一次（timeout=0），不再每条等 2s。
_stuck_breakers = weakref.WeakKeyDictionary()


def _reset_live_breakers(where, timeout=_BREAKER_RESET_TIMEOUT_S):
    """把每个活着的熔断器 reset() 回刚构造的样子；返回重置失败的说明列表（空 = 全部成功）。

    类走 `sys.modules.get`、不 import：模块没被谁 import 过 ⇒ 这一类一个实例都不存在，
    无事可做；而在 conftest 里 import `resilience` / `data_pipeline` 会把它们提前到
    收集期（CLAUDE.md「新产物的默认路径」一节：收集期 import 是路径冻结的事故窗口）。
    """
    stuck = []
    for modname, clsname in _BREAKER_CLASSES:
        mod = sys.modules.get(modname)
        if mod is None:
            continue
        cls = getattr(mod, clsname)     # 模块在、类没了 ⇒ 改名了：让它红，别静默跳过
        for br in cls.live_instances():
            known = _stuck_breakers.get(br)
            try:
                br.reset(timeout=0 if known else timeout)
            except Exception as e:      # noqa: BLE001 —— 任何重置失败都要报出来，不吞
                _stuck_breakers.setdefault(br, where)
                stuck.append(f"{clsname}[{br.name}]（最早在 {_stuck_breakers[br]} 发现）：{e}")
            else:
                _stuck_breakers.pop(br, None)
    return stuck


def _fail_on_stuck_breakers(phase, stuck):
    if stuck:
        pytest.fail(
            f"测试{phase}有熔断器重置不了。锁在测试之间被攥着本身就是 bug —— 多半是某条测试"
            "留下的线程卡死在锁里（形状见 test_slack_send_whitelist.py::TestBreakerDoesNotDeadlock）；"
            "阻塞等只会把整套卡死，所以直接红：\n  " + "\n  ".join(stuck),
            pytrace=False)


@pytest.fixture(autouse=True)
def _reset_circuit_breakers(request):
    """每条测试前后把**所有活着的**熔断器重置成刚构造的样子（v0.45.344）。

    熔断状态是进程级全局量：`resilience` 的四个模块级实例、`fred_macro._fred_breaker`、
    `newsapi_client._news_breaker`、`data_pipeline.get_fetcher()` 单例里各数据源的
    `ObservableCircuitBreaker`。此前没有套件级隔离，实测（v0.45.343）：A 把
    `yfinance_breaker` 连记到阈值、不重置，B 断言 `allow_request() is False` **通过**——
    B 默默吃了 A 漏出来的 OPEN。当时全套零条测试受影响（潜伏，不是活跃），但已经有
    四个文件各自打了补丁（重置 / 换新实例），说明以前咬过人。

    为什么在同一批对象上 reset，不换新实例：`sec_edgar` / `swarm_agents.cache` /
    `options_analyzer` 在模块顶部 `from resilience import sec_breaker` / `yfinance_breaker`，
    各持一份绑定 —— 换掉 `resilience` 上的名字够不到它们（与 `_fast_yfinance_limiter`
    要逐模块替换是同一个坑）。

    **前后各一次**：开始前那次保证本条干净；结束后那次让「锁被攥着」报在**留下它的那条**
    测试上，而不是报在无辜的下一条上。拿不到锁不阻塞（见 `_fail_on_stuck_breakers`）。
    自证：`tests/test_breaker_isolation.py`。
    """
    _fail_on_stuck_breakers("开始前", _reset_live_breakers(request.node.nodeid))
    yield
    _fail_on_stuck_breakers("结束后", _reset_live_breakers(request.node.nodeid))


@pytest.fixture
def reset_live_breakers():
    """把 `_reset_live_breakers` 暴露给它的自证测试（`conftest` 不可直接 import）。"""
    return _reset_live_breakers


# ==================== ML 模型产物隔离（v0.45.149）====================

_ML_MODEL_FILES = ("ml_model.json", "ml_model_cache.json", "ml_model_extended.json")


def _ml_model_digest(path):
    """文件内容指纹；不存在给 'MISSING'（删除/新建也算改动，不能悄悄放过）。"""
    import hashlib
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except (OSError, FileNotFoundError):
        return "MISSING"


def _assert_default_path_in_sandbox(raw, tmp_path):
    """`_isolate_ml_model_file` 防线①：模型落盘位置是**绝对路径**且在 tmp 沙箱里。

    先断言绝对、再判包含（v0.45.240）：`Path(相对).resolve()` 按 **cwd** 补全，而 v0.45.224 起测试期间
    cwd 就在本测试的 tmp 里 ⇒ 路径被改回 `"ml_model.json"` 这类相对值时 resolve 出
    `tmp/_cwd/ml_model.json`，包含判定恒真、本闸不响（实测）。测试里这条相对路径只写进空目录，
    **伤的是生产**：编排器从仓库根跑，写穿的正是 v0.45.149 那次事故的模型文件。
    （v0.45.570 前核对的是 `default_model_path()`；缺省路径删除后核对生产唯一显式写的 `PATHS.ml_model_cache`。）
    """
    p = pathlib.Path(raw)
    assert p.is_absolute(), (
        f"ML 模型默认落盘位置不是绝对路径：{raw!r}。测试期间 cwd 在 tmp 里所以这里看着无害，"
        "生产从仓库根跑就会写穿仓库根的模型文件 —— save_model/load_model 的默认值又被改回相对路径了？")
    resolved = p.resolve()
    assert resolved.is_relative_to(tmp_path.resolve()), (
        f"ML 模型默认落盘位置逃出了测试沙箱：{resolved}（沙箱应为 {tmp_path}）。"
        "多半是这条路径被写成了模块级常量 / 类属性（import 时求值 = 冻住旧值）。")


@pytest.fixture(autouse=True)
def _isolate_ml_model_file(request, tmp_path, monkeypatch):
    """核对测试没把 ML 模型写进仓库根（或 pytest 的 cwd）。

    v0.45.149。事故：`ml_predictor` 的三对 `save_model/load_model` 默认值曾是
    **cwd 相对路径** `"ml_model.json"`，而 `MLPredictionService.train_model()`
    在训练成功后**无参**调用 `save_model()`，`tests/` 里有 11 处
    `svc.train_model()` 不传路径 —— 于是**在主 checkout 跑一次 pytest，就用
    夹具模型覆盖了仓库根的模型文件**。2026-09-07 01:15 实际发生：
    `n=497 / acc 71.63 / oos 44.35` 被写成 `n=30 / acc 96.67 / oos None`，
    且后者对 15 组不同输入只吐 1 个值（常数函数）。

    两道防线：

      ① **路径侧**（在生产代码里，不在这个 fixture 里）：v0.45.149 把默认值改为
         `ml_predictor.default_model_path()` → `PATHS.home / …`；**v0.45.570 起干脆没有默认值**
         （`save_model` / `load_model` 的 filename 必填、`train_model` 不存盘，墓碑
         `tests/test_ml_model_default_path_retired.py`）。生产唯一的显式落点是
         `PATHS.ml_model_cache`，而 `PATHS.home` 读 `ALPHA_HIVE_HOME`，上面的 `_isolate_env`
         已经把它指向 tmp_path。本 fixture 在 setup 时**正面核对**它真的落在沙箱里，而不是再
         monkeypatch 一遍把问题盖住（盖住了就再也测不出它退化）。

      ② **指纹侧**：teardown 比对真身。兜住任何绕过①的写法 —— 新写的测试
         硬编码相对路径、subprocess 调 CLI、有人把默认值改回相对字符串。
         没有②的话，①将来静默失效不会有人知道（CLAUDE.md 硬检查项：
         「这个失败，下游怎么知道？」）。

    被保护的是**三个**文件，不只是出事的那个：`ml_model_cache.json` 当时是生产
    真正读的那份（2026-10-19 重查：08-28 / 09-04 正是它被 pytest 写穿——经当时冻结的类属性，
    写进的是 n=200 模型；v0.45.551 起它只写不读，仍是取证副本，照样要隔离）。
    """
    try:
        import ml_predictor as _mp
    except Exception:  # pragma: no cover - 模块不可得时无需隔离
        return

    # 真身所在：模块自己的目录。不能用 PATHS.home —— 它已经被 _isolate_env
    # 指向 tmp 了，拿它找真身等于什么都没查（恒真的守卫）。
    repo_root = pathlib.Path(_mp.__file__).resolve().parent
    watched = {repo_root / n for n in _ML_MODEL_FILES}
    # 「pytest 的 cwd」指**调用目录**，不能用 `Path.cwd()`（v0.45.240）：v0.45.224 起本 fixture setup 时
    # cwd 已被 `_isolate_cwd_and_sys_path`（按名字排在前面）挪进本测试的空目录，这一臂从那时起
    # 看的是 tmp —— 实测路径在收集期冻结到调用目录、测试往里写 ml_model.json，本闸不响。
    watched |= {pathlib.Path(request.config.invocation_params.dir).resolve() / n for n in _ML_MODEL_FILES}
    before = {p: _ml_model_digest(p) for p in watched}

    # 防线①的正面核对：生产唯一的模型落点必须落在 tmp 沙箱里（v0.45.570 前核对 default_model_path()）。
    from hive_logger import PATHS as _PATHS
    _assert_default_path_in_sandbox(str(_PATHS.ml_model_cache), tmp_path)

    yield

    touched = sorted(str(p) for p in watched if _ml_model_digest(p) != before[p])
    assert not touched, (
        f"测试写到了**仓库根/cwd** 的 ML 模型文件：{touched}。"
        "默认落盘位置已经全局指向 tmp，还能改到真身说明有绕过它的写入路径"
        "（硬编码相对路径？subprocess？）——落盘路径一律从 `PATHS` 调用时求值，不要在这里放行。")

# ==================== 生产产物总闸（v0.45.150） ====================

# ⚠️ 这里刻意用 `__file__` 而不是 `PATHS.home`：本闸要盯的正是**真实 checkout
#    根目录**，必须免受 `_isolate_env` 影响。同一写法在生产模块里是 bug，
#    在这里是需求。
_REPO_ROOT_FOR_GUARD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _artifact_signature(path):
    """(存在性, 大小, mtime_ns) 摘要；目录则递归汇总。

    用 stat 而非内容哈希：本闸问的是「有没有被写过」，而 sqlite 以读写模式
    打开就会顶 mtime（v0.45.150 实测：内容逐字节未变，但 mtime 变了并留下
    `-wal`/`-shm`）。内容哈希反而会把这种「打开了但没改行」判成无事发生。
    """
    if not os.path.exists(path):
        return "MISSING"
    if os.path.isfile(path):
        st = os.stat(path)
        return f"file:{st.st_size}:{st.st_mtime_ns}"
    acc = []
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in sorted(filenames):
            fp = os.path.join(dirpath, fn)
            try:
                st = os.stat(fp)
                acc.append(f"{os.path.relpath(fp, path)}:{st.st_size}:{st.st_mtime_ns}")
            except OSError:
                acc.append(f"{os.path.relpath(fp, path)}:ERR")
    return "dir:" + "|".join(sorted(acc))


def _assert_sell_strike_state_in_sandbox(resolved, tmp_path):
    """`_isolate_sell_strike_state` 防线①：每个解析结果都是绝对路径、且（词法上）落在 tmp 沙箱内。

    返回 `{名字: 路径字符串}`——只有全部断言都过了才有返回值，所以「记下来的」就是「核对过的」。
    先断言绝对、再做**纯词法**包含判定（`normpath` 消掉 `..`，不碰 cwd、不 resolve）：
    `_isolate_env` 把 `ALPHA_HIVE_HOME` 设成 `str(tmp_path)`，正确解析时前缀逐字相同；
    相对路径若拿 cwd 补全再判，测试期间 cwd 就在 tmp 里、判定恒真（`_assert_default_path_in_sandbox`
    记的 v0.45.240 同一个坑），而 resolve 未锚定的路径又会被 test_reads_own_checkout 的 cwd 守卫判红。
    """
    checked = {}
    for name, raw in resolved.items():
        p = pathlib.Path(raw)
        assert p.is_absolute(), (
            f"{name} 解析成了相对路径 {raw!r}：测试期间 cwd 在 tmp 里看着无害，"
            "生产从仓库根跑就会写穿仓库根的卖权账本。")
        assert pathlib.Path(os.path.normpath(p)).is_relative_to(tmp_path), (
            f"{name} = {p} 逃出了测试沙箱（应在 {tmp_path} 内）。"
            "多半是它被求值成了模块级常量 / 类属性 / 默认参数（import 期冻结），"
            "`_isolate_env` 的 setenv 追不上它。改成调用时读 `PATHS.sell_strike_state`。")
        checked[name] = str(p)
    return checked


@pytest.fixture(autouse=True)
def _isolate_sell_strike_state(_isolate_env, request, tmp_path):
    """卖权行权价账本目录的两道防线（v0.45.333，照 `_isolate_ml_model_file` 的写法）。

    ① **setup 正面核对**（`_assert_sell_strike_state_in_sandbox`）：`PATHS.sell_strike_state` 与账本
       自己的解析器 `sell_strike_ledger._state_dir()` 此刻都落在本测试的 tmp 沙箱里。
       不再 monkeypatch 一遍去「保证」它——盖住了就再也测不出它退化（有人把 `_state_dir`
       冻成模块级常量时，这一道会在**每一条**测试上红）。
    ② **teardown 比对真身指纹**：`hive_logger` 所在目录（checkout 根）与 pytest 调用目录下的
       `sell_strike_state/`，用 `_artifact_signature`（stat 口径，理由见其 docstring）。
       兜住任何绕过 ① 的写法：硬编码路径、subprocess 跑 CLI（带着现造的 env）、`__file__` 派生。
       没有 ②，① 将来静默失效不会有人知道；没有 ①，② 只能事后发现。

    显式依赖 `_isolate_env`：本闸核对的正是它设的 `ALPHA_HIVE_HOME`，不靠 autouse 名字字母序的巧合。
    ① 核对过的 `{名字: 路径}` 记在 `request.config._alpha_hive_sell_strike_guard_checked`，供「防线确实
    接上」的自证测试读（`conftest` 不可 import，只能经 config / fixture 传）；判据本身经
    `sell_strike_state_sandbox_check` fixture 暴露给它的有牙自证。
    """
    import hive_logger

    real_dirs = {pathlib.Path(hive_logger.__file__).resolve().parent / "sell_strike_state",
                 pathlib.Path(request.config.invocation_params.dir).resolve() / "sell_strike_state"}
    before = {p: _artifact_signature(str(p)) for p in real_dirs}

    resolved = {"PATHS.sell_strike_state": hive_logger.PATHS.sell_strike_state}
    try:
        import sell_strike_ledger as _ssl
    except Exception:  # pragma: no cover - 模块坏了由它自己的测试报红，这里只核 PATHS
        _ssl = None
    if _ssl is not None:
        resolved["sell_strike_ledger._state_dir()"] = _ssl._state_dir()
    request.config._alpha_hive_sell_strike_guard_checked = _assert_sell_strike_state_in_sandbox(
        resolved, tmp_path)

    yield

    touched = sorted(str(p) for p in real_dirs if _artifact_signature(str(p)) != before[p])
    assert not touched, (
        f"测试写到了**真身** sell_strike_state/：{touched}。"
        "路径已由 `PATHS.sell_strike_state` 调用时求值指向 tmp，还能写到真身说明有绕过它的写入路径"
        "（硬编码 / `__file__` 派生 / subprocess 丢了 env）——去把那条路径接到 "
        "`sell_strike_ledger._state_dir()` 上，不要在这里放行。")


@pytest.fixture(autouse=True)
def _isolate_alphabot_state(_isolate_env, tmp_path):
    """Alpha Bot 状态目录（v0.45.388）防线①：`PATHS.alphabot_state` 此刻是绝对路径且在本测试沙箱里。
    防线②（真身指纹）由仓库根默认拒绝总闸与真实数据根总闸（`_guard_real_data_root`）兜住。
    判据同 `_assert_sell_strike_state_in_sandbox`（纯词法，不拿 cwd 补全）。"""
    import hive_logger

    p = pathlib.Path(hive_logger.PATHS.alphabot_state)
    assert p.is_absolute(), f"PATHS.alphabot_state 解析成了相对路径 {p}：生产从仓库根跑会写穿仓库根"
    assert pathlib.Path(os.path.normpath(p)).is_relative_to(tmp_path), (
        f"PATHS.alphabot_state = {p} 逃出了测试沙箱（应在 {tmp_path} 内）——被冻成模块级常量了？")
    yield


@pytest.fixture(autouse=True)
def _isolate_replay_ohlc_state(_isolate_env, tmp_path):
    """回放行情库（v0.45.410，`replay_ohlc_store`）防线①：`PATHS.replay_ohlc_state` 此刻是绝对路径且在本测试沙箱里。
    F&G 前瞻检验的 `run()` 每次都会往这里写——测试里调 `run()` 的地方很多，任何一处把路径冻住都会写进真身。
    防线②（真身指纹）由仓库根默认拒绝总闸与真实数据根总闸（`_guard_real_data_root`，v0.45.409）兜住。"""
    import hive_logger

    p = pathlib.Path(hive_logger.PATHS.replay_ohlc_state)
    assert p.is_absolute(), f"PATHS.replay_ohlc_state 解析成了相对路径 {p}：生产从仓库根跑会写穿仓库根"
    assert pathlib.Path(os.path.normpath(p)).is_relative_to(tmp_path), (
        f"PATHS.replay_ohlc_state = {p} 逃出了测试沙箱（应在 {tmp_path} 内）——被冻成模块级常量了？")
    yield


@pytest.fixture(autouse=True)
def _isolate_observation_archive(_isolate_env, tmp_path):
    """H1 原始观测档案（v0.45.568，`observation_archive`）防线①：`PATHS.observation_archive` 此刻是绝对路径且在本测试沙箱里。
    另：档案开关是**进程级**状态（扫描进程 `enable()` 一次）——一条测试跑了 `main()` 就会让之后每条测试的取数都落盘，
    所以每条测试前后都复位（关档、清已知地址、清统计）。防线②（真身指纹）由仓库根默认拒绝总闸与真实数据根总闸兜住。"""
    import hive_logger
    import observation_archive

    p = pathlib.Path(hive_logger.PATHS.observation_archive)
    assert p.is_absolute(), f"PATHS.observation_archive 解析成了相对路径 {p}：生产从仓库根跑会写穿仓库根"
    assert pathlib.Path(os.path.normpath(p)).is_relative_to(tmp_path), (
        f"PATHS.observation_archive = {p} 逃出了测试沙箱（应在 {tmp_path} 内）——被冻成模块级常量了？")
    observation_archive._reset_for_tests()
    yield
    observation_archive._reset_for_tests()


@pytest.fixture(autouse=True)
def _isolate_straddle_prereg_result(_isolate_env, tmp_path):
    """跨式 × GEX 预注册的冻结结果（v0.45.428，`straddle_gex_prereg.run_once`）防线①：`PATHS.straddle_prereg_result`
    此刻是绝对路径且在本测试沙箱里。这个文件**只写一次、永不覆盖**——误写进真身就把协议永久冻住，比别的产物更不能漏。
    防线②（真身指纹）由仓库根默认拒绝总闸与真实数据根总闸（`_guard_real_data_root`）兜住。"""
    import hive_logger

    p = pathlib.Path(hive_logger.PATHS.straddle_prereg_result)
    assert p.is_absolute(), f"PATHS.straddle_prereg_result 解析成了相对路径 {p}：生产从仓库根跑会写穿仓库根"
    assert pathlib.Path(os.path.normpath(p)).is_relative_to(tmp_path), (
        f"PATHS.straddle_prereg_result = {p} 逃出了测试沙箱（应在 {tmp_path} 内）——被冻成模块级常量了？")
    yield


@pytest.fixture(autouse=True)
def _isolate_straddle_strangle_shadow_result(_isolate_env, tmp_path):
    """影子宽跨预注册的冻结结果（v0.45.586，`straddle_strangle_shadow.run_once`）防线①：同上一个夹具——
    `PATHS.straddle_strangle_shadow_result` 此刻是绝对路径且在本测试沙箱里（只写一次、永不覆盖，误写进真身会永久冻住协议）。
    防线②（真身指纹）由仓库根默认拒绝总闸与真实数据根总闸兜住。另清零本进程的影子记录计数（故障登记是进程级全局状态，
    不清的话上一条测试造的故障会让下一条测试的 `stats()["errors"]` 非零）。"""
    import hive_logger
    import straddle_strangle_shadow

    p = pathlib.Path(hive_logger.PATHS.straddle_strangle_shadow_result)
    assert p.is_absolute(), f"PATHS.straddle_strangle_shadow_result 解析成了相对路径 {p}：生产从仓库根跑会写穿仓库根"
    assert pathlib.Path(os.path.normpath(p)).is_relative_to(tmp_path), (
        f"PATHS.straddle_strangle_shadow_result = {p} 逃出了测试沙箱（应在 {tmp_path} 内）——被冻成模块级常量了？")
    straddle_strangle_shadow.reset_stats()
    yield
    straddle_strangle_shadow.reset_stats()


@pytest.fixture(autouse=True)
def _reset_stock_fetch_legacy_stats():
    """v0.45.639：`swarm_agents.cache` 的「行情改走旧回落」计数是进程级全局状态（同上一条的影子宽跨计数）。已有测试会把 data_pipeline
    弄成不可导入来测旧回落（test_volatility_fallback / test_silent_failure_guards），不清的话后面读实时计数（scan_timing.counters()）的
    测试会带着非零值跑、结果随顺序变。只在模块已导入时清——不在这里触发导入，免得改变首次导入的位置。"""
    mod = sys.modules.get("swarm_agents.cache")
    if mod is not None:
        mod._reset_legacy_fallback_stats()
    yield
    mod = sys.modules.get("swarm_agents.cache")
    if mod is not None:
        mod._reset_legacy_fallback_stats()


@pytest.fixture(autouse=True)
def _reset_event_row_stats():
    """v0.45.645：`price_metrics` 的「日线公司行动空行」计数是进程级全局状态（同上一条）。只在模块已导入时清——不在这里触发导入。"""
    mod = sys.modules.get("price_metrics")
    if mod is not None:
        mod._reset_event_row_stats()
    yield
    mod = sys.modules.get("price_metrics")
    if mod is not None:
        mod._reset_event_row_stats()


@pytest.fixture(autouse=True)
def _reset_yf_daily_stats():
    """v0.45.652：`yf_daily` 唯一入口的公司行动空行计数是进程级全局状态（同上一条）。只在模块已导入时清——不在这里触发导入。"""
    mod = sys.modules.get("yf_daily")
    if mod is not None:
        mod._reset_stats()
    yield
    mod = sys.modules.get("yf_daily")
    if mod is not None:
        mod._reset_stats()


@pytest.fixture
def sell_strike_state_sandbox_check():
    """把 `_assert_sell_strike_state_in_sandbox` 暴露给它的有牙自证（同 `default_path_sandbox_check`）。"""
    return _assert_sell_strike_state_in_sandbox


@pytest.fixture
def artifact_signature():
    """把上面的指纹函数暴露给测试用。

    `tests/` 不是包、`conftest` 不可直接 import，所以走 fixture 而不是
    `from conftest import ...`。目的是让 session 级总闸和逐条测试**共用同一个
    判据**——两份实现早晚漂移，而漂移的那一刻两边都还是绿的。
    """
    return _artifact_signature


@pytest.fixture
def default_path_sandbox_check():
    """把 `_assert_default_path_in_sandbox` 暴露给它的自证测试（同上：`conftest` 不可直接 import）。"""
    return _assert_default_path_in_sandbox


@pytest.fixture
def paper_portfolio_real_dir_fn():
    """把 `_paper_portfolio_real_dir` 暴露给对拍测试（同上：`conftest` 不可直接 import）。"""
    return _paper_portfolio_real_dir


@pytest.fixture
def write_sandbox_guard_fn():
    """把写沙箱生效时的判定 `_guard_by_write_sandbox` 暴露给 `tests/test_write_sandbox.py`（`conftest` 不可直接 import）。"""
    return _guard_by_write_sandbox


@pytest.fixture
def paper_portfolio_isolation_fn():
    """把逐条夹具 `_isolate_paper_portfolio_state` 的原函数（生成器）暴露给 `tests/test_write_sandbox.py`，验 v0.45.577 的让位。"""
    fx = _isolate_paper_portfolio_state
    getter = getattr(fx, "_get_wrapped_function", None)       # pytest ≥ 8.4
    return getter() if getter else getattr(fx, "__wrapped__", fx)


@pytest.fixture
def data_root_session_facts(pytestconfig):
    """会话级数据根接线的事实，给 `tests/test_data_root_default.py` 自证（同上：`conftest` 不可直接 import）。

    `invocation_env`：调用 pytest 时的 ALPHA_HIVE_HOME / HOME；`session_home`：`pytest_configure` 设的会话沙箱；
    `guard_root`：真实数据根闸在 `pytest_sessionstart` 认定的根（None = 本机没有真实数据根，闸不生效）。"""
    root, _fp = pytestconfig.stash.get(_DATA_ROOT_BEFORE, (None, None))
    return {"invocation_env": dict(pytestconfig._alpha_hive_invocation_env),
            "session_home": pytestconfig._alpha_hive_session_home,
            "guard_root": root}


# ==================== 仓库根「默认拒绝」总闸（v0.45.233） ====================
#
# 本闸盯仓库根下**除代码以外的一切**，不维护清单（新产物默认就被盯住）。
# 「代码」怎么界定、为什么这么界定，全在 `tests/_root_data_guard.py` 的 docstring，这里不抄（抄一份就是快照）。
# v0.45.409（阶段 6 ⑤）：原先还有一道「点名 8 个产物」的清单式总闸（`_GUARDED_PRODUCTION_ARTIFACTS`）；
# 数据搬到 `$ALPHA_HIVE_HOME` 后它盯的仓库根路径都不存在，被本闸完全覆盖，已删；
# 对应的真缺口——测试写到**真实数据根**——由下面的 `_guard_real_data_root` 补上。
#
# 为什么「之前」的指纹取在 `pytest_sessionstart` 而不是 session fixture 的 setup：
# fixture 最早也要等**收集结束**才跑，而冻结路径那一族正是在收集期 import 时
# 落盘的（v0.45.150 实测收集结束时 `ALPHA_HIVE_HOME` 仍是 `<UNSET>`）。在 fixture
# 里取「之前」，收集期写进仓库根的东西会被算进基线、永远不红。
# 「之后」放在 session fixture 的 teardown 里比对，这样失败是一条正常的 ERROR，
# 而不是 sessionfinish 里一行容易被忽略的输出。

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _root_data_guard  # noqa: E402
import _write_sandbox  # noqa: E402
import _concurrency_hint  # noqa: E402
# v0.45.651：财报信号 scan 的运行时钟断言（夹具定义在该模块，这里 import 进来才会被 pytest 注册；用包路径 import，
# 守卫自己的测试读的是同一份模块状态）。说明见 tests/_scan_clock_guard.py。
from tests._scan_clock_guard import (  # noqa: E402,F401
    _scan_clock_guard_installed, _scan_clock_must_be_pinned, allow_real_scan_clock)

_ROOT_FP_BEFORE = pytest.StashKey[dict]()


_DATA_ROOT_BEFORE = pytest.StashKey[tuple]()
_DATA_ROOT_ALPHABOT = pytest.StashKey[tuple]()
# v0.45.455：两道 session 级总闸报红时，逐文件标出「写入时刻在跑哪条测试」要用的本轮开始时刻与时间线。
_SESSION_START_WALL = pytest.StashKey[float]()


def pytest_sessionstart(session):
    # 先记开始时刻再取指纹：取指纹期间被写的文件，mtime 也不会早于这个时刻（否则会被误标成「保留了旧 mtime」）。
    # Linux 上 mtime 可比此刻早至多一个 tick（粗粒度内核时钟），`locate` 按 `mtime_lag` 放宽（v0.45.507）
    session.config.stash[_SESSION_START_WALL] = time.time()
    _GATE_TIMELINE.clear()
    session.config.stash[_ROOT_FP_BEFORE] = _root_data_guard.fingerprint(_REPO_ROOT_FOR_GUARD)
    # 真实数据根：按**调用 pytest 时**的环境取——`pytest_configure` 已把 ALPHA_HIVE_HOME 换成会话沙箱
    # （v0.45.422），`_isolate_env` 之后还会逐条换。读 os.environ 就会把沙箱当成「真实数据根」，闸当场失明。
    invoked = session.config._alpha_hive_invocation_env
    root = _root_data_guard.real_data_root(
        invoked, invoked.get("HOME") or os.path.expanduser("~"), _REPO_ROOT_FOR_GUARD)
    session.config.stash[_DATA_ROOT_BEFORE] = (
        root, _root_data_guard.fingerprint(root, _root_data_guard.data_root_excluded_reason) if root else None)
    # v0.45.453：报红时分辨「全部是 Alpha Bot 盘中追加」要用的起点（会话开始时刻 + 近两天盘中文件的哈希）。只影响措辞。
    _fp = session.config.stash[_DATA_ROOT_BEFORE][1]
    session.config.stash[_DATA_ROOT_ALPHABOT] = (
        datetime.datetime.now(datetime.timezone.utc),
        _root_data_guard.alphabot_baseline(root, _fp, datetime.date.today()) if root else {})


#: 本轮时间线：`pytest_runtest_logreport` 拿不到 config，只能经模块级引用写（`pytest_sessionstart` 清空）。
_GATE_TIMELINE: list = []


def pytest_runtest_logreport(report):
    """逐条测试逐阶段记 (开始, 结束, nodeid, 阶段)——只给两道总闸报红时定位用（v0.45.455）。
    `report.start/stop` 是 pytest 自留的墙钟，测试 mock `time` 改不到；与文件 mtime 可直接比。"""
    _GATE_TIMELINE.append((report.start, report.stop, report.nodeid, report.when))


def _gate_timeline(config) -> tuple:
    """(本轮开始时刻 | None, 时间线)。时间线直接读模块级那份——v0.45.460：曾从 stash 取、取不到给 `[]`，
    sessionstart 没调到本 conftest 时会把**已记下**的时间线扔掉、全标成「收集期」（实际调用里几乎走不到：
    pytest 把参数下 `test*` 子目录的 conftest 也当 initial 加载）。开始时刻取不到就是 None，`locate` 会照实说。"""
    return config.stash.get(_SESSION_START_WALL, None), _GATE_TIMELINE


def pytest_report_header(config):
    """让「这台机器上真实数据根闸有没有生效」可见：不在时它什么都不保护，不能让这件事静默。"""
    root, fp = config.stash.get(_DATA_ROOT_BEFORE, (None, None))
    note = _root_data_guard.nonabsolute_env_note(getattr(config, "_alpha_hive_invocation_env", {}) or {})
    tail = f"；{note}" if note else ""                  # v0.45.488：环境值没被采用要说出来，不能静默改守别处
    sb = getattr(config, "_alpha_hive_write_sandbox", None) or {}
    if sb.get("active"):
        sb_line = (f"write sandbox: ACTIVE（内核禁写 {sb['root']}{'，继承自外层会话' if sb.get('inherited') else ''}；"
                   "数据根变化只说明、不报红，测试写入的拒绝记录才报红）")
        for k in ("stale_marker", "registry_error"):     # v0.45.577：说清楚，不静默
            if sb.get(k):
                sb_line += f"\n  ⚠️ {sb[k]}"
    else:
        sb_line = f"write sandbox: INACTIVE（{sb.get('reason') or '未尝试'}）——真实数据根闸按指纹比对判红"
    # 返回单个字符串（两行用换行连）：既有测试按子串读报告头（`'active on …' in header`）
    if root is None:
        return (f"real-data-root guard: INACTIVE（此机器上没有真实数据根；CI / 干净克隆都是这样，牙只靠合成测试证明）{tail}\n"
                + sb_line)
    return f"real-data-root guard: active on {root}（{len(fp)} 个受闸条目）{tail}\n" + sb_line


def pytest_report_collectionfinish(config, items):
    """收集结束、条数已知：把本会话登记为「N 条」，并看有没有别的大会话同时在跑（v0.45.590，只提示）。

    ⚠️ 必须在这个钩子里算，不能拆成 `pytest_collection_finish` 存 stash、这里再读：终端报告器自己的
    `pytest_collection_finish` 就是在里面调本钩子的，顺序不保证在别的 conftest 之后——读到的会是空（实测：拆开时 `-q` 下什么都不打）。"""
    return _concurrency_hint.session_lines(getattr(config, "_alpha_hive_write_sandbox", None), len(items))


@pytest.fixture(scope="session", autouse=True)
def _guard_repo_root_default_deny(request):
    """整个 session（含收集期）跑完，仓库根下的非代码内容必须一个字节都没变。

    ⚠️ 与上面那道一样是 session 级的：红在整轮末尾，pytest 把它记在收集序最后一条测试名下（与原因无关）。
    v0.45.455 起报错逐文件给出「mtime 落在哪条测试的哪个阶段」——只是时间重合，并发写入者照样会落在
    无关测试期间。要确认是谁写的：对红出来的路径 patch `builtins.open` / `os.makedirs` / `sqlite3.connect`
    记 `nodeid` 与调用栈；或二分跑测试文件。
    ⚠️ 在**生产 checkout** 里跑时，每日扫描、别的 session 往生产目录写的东西都会让它红——
    那是真实存在的「测试与生产写同一批文件」的时间窗，不是假警报（数据根迁移阶段 5 之后才会消失）。
    """
    before = request.config.stash.get(_ROOT_FP_BEFORE, None)
    late = before is None
    if late:   # sessionstart 没调到本 conftest（非 initial conftest）：退回 setup 时取，并在报错里注明
        before = _root_data_guard.fingerprint(_REPO_ROOT_FOR_GUARD)

    yield

    after = _root_data_guard.fingerprint(_REPO_ROOT_FOR_GUARD)
    changes = _root_data_guard.diff(before, after)
    if not changes:
        return
    started, timeline = _gate_timeline(request.config)
    assert not changes, (
        f"本轮期间**仓库根**的非代码内容被改动了（checkout 根目录 = {_REPO_ROOT_FOR_GUARD}）：\n"
        f"{_root_data_guard.format_diff(changes)}\n\n"
        + ("⚠️ 基线取自 session fixture setup（sessionstart 未生效），收集期写入不在本次比对范围内。\n"
           if late else "")
        + f"{_root_data_guard.timeline_section(_REPO_ROOT_FOR_GUARD, changes, after, timeline, started)}\n\n"
        "若是测试写的：被测代码的写入路径绕过了 `ALPHA_HIVE_HOME` 隔离——多半是 `Path(__file__).parent / 数据名`、"
        "cwd 相对路径，或把 `PATHS.*` 求值成了模块级常量 / 类属性 / 默认参数。"
        "改成调用时读 `PATHS.*`。\n"
        "**不要**往 `tests/_root_data_guard.py` 的豁免里加数据路径——那里只放人写的代码。")

@pytest.fixture(scope="session", autouse=True)
def _guard_real_data_root(request):
    """整个 session（含收集期）跑完，**真实数据根**里的生产数据必须一个字节都没变（v0.45.409，阶段 6 ⑤）。

    阶段 5 之后生产数据在 `$ALPHA_HIVE_HOME`（~/alpha-hive-data）。`_isolate_env` 逐条把它改成沙箱，
    但任何绕过隔离的写入（`__file__` 派生 / 模块级常量冻结 / subprocess 丢了 env / 硬编码 `~/alpha-hive-data`）
    都会写到真实数据根——生产账本、样本库、模型，**不可重取**。此前没有任何东西会为此变红。
    口径同仓库根闸：默认拒绝（除 `logs/`、`db_backups/` 与 `_` 打头的元目录外一切受闸；`_git_backup/` 例外照样受闸，
    v0.45.427——`PATHS.data_backup_repo` 让测试碰得到它，10-03 夹具就是写进了这里）、`(size, mtime_ns)`、
    `-shm` 只记大小。「之前」取在 `pytest_sessionstart`（收集期之前），理由见仓库根闸一节。

    ⚠️ 与仓库根闸同样是 session 级：红在整轮末尾、记在最后一条测试名下；逐文件写入时刻落在哪条测试期间
    见报错（v0.45.455，只是时间重合），确认法见仓库根闸。
    ⭐ **v0.45.573 起，写沙箱生效时（macOS + 本机有真实数据根）判据换了**：测试进程树被内核禁写真实数据根，
    红的是「统一日志里有对它的写拒绝」（逐条定位到测试）或「核对不了」，指纹差异只可能来自沙箱外的写入者 ⇒ 只在终端摘要说明。
    下面这段「扫描 / Alpha Bot 在写时会红」只在沙箱**未生效**时成立。见 `_guard_by_write_sandbox` 与 `tests/_write_sandbox.py`。
    ⚠️ **机器上正在跑每日扫描 / 周任务时它会红**（扫描写 `cache/`、`report_snapshots/`、`paper_portfolio_state/`…）——
    那不是假警报，是「测试与生产写同一批文件的时间窗真实存在」；平日 14:00–15:00 与周日 09:07 前后别跑全套。
    ⚠️ 没有真实数据根的机器上（CI / 干净克隆）本闸不做任何事（`pytest_report_header` 会说明）；它的牙由
    `tests/test_root_data_guard.py` 的合成数据根测试证明，不能靠「这轮绿」证明。
    """
    root, before = request.config.stash.get(_DATA_ROOT_BEFORE, (None, None))

    yield

    if root is None:
        return
    after = _root_data_guard.fingerprint(root, _root_data_guard.data_root_excluded_reason)
    changes = _root_data_guard.diff(before, after)
    sb = getattr(request.config, "_alpha_hive_write_sandbox", None) or {}
    if sb.get("active"):
        _guard_by_write_sandbox(request.config, sb, root, changes)
        return
    if not changes:
        return
    started, baseline = request.config.stash.get(_DATA_ROOT_ALPHABOT, (None, {}))
    v = _root_data_guard.explain_alphabot_appends(
        root, changes, before, baseline, started, datetime.datetime.now(datetime.timezone.utc))
    # 给 pre-push 契约闸的报告插件读（`changelog_guard._REPORT_PLUGIN`）：只有这里能逐项核实，钩子不抠输出文本
    setattr(request.config, _root_data_guard.VERDICT_CONFIG_ATTR, {
        "all_alphabot_appends": not v["unexplained"], "alphabot_files": sum(
            1 for p in v["explained"] if p.endswith(".jsonl")),
        "alphabot_records": v["records"], "unexplained": sum(len(x) for x in v["unexplained"].values())})
    started_wall, timeline = _gate_timeline(request.config)
    assert not changes, _real_data_root_message(root, changes, v) + "\n\n" + _root_data_guard.timeline_section(
        root, changes, after, timeline, started_wall)


def _guard_by_write_sandbox(config, sb: dict, root: str, changes: dict) -> None:
    """沙箱生效时的判据（v0.45.573）：测试进程树**写不进**真实数据根（内核拒），所以——
    ① 统一日志里有对它的写拒绝 ⇒ 有测试 / 被测代码试图写生产（哪怕被 `except OSError` 吞了）⇒ 红，逐条定位到测试；
    ② 查不了日志 ⇒ 红（核对失败不能当「没有」；生产数据本身有内核护着，红的是「没核对」）；
    ③ 指纹有差异但没有拒绝 ⇒ 只能是沙箱外的写入者（Alpha Bot 盘中采样 / 每日扫描 / 别的会话）⇒ 说明，不红。"""
    if not sb.get("check_log"):
        # 继承外层沙箱的内层会话：生产照样被内核护着；它期间的拒绝落在外层会话的查询窗里，由外层核对
        if changes:
            config._alpha_hive_external_writes = changes
        return
    found, err = _write_sandbox.denials(sb)
    found, foreign = _write_sandbox.attribute(sb, found)      # v0.45.577：并发会话各报各的
    _write_sandbox.mark(sb, checked=err is None)
    _write_sandbox.close(sb)
    if foreign:
        config._alpha_hive_foreign_denials = foreign
    assert err is None, (
        f"写沙箱生效中（{root} 由内核禁写，生产数据未受损），但核对「有没有测试试图写它」失败：{err}\n"
        "核对失败不能当没有。重跑；若持续失败，临时用 ALPHA_HIVE_TEST_WRITE_SANDBOX=0 退回指纹闸并查明原因。")
    if found:
        started_wall, timeline = _gate_timeline(config)
        rows = "\n".join(
            f"    {d['path']}\n      {d['op']} by {d['proc']}({d['pid']}) @ {d['ts']} → "
            f"{_root_data_guard.locate(_write_sandbox.ts_epoch(d['ts']), timeline, started_wall, lag=_write_sandbox.LOG_TS_LAG)}"
            + (f"\n      归属：{d['who']}" if d.get("who") and d["who"] != "本会话" else "")
            for d in found[:50])
        more = f"\n    …另有 {len(found) - 50} 条" if len(found) > 50 else ""
        raise AssertionError(
            f"本轮有测试（或被测代码、或它起的子进程）试图写**真实数据根**（{root}），被内核写沙箱拒绝了 {len(found)} 次——"
            "生产数据未受损，但被测代码绕过了 `ALPHA_HIVE_HOME` 隔离：\n"
            f"{rows}{more}\n\n"
            "这些拒绝只可能来自受沙箱约束的测试进程树（沙箱外的 Alpha Bot / 扫描从不被拒；另一个同时在跑的测试会话"
            "登记过的进程已剔除，归属不明的照样列出并注明），时间定位见每条末尾（会话级闸，"
            "pytest 把它记在最后一条测试头上，那条测试名与原因无关）。若当时测试没红，是被测代码吞了 PermissionError——"
            "那正是要修的：改成调用时读 `PATHS.*`。**不要**往豁免里加路径。")
    if changes:
        config._alpha_hive_external_writes = changes


def _real_data_root_message(root, changes: dict, v: dict) -> str:
    """真实数据根闸的报错文案（v0.45.453 起按 `explain_alphabot_appends` 分三种；判定不受影响，照样红）。
    v0.45.455 起调用方在其后接 `timeline_section`（逐文件写入时刻落在哪条测试期间），三种措辞都带。"""
    if v["explained"] and not v["unexplained"]:
        n_files = sum(1 for p in v["explained"] if p.endswith(".jsonl"))
        return (
            f"真实数据根（{root}）有变化，但**全部**是 Alpha Bot 盘中采样的追加——已逐项核实，不是测试写的：\n"
            f"{_root_data_guard.format_diff(changes)}\n"
            f"  {n_files} 个文件只在末尾新增了 {v['records']} 张快照：原有内容逐字节未变，快照时间都落在本轮测试期间。\n\n"
            "判定照样是红（闸只认「没有变化」，不认「看起来像合法写入」）。处置：Alpha Bot 盘中按设定间隔整批写、"
            "每批几十秒，等这一批写完重跑 / 重推即可。\n"
            "**不要** `git push --no-verify`（会连 CHANGELOG 检查一起跳过），也**不要**往 `tests/_root_data_guard.py` "
            "的豁免里加路径。\n"
            "⚠️ 收盘后、或 Alpha Bot 根本没在跑时仍出现这条，就**不是**它——按测试写穿查："
            "测试若绕过隔离、用 Alpha Bot 自己的代码往真实数据根写快照，长得和它一模一样。")
    msg = (
        f"本轮期间**真实数据根**（{root}）里的生产数据被改动了：\n"
        f"{_root_data_guard.format_diff(changes)}\n\n"
        "若是测试写的：被测代码绕过了 `ALPHA_HIVE_HOME` 隔离——多半是 `Path(__file__).parent / 数据名`、模块级常量冻结、"
        "subprocess 丢了 env，或硬编码了 `~/alpha-hive-data`。改成调用时读 `PATHS.*`。\n"
        "若这是每日扫描 / 周任务恰好在写（不是测试）：等它结束再跑。\n"
        "**不要**往 `tests/_root_data_guard.py` 的豁免里加数据路径。")
    if v["explained"]:
        msg += (f"\n\n其中 {len(v['explained'])} 项已核实是 Alpha Bot 盘中追加（{v['records']} 张快照）；"
                "**其余这些不能归因于它，问题在这里**：\n"
                f"{_root_data_guard.format_diff(v['unexplained'])}")
    if v["why"]:
        msg += "\n\nAlpha Bot 目录下没能归因的项及原因：\n" + "\n".join(
            f"    {p}：{r}" for p, r in sorted(v["why"].items()))
    return msg


# ==================== hive_logger 文件日志隔离（v0.45.239） ====================
#
# 事故：`hive_logger` 在 import 时 `logger = _setup_logger()`，裸 `RotatingFileHandler`
# 把 `baseFilename` 存死 ⇒ pytest 收集期（早于 `_isolate_env`）就冻成
# `<checkout>/logs/alpha_hive.log`，全套测试日志——含夹具造的 ERROR——写进 checkout；
# 在主 checkout 起 pytest 就混进生产日志。修在生产代码（`LogsDirRotatingFileHandler`
# 每条记录按 `PATHS` 求值），这里是三道观测：
#
#   ⓪ 会话级缺省（`pytest_configure`）：测试**之外**的日志——后台线程在 teardown 之后才落的、
#      module/session 级 fixture、atexit——也有 env 可读，指向会话临时目录。
#   ① 每条测试 setup 正面核对：handler **此刻**的落点在本条 `tmp_path` 里。
#      ⚠️ 这一道不能省：⓪ 会把一个冻住的 handler 也「接住」（冻在会话目录而非 checkout），
#      ②③ 于是恒绿。⓪ 在、生产修复被改回时，只有 ① 会红——这正是 ML 模型那道 fixture
#      说的「正面核对防线真的生效，而不是再盖一层把问题盖住」。
#   ② 每条测试 teardown 比对 checkout 真身指纹：兜住 ① 管不到的绕过——测试自己 delenv、
#      subprocess 用现造的 env（launchd 只给 PATH）跑 CLI。红在具体那条测试上。
#   ③ 会话级比对（快照在 ⓪ 的 `pytest_configure` 里取，早于收集）：兜住测试之间的写入。
#
# 被盯的是**两个文件名**而非整个 `logs/`：同目录的 `scan_timing.json` /
# `production_sync.json` 等别有写入者，不归本闸（全目录默认拒绝属数据根迁移阶段 0.3）。
# ⚠️ 在主 checkout 起 pytest 时若正好在跑每日扫描，②③ 会红——不是假警报，
#    是「测试与生产写同一文件的时间窗」真实存在；去 worktree 里跑。

_HIVE_LOG_FILES = ("alpha_hive.log", "alpha_hive_structured.jsonl")


def _hive_log_watch_paths(*roots):
    return sorted({os.path.join(str(r), "logs", n)
                   for r in roots if r for n in _HIVE_LOG_FILES})


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """写沙箱生效时，真实数据根的变化来自沙箱外——只在这里说一声，不报红（v0.45.573）。"""
    foreign = getattr(config, "_alpha_hive_foreign_denials", None)
    if foreign:          # v0.45.577
        terminalreporter.write_sep("-", "真实数据根：另一个测试会话的写拒绝（由它自己报，不在这里报红）")
        for d in foreign[:20]:
            terminalreporter.write_line(f"  {d['path']}  {d['op']} by {d['proc']}({d['pid']}) @ {d['ts']}  —— {d['who']}")
    changes = getattr(config, "_alpha_hive_external_writes", None)
    if not changes:
        return
    sb = getattr(config, "_alpha_hive_write_sandbox", {}) or {}
    terminalreporter.write_sep("-", "真实数据根：沙箱外的写入（说明，不报红）")
    terminalreporter.write_line(
        f"写沙箱生效中，本测试进程树写不进 {sb.get('root')}，以下变化来自沙箱外的写入者"
        "（Alpha Bot 盘中采样 / 每日扫描 / 别的会话）：")
    terminalreporter.write_line(_root_data_guard.format_diff(changes))


@pytest.hookimpl(trylast=True)  # 晚于 tmpdir 插件的 pytest_configure（它才建 _tmp_path_factory）
def pytest_configure(config):
    """⓪：会话级 `ALPHA_HIVE_LOGS_DIR` 缺省（逐条测试由 `_isolate_env` 覆盖）；顺带取 ③ 的「之前」快照。

    **刻意不在会话结束时还原**：atexit 钩子（`PheromoneBoard._shutdown` 失败时打 warning）
    跑在 `pytest_unconfigure` 之后，还原了它们就回落到 checkout。本进程随即退出，
    不还原没有下游；`pytest.main()` 进程内调用者之后的日志会落到这个临时目录——代价可接受。

    ③ 的「之前」必须早于收集——收集期 import 就是事故窗口，session fixture 的 setup 已经太晚。
    本钩子满足（实测次序：conftest import → 本钩子 → 收集；此刻 `hive_logger` 尚未被 import）。
    v0.45.246 前快照在模块级用 `os.getcwd()` 取：意图是调用目录，读的是「此刻的 cwd」，两者只在更早有谁
    chdir 过时不同。改用 `invocation_params.dir`（与 ② 同源），conftest 里便没有模块级读 cwd——
    `test_reads_own_checkout.py::TestConftestGuardsDoNotAnchorOnCwd` 若扩到模块级，这里不必另开豁免。
    此处不 import `hive_logger`：会改变被测的时序。
    """
    watched = _hive_log_watch_paths(_REPO_ROOT_FOR_GUARD, config.invocation_params.dir)
    config._alpha_hive_logs_before_collection = {p: _artifact_signature(p) for p in watched}
    d = config._tmp_path_factory.mktemp("hive_logs_outside_tests", numbered=False)
    os.environ["ALPHA_HIVE_LOGS_DIR"] = str(d)
    # 会话级 `ALPHA_HIVE_HOME` 沙箱（v0.45.422）：数据根缺省改成 `~/alpha-hive-data` 之后，收集期 import 就冻住的
    # PATHS 派生常量（`config.py` 的 *_CONFIG、若干 `BASE_DIR` …，清单见 test_paths_not_frozen_at_import::KNOWN）
    # 若在未设该变量时求值，会指向**本机真实生产数据**。必须早于收集，所以在这里、不在 session 夹具里设。
    # 先记下调用 pytest 时的环境：真实数据根闸（`pytest_sessionstart`）与 paper_portfolio 真身夹具
    # 要的是「生产在哪」，不是这个沙箱——拿沙箱推真身正是 v0.45.303 那种守卫盯着自己的失明。
    config._alpha_hive_invocation_env = {
        k: os.environ[k] for k in ("ALPHA_HIVE_HOME", "HOME") if k in os.environ}
    # v0.45.573：内核写沙箱——收集之前就让本进程及其子进程**写不进**真实数据根（由内核保证，不再靠事后比指纹去猜）。
    # 真实数据根与 `pytest_sessionstart` 的闸同一推导；套不上 / 金丝雀没被拒 ⇒ `active=False`，闸回到指纹比对。
    # 原因与设计见 `tests/_write_sandbox.py`。
    _invoked = config._alpha_hive_invocation_env
    config._alpha_hive_write_sandbox = _write_sandbox.apply(_root_data_guard.real_data_root(
        _invoked, _invoked.get("HOME") or os.path.expanduser("~"), _REPO_ROOT_FOR_GUARD))
    h = config._tmp_path_factory.mktemp("hive_home_outside_tests", numbered=False)
    config._alpha_hive_session_home = str(h)
    os.environ["ALPHA_HIVE_HOME"] = str(h)


def _hive_log_handler_escapes(sandbox, handlers=None):
    """**此刻**会写到 `sandbox` 之外的文件 handler：[(handler 类名, 落点)]。

    `handlers` 缺省取 `alpha_hive` logger 上的；验牙的测试传自己造的，不去改全局 logger
    （挂上去的那一瞬间，前面测试残留的后台线程就可能经它写盘）。
    落点优先问 handler 自己（`current_target()`，emit 时用的同一个解析）；没有这个方法的
    就是 import 时存死 `baseFilename` 的那一族，按 `baseFilename` 算。

    **先判绝对、再 resolve 判包含**（v0.45.246，与 `_assert_default_path_in_sandbox` 同形）：
    `Path(相对).resolve()` 按 **cwd** 补全，而 ① 跑的时候 cwd 已是本条的 `tmp_path/_cwd`
    （`_isolate_cwd_and_sys_path` 字母序排在 `_isolate_hive_logger_files` 前）⇒ 相对落点恒「在沙箱里」，
    ① 不响（实测：`_isolate_env` 把 `ALPHA_HIVE_LOGS_DIR` 改成 `"logs"`、或 `current_target` 改回
    `Path("logs") / leaf`，修前均 1 passed）。相对落点**本身就是逃逸**：`emit` 每条记录 `abspath` 一次，
    进程中途有谁 chdir，日志就跟着搬家——比 import 时 abspath 一次的裸 handler 更糟。
    故相对落点原样报出（`(类名, 相对路径)`），不补全。
    resolve 不能省：macOS `$TMPDIR` 是 `/var/…`、pytest 的 `tmp_path` 是 `/private/var/…`；
    词法包含会误报前者、漏报「字面在沙箱里、经 symlink 指出去」。
    """
    import logging
    import hive_logger  # noqa: F401 — 确保 _setup_logger() 已跑过
    if handlers is None:
        handlers = logging.getLogger("alpha_hive").handlers
    sandbox = pathlib.Path(sandbox)
    assert sandbox.is_absolute(), f"沙箱不是绝对路径：{str(sandbox)!r}——按 cwd 补全后判包含，等于判「在不在 cwd 里」"
    sandbox = sandbox.resolve()
    out = []
    for h in handlers:
        if not isinstance(h, logging.FileHandler):
            continue
        resolver = getattr(h, "current_target", None)
        raw = pathlib.Path(resolver() if callable(resolver) else h.baseFilename)
        if not raw.is_absolute():
            out.append((type(h).__name__, str(raw)))
            continue
        target = raw.resolve()
        if not target.is_relative_to(sandbox):
            out.append((type(h).__name__, str(target)))
    return out


@pytest.fixture
def hive_log_handler_escapes():
    """把 ① 的判据暴露给测试（验它有牙），理由同 `artifact_signature`。"""
    return _hive_log_handler_escapes


@pytest.fixture(autouse=True)
def _isolate_hive_logger_files(_isolate_env, tmp_path, request):
    """① setup 正面核对 + ② teardown 真身指纹。见上方分节注释。"""
    import logging
    import hive_logger

    file_handlers = [h for h in logging.getLogger("alpha_hive").handlers
                     if isinstance(h, logging.FileHandler)]
    # 空列表 ⇒ ① 恒真。生产上也等于文件日志没了，两边都该红。
    assert file_handlers, "alpha_hive logger 上一个文件 handler 都没有：① 的核对恒真，生产也没有文件日志了"
    leaves = {os.path.basename(h.baseFilename) for h in file_handlers}
    # 子集语义：生产加了/改名了日志文件而这里没跟上 ⇒ ②③ 盯的是过期清单，必须红。
    assert leaves <= set(_HIVE_LOG_FILES), (
        f"hive_logger 的文件 handler 写 {sorted(leaves)}，不在被盯清单 {_HIVE_LOG_FILES} 里——"
        "改 conftest 的 `_HIVE_LOG_FILES`，否则 ②③ 看不见新文件。")

    escapes = _hive_log_handler_escapes(tmp_path)
    assert not escapes, (
        f"hive_logger 文件日志的落点逃出了本条测试的沙箱 {tmp_path}：{escapes}\n"
        "落点若是会话临时目录 `hive_logs_outside_tests`，说明 handler 又在 import 时存死了路径"
        "（被 conftest 的会话缺省接住，所以 checkout 没被写——但逐条隔离已失效）；"
        "若是 checkout 的 `logs/`，就是事故原样复发；"
        "若是**相对路径**，落点跟着 cwd 走（测试里 cwd 在 tmp 所以看着无害，生产换个目录起进程、"
        "或中途有谁 chdir 就写到别处）——`ALPHA_HIVE_LOGS_DIR`/`ALPHA_HIVE_HOME` 或 `current_target` "
        "被改成相对的了？改法见 `hive_logger.LogsDirRotatingFileHandler`。")

    watched = _hive_log_watch_paths(_REPO_ROOT_FOR_GUARD,
                                    os.path.dirname(os.path.abspath(hive_logger.__file__)),
                                    request.config.invocation_params.dir)
    before = {p: _artifact_signature(p) for p in watched}

    yield

    for h in file_handlers:
        h.flush()
    touched = sorted(p for p in watched if _artifact_signature(p) != before[p])
    assert not touched, (
        f"本条测试写了 checkout 的真实日志：{touched}\n"
        "① 已核对 handler 落点在沙箱里，还能写到真身，说明有绕过它的路径：测试自己 delenv 了 "
        "`ALPHA_HIVE_LOGS_DIR`/`ALPHA_HIVE_HOME` 后打日志，或 subprocess 用现造的 env 跑 CLI。"
        "给那条路径补上 env，不要在这里放行。")


@pytest.fixture(scope="session", autouse=True)
def _guard_hive_log_files_session(request):
    """③：从 `pytest_configure`（早于收集）到会话结束，checkout 真实日志不许变。"""
    before = getattr(request.config, "_alpha_hive_logs_before_collection", None)
    # 取不到 / 空 ⇒ ③ 恒真（⓪ 被改名、没注册、或提前 return）。
    assert before, "③ 的收集前快照没取到：`pytest_configure` 没跑到取快照那一步，会话级比对恒真"
    yield
    import logging
    for h in logging.getLogger("alpha_hive").handlers:
        h.flush()
    touched = sorted(p for p in before if _artifact_signature(p) != before[p])
    assert not touched, (
        f"本轮 pytest 写了 checkout 的真实日志：{touched}\n"
        "逐条测试的 ② 没红 ⇒ 写入发生在测试之外（收集期 import、module/session 级 fixture、"
        "测试结束后才落的后台线程）。先查 `pytest_configure` 的会话缺省还在不在。")
