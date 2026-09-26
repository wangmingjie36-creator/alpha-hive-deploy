"""
Alpha Hive 测试 fixtures - 共享 mock 数据 + 隔离数据库
"""

import sys
import os
import pathlib
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
    # `HGBModel.save_model` 的默认文件名是**相对路径**，tests/ 里约 12 处
    # `svc.train_model()` 不传 tmp 路径 ⇒ 在主 checkout 跑 pytest 会往 cwd 写
    # ml_model.json，本模块会顺手快照它。而 ml_model_history/ 是 git 跟踪 +
    # 在自动提交白名单里的 ⇒ 夹具模型会被当成生产模型提交推送。
    # ml_model_guard 自己也在 pytest 下默认关闭（两层），这里再显式关一次。
    monkeypatch.setenv("ALPHA_HIVE_MODEL_SNAPSHOT_DISABLE", "1")


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
        requests.Session.request        ← reddit_sentiment（及 Slack，另有专闸）
        curl_cffi.Session.request       ← yfinance 1.2（libcurl 在 C 层开 socket）

    本文件已有的四条「关掉某个源」的 fixture（`_block_llm_api` /
    `_block_same_day_macro` 的财政部+Finnhub / Twelve Data / `_block_slack`）
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
    """
    import cboe_vix
    monkeypatch.setattr(cboe_vix, "_download", lambda: None)


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

@pytest.fixture(autouse=True)
def _block_slack(monkeypatch):
    """禁止任何测试使用生产 Slack 凭证、出网、或真的发消息（v0.45.131）。

    事故：双层出网探针（socket + curl_cffi）实测发现
    `test_instrument_integrity.py::TestSourceHealthTracking::
    test_three_empty_responses_trigger_alert` 会**真的往 #alpha-hive
    （C0AGUUWJXJS）发一条「数据源降级」告警**——

        _record_src_failure → _try_src_slack_alert → SlackReportNotifier()
          ├ __init__ 里 requests.head(真 webhook)                  ← 出网
          └ enabled = bool(user_token)，而本机 user_token 解析得到 59 字符
             → send_risk_alert → _send_slack_message
             → get_session("slack").post(chat.postMessage)         ← 真发送

    `test_utilities.py::TestSlackWebhookEnvVar::test_report_notifier_env_var`
    是同一根因的轻症版（只走到 head）。而这类告警本就被 CLAUDE.md 的
    「Slack 通知精简规则」明令禁止发送。**在测试里 `new` 一个通知器对象
    ＝一次对外动作**——这是本仓库此前没有覆盖到的一类副作用。

    三道闸，各管一件事：

      ① `_read_user_token → None`
         测试永不使用**生产**凭证。要测 token 分支的测试自己 setattr 一个假
         token（`test_slack_notifier.py` 里就这么写），后设的赢。
      ② `_check_webhook_alive → False`
         掐掉 `__init__` 里那次 `requests.head`。
         ①② 合起来让 `enabled` 恒为 False，于是 `_try_src_slack_alert` 的
         `if getattr(n, "enabled", False)` 守卫会在任何发送之前短路。
      ③ `get_session` 换成记录器 —— 掐掉 `slack_report_notifier` 里每一处
         `get_session("slack").post`，并**兼作观测点**：teardown 断言没有任何测试试图发送。
         （v0.45.341 起只剩这一个模块：`slack_notifier.py` 已删，它的发送面随之消失；
         `pre_scan_notify` 直接 `requests.post`，由 `_offline_transport` 的库级闸兜住。）
         没有③的话，将来谁把 `enabled` 又弄成 True，①②会静默失效而没人知道
         （与本文件 `_isolate_paper_portfolio_state`「默认重绑 + teardown
         核对真身」同构，也是 CLAUDE.md「这个失败，下游怎么知道？」那条）。

    真要测发送的测试自己 `patch("slack_report_notifier.get_session")`
    （`test_slack_notifier.py` 里就这么写），会覆盖③，本 fixture 不改它们的语义。
    """
    import importlib

    attempts = []

    class _SlackSessionRecorder:
        """任何 .post/.get 都记一笔并炸掉——不允许测试真的发出去。"""

        def __getattr__(self, name):
            def _call(*args, **kwargs):
                url = args[0] if args else kwargs.get("url", "?")
                attempts.append(f"{name} {str(url)[:90]}")
                raise RuntimeError(
                    "测试试图真的调用 Slack（conftest._block_slack 拦下）。"
                    "要测发送请自行 patch('<模块>.get_session')。")
            return _call

    def _fake_get_session(*_a, **_k):
        return _SlackSessionRecorder()

    try:
        mod = importlib.import_module("slack_report_notifier")
    except Exception:  # pragma: no cover - 模块不可得时无需拦
        pass
    else:
        monkeypatch.setattr(mod, "get_session", _fake_get_session, raising=False)
        monkeypatch.setattr(mod.SlackReportNotifier, "_read_user_token",
                            lambda self: None)
        monkeypatch.setattr(mod.SlackReportNotifier, "_check_webhook_alive",
                            staticmethod(lambda url: False))

    yield

    assert not attempts, (
        f"本条测试试图真的往 Slack 发消息：{sorted(set(attempts))}。"
        "①② 两道闸本该让 enabled 恒为 False、在发送前就短路——"
        "走到这里说明有测试把 enabled 又弄成了 True 而没有自己 patch "
        "get_session。去那条测试里补 patch，不要在这里放行。")


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
def _real_paper_portfolio_state_dir():
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
    `STATE_DIR` 还是收集期真环境下的 import 期值），它必须与这里求得的一致——否则「本夹具对真身的推导」
    与「`paper_portfolio` 自己的推导」漂移了（例如有人把状态目录改名/迁走），守卫在盯过期路径。
    漂移就在这里红，而不是让守卫悄悄失明。未被收集期导入时无从比较（不假装比较过），
    该情形由 `tests/test_paper_portfolio_guard_identity.py` 在子进程里正面核对。
    """
    from hive_logger import PATHS
    real = pathlib.Path(PATHS.home) / "paper_portfolio_state"
    imported = sys.modules.get("paper_portfolio")
    if imported is not None:
        assert imported.STATE_DIR == real, (
            f"守卫对「生产状态目录」的推导与 paper_portfolio 自己的推导不一致：\n"
            f"  本夹具（PATHS.home / 'paper_portfolio_state'）= {real}\n"
            f"  paper_portfolio.STATE_DIR（收集期 import 期值）  = {imported.STATE_DIR}\n"
            "守卫盯的是过期路径 ⇒ 真写穿生产它也看不见。改 `_real_paper_portfolio_state_dir` "
            "使之与 `paper_portfolio.STATE_DIR` 的定义同步（或反之），不要在这里放行。")
    return real


