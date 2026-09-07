"""
Alpha Hive 测试 fixtures - 共享 mock 数据 + 隔离数据库
"""

import sys
import os
import pathlib
import pytest
import tempfile

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ==================== 环境隔离 ====================

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
         token（`test_slack_notifier.py` 已有 3 处这么写），后设的赢。
      ② `_check_webhook_alive → False`
         掐掉 `__init__` 里那次 `requests.head`。
         ①② 合起来让 `enabled` 恒为 False，于是 `_try_src_slack_alert` 的
         `if getattr(n, "enabled", False)` 守卫会在任何发送之前短路。
      ③ `get_session` 换成记录器 —— 掐掉三处 `get_session("slack").post`，
         并**兼作观测点**：teardown 断言没有任何测试试图发送。
         没有③的话，将来谁把 `enabled` 又弄成 True，①②会静默失效而没人知道
         （与本文件 `_isolate_paper_portfolio_state`「默认重绑 + teardown
         核对真身」同构，也是 CLAUDE.md「这个失败，下游怎么知道？」那条）。

    真要测发送的测试自己 `patch("slack_report_notifier.get_session")`
    （现成 4 处这么写），会覆盖③，本 fixture 不改它们的语义。
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

    for mod_name in ("slack_report_notifier", "slack_notifier"):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:  # pragma: no cover - 模块不可得时无需拦
            continue
        monkeypatch.setattr(mod, "get_session", _fake_get_session, raising=False)

    try:
        from slack_report_notifier import SlackReportNotifier
    except Exception:  # pragma: no cover
        pass
    else:
        monkeypatch.setattr(SlackReportNotifier, "_read_user_token",
                            lambda self: None)
        monkeypatch.setattr(SlackReportNotifier, "_check_webhook_alive",
                            staticmethod(lambda url: False))

    yield

    assert not attempts, (
        f"本条测试试图真的往 Slack 发消息：{sorted(set(attempts))}。"
        "①② 两道闸本该让 enabled 恒为 False、在发送前就短路——"
        "走到这里说明有测试把 enabled 又弄成了 True 而没有自己 patch "
        "get_session。去那条测试里补 patch，不要在这里放行。")


# ==================== weekly_optimizer 生产库隔离 ====================

@pytest.fixture(autouse=True)
def _isolate_weekly_optimizer_db(tmp_path, monkeypatch):
    """把 weekly_optimizer.PHEROMONE_DB_PATH 指向不存在的临时路径（v0.45.86）。

    该常量不走 `_isolate_env` 的 ALPHA_HIVE_DB_PATH 隔离（weekly_optimizer.py
    自己算 ALPHAHIVE_DIR，不读那个 env var）。v0.45.86 起 Track A 会用它
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


@pytest.fixture(autouse=True)
def _isolate_paper_portfolio_state(tmp_path, monkeypatch):
    """把 paper_portfolio 的四个状态文件全局重绑到 tmp，并核对真身没被动过。

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
    """
    try:
        import paper_portfolio as _pp
    except Exception:  # pragma: no cover - 模块不可得时无需隔离
        return

    real = {n: _pp.STATE_DIR / n for n in _PP_STATE_FILES}
    before = {n: _pp_state_digest(p) for n, p in real.items()}

    sandbox = tmp_path / "paper_portfolio_state"
    sandbox.mkdir(exist_ok=True)
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
    def _mock_fetch(ticker):
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


# ==================== ML 模型产物隔离（v0.45.149）====================

_ML_MODEL_FILES = ("ml_model.json", "ml_model_cache.json", "ml_model_extended.json")


def _ml_model_digest(path):
    """文件内容指纹；不存在给 'MISSING'（删除/新建也算改动，不能悄悄放过）。"""
    import hashlib
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except (OSError, FileNotFoundError):
        return "MISSING"


@pytest.fixture(autouse=True)
def _isolate_ml_model_file(tmp_path, monkeypatch):
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
    watched |= {pathlib.Path.cwd().resolve() / n for n in _ML_MODEL_FILES}
    before = {p: _ml_model_digest(p) for p in watched}

    # 防线①的正面核对：默认落盘位置必须落在 tmp 沙箱里。
    resolved = pathlib.Path(_mp.default_model_path()).resolve()
    assert resolved.is_relative_to(tmp_path.resolve()), (
        f"ML 模型默认落盘位置逃出了测试沙箱：{resolved}（沙箱应为 {tmp_path}）。"
        "多半是 save_model/load_model 的默认值又被改回相对路径，"
        "或 default_model_path() 被写成了模块级常量（import 时求值 = 冻住旧值）。")

    yield

    touched = sorted(str(p) for p in watched if _ml_model_digest(p) != before[p])
    assert not touched, (
        f"测试写到了**仓库根/cwd** 的 ML 模型文件：{touched}。"
        "默认落盘位置已经全局指向 tmp，还能改到真身说明有绕过它的写入路径"
        "（硬编码相对路径？subprocess？）——去把那条路径也接到 "
        "`ml_predictor.default_model_path()` 上，不要在这里放行。")