@pytest.fixture(autouse=True)
def _isolate_paper_portfolio_state(tmp_path, monkeypatch, _real_paper_portfolio_state_dir):
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

    yield

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
    """`_isolate_ml_model_file` 防线①：默认落盘位置是**绝对路径**且在 tmp 沙箱里。

    先断言绝对、再判包含（v0.45.240）：`Path(相对).resolve()` 按 **cwd** 补全，而 v0.45.224 起测试期间
    cwd 就在本测试的 tmp 里 ⇒ `default_model_path()` 被改回 `"ml_model.json"` 时 resolve 出
    `tmp/_cwd/ml_model.json`，包含判定恒真、本闸不响（实测）。测试里这条相对路径只写进空目录，
    **伤的是生产**：编排器从仓库根跑，写穿的正是 v0.45.149 那次事故的模型文件。
    """
    p = pathlib.Path(raw)
    assert p.is_absolute(), (
        f"ML 模型默认落盘位置不是绝对路径：{raw!r}。测试期间 cwd 在 tmp 里所以这里看着无害，"
        "生产从仓库根跑就会写穿仓库根的模型文件 —— save_model/load_model 的默认值又被改回相对路径了？")
    resolved = p.resolve()
    assert resolved.is_relative_to(tmp_path.resolve()), (
        f"ML 模型默认落盘位置逃出了测试沙箱：{resolved}（沙箱应为 {tmp_path}）。"
        "多半是 default_model_path() 被写成了模块级常量（import 时求值 = 冻住旧值）。")


@pytest.fixture(autouse=True)
def _isolate_ml_model_file(request, tmp_path, monkeypatch):
    """核对测试没把 ML 模型写进仓库根（或 pytest 的 cwd）。

    v0.45.149。事故：`ml_predictor` 的三对 `save_model/load_model` 默认值是
    **cwd 相对路径** `"ml_model.json"`，而 `MLPredictionService.train_model()`
    在训练成功后**无参**调用 `save_model()`，`tests/` 里有 11 处
    `svc.train_model()` 不传路径 —— 于是**在主 checkout 跑一次 pytest，就用
    夹具模型覆盖了仓库根的模型文件**。2026-09-07 01:15 实际发生：
    `n=497 / acc 71.63 / oos 44.35` 被写成 `n=30 / acc 96.67 / oos None`，
    且后者对 15 组不同输入只吐 1 个值（常数函数）。

    两道防线：

      ① **路径侧**（在生产代码里，不在这个 fixture 里）：默认值改为
         `ml_predictor.default_model_path()` → `PATHS.home / MODEL_FILENAME`，
         而 `PATHS.home` 读 `ALPHA_HIVE_HOME`，上面的 `_isolate_env` 已经把它
         指向 tmp_path。**注意这道防线本来就该罩住模型文件，是那六个裸相对
         路径默认值从它底下钻了出去** —— 与限流器只覆盖 6/40 个调用点同形。
         本 fixture 因此在 setup 时**正面核对**这道防线真的生效，而不是再
         monkeypatch 一遍把问题盖住（盖住了就再也测不出它退化）。

      ② **指纹侧**：teardown 比对真身。兜住任何绕过①的写法 —— 新写的测试
         硬编码相对路径、subprocess 调 CLI、有人把默认值改回相对字符串。
         没有②的话，①将来静默失效不会有人知道（CLAUDE.md 硬检查项：
         「这个失败，下游怎么知道？」）。

    被保护的是**三个**文件，不只是出事的那个：`ml_model_cache.json` 才是生产
    真正读的那份（`alpha_hive_daily_report` / `generate_ml_report` /
    `queen_distiller` 三处都显式传它），这次侥幸没被写到，纯粹因为两个写入者
    恰好用了不同文件名。
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

    # 防线①的正面核对：默认落盘位置必须落在 tmp 沙箱里。
    _assert_default_path_in_sandbox(_mp.default_model_path(), tmp_path)

    yield

    touched = sorted(str(p) for p in watched if _ml_model_digest(p) != before[p])
    assert not touched, (
        f"测试写到了**仓库根/cwd** 的 ML 模型文件：{touched}。"
        "默认落盘位置已经全局指向 tmp，还能改到真身说明有绕过它的写入路径"
        "（硬编码相对路径？subprocess？）——去把那条路径也接到 "
        "`ml_predictor.default_model_path()` 上，不要在这里放行。")

# ==================== 生产产物总闸（v0.45.150） ====================

# ⚠️ 这里刻意用 `__file__` 而不是 `PATHS.home`：本闸要盯的正是**真实 checkout
#    根目录**，必须免受 `_isolate_env` 影响。同一写法在生产模块里是 bug，
#    在这里是需求。
_REPO_ROOT_FOR_GUARD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 只盯「生产数据」级别的产物：样本库、向量库、三本期权账本。
# 缓存目录（cache/ data_cache/ reddit_cache/ …）与 ml_model*.json 暂不入闸——
# 前者危害是脏缓存、后者归 v0.45.149，另见 CHANGELOG v0.45.150 的分级表。
_GUARDED_PRODUCTION_ARTIFACTS = (
    "pheromone.db",
    "metrics.db",
    "chroma_db",
    "vrp_state",
    "options_paper_state",
    "hedge_state",
)


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


@pytest.fixture(scope="session", autouse=True)
def _guard_production_artifacts():
    """第二道防线：整个 session 跑完，生产产物指纹必须没变。

    第一道防线是「路径调用时求值」——`PATHS.*` 全是 property，配上
    `_isolate_env` 的 `monkeypatch.setenv` 本该够了。它失效过：把 `PATHS.x`
    求值成模块级常量或类属性，值就冻在 import 那一刻，而 pytest 在**收集期**
    就 import 生产模块，那时本文件的 fixture 一次都没跑过（实测收集结束时
    `ALPHA_HIVE_HOME` 确为 `<UNSET>`）⇒ 冻成 checkout 根目录，隔离形同不存在。

    v0.45.150 之前的实测后果：跑一次全套测试，`PredictionStore.__init__` →
    `_init_table()` 会以读写模式打开**生产** `pheromone.db`（37 MB，喂 IC 闸 /
    权重优化 / 概率记分卡的样本库），执行 `CREATE TABLE IF NOT EXISTS`、进入
    WAL 模式、留下 `-wal`/`-shm`；`MemoryStore()` 还会在其上跑 `schema_migrate`。

    为什么需要这一道而不是只修路径（照 `_isolate_paper_portfolio_state` 的理由）：
    逐模块打补丁是打地鼠——本仓库已经为 `weekly_optimizer` / `feedback_loop` /
    `paper_portfolio` 各写过一个专属 fixture，每次都是**事后**补的。本闸不认
    模块、只认盘上的产物，因此对将来新增的写入路径同样有效。

    ⚠️ 它是 session 级的，红在整轮末尾，不会指出是哪条测试写的。定位办法：
    patch `sqlite3.connect` / `builtins.open` / `os.makedirs` 记 `nodeid` 与调用栈
    （v0.45.150 就是这么把 `pheromone.db` 归因到 `backtester.py:99` 的）。
    ⚠️ 若此时机器上正好在跑每日扫描，本闸也会红——那不是假警报，是提示你
    「测试与生产写同一批文件的时间窗真实存在」。
    """
    real = {name: os.path.join(_REPO_ROOT_FOR_GUARD, name)
            for name in _GUARDED_PRODUCTION_ARTIFACTS}
    before = {n: _artifact_signature(p) for n, p in real.items()}

    yield

    touched = sorted(n for n, p in real.items()
                     if _artifact_signature(p) != before[n])
    assert not touched, (
        f"测试写到了**生产**产物：{touched}\n"
        f"（checkout 根目录 = {_REPO_ROOT_FOR_GUARD}）\n\n"
        "十有八九是某处把 `PATHS.*` 派生的路径求值成了模块级常量、类属性，"
        "或者写成了默认参数 `def f(db_path=DB_PATH)`——三者都冻在 import 期，"
        "而 pytest 收集期 import ⇒ `_isolate_env` 的 setenv 追不上它。\n"
        "改法：改成 property / 函数（调用时求值）；默认参数写 `= None` "
        "再在函数体里解析。结构守卫见 "
        "`tests/test_paths_not_frozen_at_import.py::TestSpeciesDoesNotSpread`。")


# ==================== 仓库根「默认拒绝」总闸（v0.45.233） ====================
#
# 上面那道只盯 6 个点名的产物；本闸盯仓库根下**除代码以外的一切**，不维护清单。
# 两道都留：上面那道是纵深防御，且 `_GUARDED_PRODUCTION_ARTIFACTS` 必须是本闸
# 覆盖面的子集——`tests/test_root_data_guard.py` 断言这一点，谁把其中一项划进
# 「代码」谁红。「代码」怎么界定、为什么这么界定，全在 `tests/_root_data_guard.py`
# 的 docstring，这里不抄（抄一份就是快照）。
#
# 为什么「之前」的指纹取在 `pytest_sessionstart` 而不是 session fixture 的 setup：
# fixture 最早也要等**收集结束**才跑，而冻结路径那一族正是在收集期 import 时
# 落盘的（v0.45.150 实测收集结束时 `ALPHA_HIVE_HOME` 仍是 `<UNSET>`）。在 fixture
# 里取「之前」，收集期写进仓库根的东西会被算进基线、永远不红。
# 「之后」放在 session fixture 的 teardown 里比对，这样失败是一条正常的 ERROR，
# 而不是 sessionfinish 里一行容易被忽略的输出。

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _root_data_guard  # noqa: E402

_ROOT_FP_BEFORE = pytest.StashKey[dict]()


def pytest_sessionstart(session):
    session.config.stash[_ROOT_FP_BEFORE] = _root_data_guard.fingerprint(_REPO_ROOT_FOR_GUARD)


@pytest.fixture(scope="session", autouse=True)
def _guard_repo_root_default_deny(request):
    """整个 session（含收集期）跑完，仓库根下的非代码内容必须一个字节都没变。

    ⚠️ 与上面那道一样是 session 级的：红在整轮末尾，不指出是哪条测试写的。
    定位：对红出来的路径 patch `builtins.open` / `os.makedirs` / `sqlite3.connect`
    记 `nodeid` 与调用栈；或二分跑测试文件。
    ⚠️ 在**生产 checkout** 里跑时，每日扫描、别的 session 往生产目录写的东西都会让它红——
    那是真实存在的「测试与生产写同一批文件」的时间窗，不是假警报（数据根迁移阶段 5 之后才会消失）。
    """
    before = request.config.stash.get(_ROOT_FP_BEFORE, None)
    late = before is None
    if late:   # sessionstart 没调到本 conftest（非 initial conftest）：退回 setup 时取，并在报错里注明
        before = _root_data_guard.fingerprint(_REPO_ROOT_FOR_GUARD)

    yield

    changes = _root_data_guard.diff(before, _root_data_guard.fingerprint(_REPO_ROOT_FOR_GUARD))
    assert not changes, (
        f"测试往**仓库根**写了非代码内容（checkout 根目录 = {_REPO_ROOT_FOR_GUARD}）：\n"
        f"{_root_data_guard.format_diff(changes)}\n\n"
        + ("⚠️ 基线取自 session fixture setup（sessionstart 未生效），收集期写入不在本次比对范围内。\n"
           if late else "")
        + "被测代码的写入路径绕过了 `ALPHA_HIVE_HOME` 隔离：多半是 `Path(__file__).parent / 数据名`、"
        "cwd 相对路径，或把 `PATHS.*` 求值成了模块级常量 / 类属性 / 默认参数。"
        "改成调用时读 `PATHS.*`。\n"
        "**不要**往 `tests/_root_data_guard.py` 的豁免里加数据路径——那里只放人写的代码。")

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
