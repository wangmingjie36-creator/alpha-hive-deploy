"""10-12 整日重放（v0.45.484）。在 10-12 生产代码 0993efde 的检出里、以沙箱数据根真跑 `run_swarm_scan`。

用法：python3 replay_1012.py <control|corrected> <code_dir> <sandbox> <out_dir> <rec_dir> <record|replay|off> [tickers,...]

- 钟：freezegun 钉在 2026-10-12 21:11:56Z（= 生产「蜂群协作启动」14:11:56 PDT），tick=True。
- control：复现生产当时的故障——直连（清代理变量）、所有 TLS 握手抛 SSLEOFError（生产原文同款）、
  yfinance 一律 YFRateLimitError（生产直连 Yahoo 得 429）、CodeExecutor 子进程代理指死端口。
  判据：final_score / direction / dimension_weights 与生产存档 30/30 一致，否则重放本身有偏差。
- corrected：走代理真取数，期权链/宏观走 10-12 云端快照（snapshot_mode），ALPHA_HIVE_TARGET_DATE=2026-10-12；
  在 10-13 开盘前跑，使只有「最新」口径的源（yfinance 日线 / .info / 期权成交量 / CBOE 报价）仍是 10-12 收盘态。
  新闻：Massive 请求加 published_utc.lte=扫描时刻；SEC submissions / EDGAR RSS 剔除扫描时刻之后的申报。
  全部 HTTP（requests / urllib / yfinance）落盘录制，replay 模式可离线重放同一组输入。
- 只写沙箱与 out_dir；不调 save_report / deploy_and_notify；LLM 关闭；Slack 域名一律拒绝。
"""
import hashlib
import io
import json
import os
import pickle
import sys
import threading
import time as _time

MODE, CODE, SANDBOX, OUT, REC, REC_MODE = sys.argv[1:7]
ONLY = sys.argv[7].split(",") if len(sys.argv) > 7 and sys.argv[7] else None
assert MODE in ("control", "corrected") and REC_MODE in ("record", "replay", "off", "hybrid", "union")
# hybrid：先查 REC_PRIOR（上一轮录制）里同键同序号且**成功**的响应，有就复用；没有（或上一轮这次是失败）才真取。新录制写 REC。
REC_PRIORS = [d for d in (os.environ.get("REC_PRIOR") or "").split(":") if d]
# union：只用 REC_PRIOR 里的成功响应（按目录顺序取第一个），找不到就按取数失败处理——**不出网**，开盘后也可安全运行。
assert REC_MODE not in ("hybrid", "union") or (REC_PRIORS and all(os.path.isdir(d) for d in REC_PRIORS))
PYLIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pylib")
SCAN_UTC = "2026-10-12 21:11:56"
CUTOFF_ISO_Z = "2026-10-12T21:11:56Z"
TICKERS = ("NVDA TSLA MSFT QCOM VKTX META BILI AMZN RKLB CRCL CVX VZ JNJ XOM COST BRK-B AMC ABBV T DELL "
           "DE CRM MU WMT TMO TMUS ENPH NFLX NEE SNOW").split()

os.makedirs(OUT, exist_ok=True)
os.makedirs(REC, exist_ok=True)
os.environ["ALPHA_HIVE_HOME"] = SANDBOX
os.environ["ALPHA_HIVE_SANDBOX_DIR"] = os.path.join(SANDBOX, "_ce")
os.environ["ALPHA_HIVE_MODEL_SNAPSHOT_DISABLE"] = "1"
os.environ["ANONYMIZED_TELEMETRY"] = "False"
for k in ("ALPHA_HIVE_CACHE_DIR", "ALPHA_HIVE_DB_PATH", "ALPHA_HIVE_TARGET_DATE"):
    os.environ.pop(k, None)
if MODE == "control":
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
else:
    os.environ["ALPHA_HIVE_TARGET_DATE"] = "2026-10-12"
    assert os.environ.get("HTTPS_PROXY"), "corrected 需要代理（生产正常日走代理）"

sys.path.insert(0, CODE)
sys.path.append(PYLIB)
os.chdir(CODE)          # 生产 cwd = 代码克隆

# C 扩展库必须在 freezegun 启动前载入（否则 pandas tslibs 的 datetime 结构体尺寸校验失败 → 段错误）
import numpy  # noqa: E402,F401
import pandas  # noqa: E402,F401
import pytz  # noqa: E402,F401
import yfinance  # noqa: E402,F401
import requests  # noqa: E402,F401
import sklearn.ensemble  # noqa: E402,F401
try:
    import chromadb  # noqa: E402,F401
except Exception:  # noqa: BLE001
    pass
try:
    import matplotlib  # noqa: E402,F401
    matplotlib.use("Agg")
    import matplotlib.pyplot  # noqa: E402,F401
except Exception:  # noqa: BLE001
    pass
import freezegun  # noqa: E402
# ⚠️ 本地钟：freezegun 的「无 tz 的 datetime.now()」= 冻结时刻 + tz_offset（当 UTC 用），而生产代码里的 naive now()
# 是**本地 PDT**（14:11）。缺省 tz_offset=0 ⇒ naive now 成了 21:11（v0.45.484 首版 union3 即如此：新闻时效权重拿
# naive UTC 的发布时间比 naive now，7 小时差改了权重截断）；改 tz_offset=-7 又会把 now(tz) 一并错移 7 小时（实测
# now(ET)=10:11）。⇒ tz_offset 保持 0，只把 naive now() 改成「冻结的 UTC 换算到本机时区」。
_FZ = freezegun.freeze_time(SCAN_UTC, tick=True)
_FZ.start()
import datetime as _dtm  # noqa: E402
from freezegun.api import FakeDatetime as _FakeDT  # noqa: E402
_orig_fake_now = _FakeDT.now.__func__


def _fake_now(cls, tz=None):
    utc = _orig_fake_now(cls, _dtm.timezone.utc)
    return utc.astimezone().replace(tzinfo=None) if tz is None else utc.astimezone(tz)


_FakeDT.now = classmethod(_fake_now)

# ─────────────────────────────────────────────── 网络层：control 故障复现 / corrected 录制
import ssl  # noqa: E402
import requests.adapters  # noqa: E402
import urllib.request  # noqa: E402
import urllib.error  # noqa: E402
import yfinance.data as _yfd  # noqa: E402
from yfinance.exceptions import YFRateLimitError  # noqa: E402

_LOCK = threading.Lock()
_COUNTS = {}
_STATS = {"requests": 0, "urllib": 0, "yfinance": 0, "blocked_slack": 0, "replay_miss": 0,
          "massive_lte": 0, "sec_filtered": 0, "rss_filtered": 0}


def _key(kind, method, url, body):
    h = hashlib.sha256(f"{kind}|{method}|{url}|".encode() + (body or b"")).hexdigest()[:24]
    return h


def _rec_path(key, n):
    return os.path.join(REC, f"{key}_{n}.pkl")


def _next_n(key):
    with _LOCK:
        n = _COUNTS.get(key, 0)
        _COUNTS[key] = n + 1
        return n


def _save(key, rec):
    n = _next_n(key)
    with open(_rec_path(key, n), "wb") as f:
        pickle.dump(rec, f)
    with _LOCK:
        with open(os.path.join(REC, "_index.jsonl"), "a") as f:
            f.write(json.dumps({"key": key, "n": n, "kind": rec["kind"], "url": rec["url"][:300],
                                "status": rec.get("status"), "exc": rec.get("exc"),
                                "t_real": _time.monotonic()}) + "\n")


def _load(key):
    n = _next_n(key)
    p = _rec_path(key, n)
    if not os.path.exists(p):
        # 同一请求被调用次数多于录制时：返回最后一次
        k = n - 1
        while k >= 0 and not os.path.exists(_rec_path(key, k)):
            k -= 1
        if k < 0:
            _STATS["replay_miss"] += 1
            return None
        p = _rec_path(key, k)
    with open(p, "rb") as f:
        return pickle.load(f)


_PCOUNTS = {}


def _prior(key):
    """hybrid：上一轮录制里同键第 n 次（n 按本进程调用序）的成功响应；失败 / 缺 ⇒ None。"""
    with _LOCK:
        n = _PCOUNTS.get(key, 0)
        _PCOUNTS[key] = n + 1
    for d in REC_PRIORS:
        for nn in (n, 0):     # 同序号优先；该序号失败则退到第 0 次（同一请求的内容相同）
            p = os.path.join(d, f"{key}_{nn}.pkl")
            if not os.path.exists(p):
                continue
            with open(p, "rb") as f:
                rec = pickle.load(f)
            if rec.get("exc") or rec.get("http_error"):
                continue
            st = rec.get("status")
            if st is None or not (200 <= int(st) < 300):
                continue
            _STATS["hybrid_reused"] = _STATS.get("hybrid_reused", 0) + 1
            return rec
    return None


def _is_slack(url):
    return "slack.com" in url


def _ssl_eof(*a, **k):
    raise ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:992)")


# ---- requests
_orig_send = requests.adapters.HTTPAdapter.send
MASSIVE_NEWS = "api.massive.com/v2/reference/news"


def _filter_sec_submissions(content):
    """data.sec.gov submissions：剔除扫描时刻之后的申报（acceptanceDateTime 字面值按 ET 读，取更严的一侧）。"""
    try:
        d = json.loads(content)
        rec = d.get("filings", {}).get("recent", {})
        fd, acc = rec.get("filingDate", []), rec.get("acceptanceDateTime", [])
        keep = [i for i in range(len(fd))
                if fd[i] < "2026-10-12" or (fd[i] == "2026-10-12" and (acc[i] if i < len(acc) else "")[:19] <= "2026-10-12T17:11:56")]
        dropped = len(fd) - len(keep)
        if dropped:
            for k, v in list(rec.items()):
                if isinstance(v, list) and len(v) == len(fd):
                    rec[k] = [v[i] for i in keep]
            _STATS["sec_filtered"] += dropped
        return json.dumps(d).encode()
    except Exception:
        return content


def _send(self, request, **kw):
    url = request.url
    if _is_slack(url):
        _STATS["blocked_slack"] += 1
        raise requests.exceptions.ConnectionError("replay: slack blocked")
    if MODE == "corrected" and MASSIVE_NEWS in url and "published_utc.lte" not in url:
        request.url = url = url + ("&" if "?" in url else "?") + "published_utc.lte=" + CUTOFF_ISO_Z
        _STATS["massive_lte"] += 1
    _STATS["requests"] += 1
    body = request.body if isinstance(request.body, bytes) else (request.body or "").encode()
    key = _key("requests", request.method, url, body)
    _hy = _prior(key) if REC_MODE in ("hybrid", "union") else None
    if _hy is not None:
        _save(key, _hy)
    if REC_MODE == "union" and _hy is None:
        _STATS["union_miss"] = _STATS.get("union_miss", 0) + 1
        _save(key, {"kind": "requests", "url": url, "exc": "union: no successful recording"})
        raise requests.exceptions.ConnectionError(f"union miss: {url[:120]}")
    if REC_MODE == "replay" or _hy is not None:
        rec = _hy if _hy is not None else _load(key)
        if rec is None:
            raise requests.exceptions.ConnectionError(f"replay miss: {url[:120]}")
        if rec.get("exc"):
            raise requests.exceptions.ConnectionError(f"replayed: {rec['exc']}")
        r = requests.models.Response()
        r.status_code, r._content, r.url, r.encoding, r.reason = rec["status"], rec["content"], rec["url"], rec.get("encoding"), rec.get("reason")
        r.headers = requests.structures.CaseInsensitiveDict(rec["headers"])
        r.request = request
        import datetime as _dt
        r.elapsed = _dt.timedelta(0)
    else:
        try:
            r = _orig_send(self, request, **kw)
            _ = r.content
        except Exception as e:
            if REC_MODE in ("record", "hybrid"):
                _save(key, {"kind": "requests", "url": url, "exc": f"{type(e).__name__}: {e}"[:500]})
            raise
        if REC_MODE in ("record", "hybrid"):
            _save(key, {"kind": "requests", "url": url, "status": r.status_code, "content": r.content,
                        "headers": dict(r.headers), "encoding": r.encoding, "reason": r.reason})
    if "data.sec.gov/submissions/" in url and MODE == "corrected" and r.status_code == 200:
        r._content = _filter_sec_submissions(r._content)
    return r


requests.adapters.HTTPAdapter.send = _send

# ---- urllib
_orig_open = urllib.request.OpenerDirector.open


class _FakeResp(io.BytesIO):
    def __init__(self, data, status, headers, url, reason=""):
        super().__init__(data)
        self.status = self.code = status
        self.headers = self.msg = headers
        self.url = url
        self.reason = reason

    def getcode(self):
        return self.status

    def info(self):
        return self.headers

    def geturl(self):
        return self.url


def _open(self, fullurl, data=None, timeout=None, **kw):
    url = fullurl if isinstance(fullurl, str) else fullurl.full_url
    method = "GET" if data is None else "POST"
    if _is_slack(url):
        _STATS["blocked_slack"] += 1
        raise urllib.error.URLError("replay: slack blocked")
    _STATS["urllib"] += 1
    key = _key("urllib", method, url, data if isinstance(data, bytes) else None)
    _hy = _prior(key) if REC_MODE in ("hybrid", "union") else None
    if _hy is not None:
        _save(key, _hy)
        return _FakeResp(_hy["content"], _hy["status"], _hy["headers"], url, _hy.get("reason", ""))
    if REC_MODE == "union":
        _STATS["union_miss"] = _STATS.get("union_miss", 0) + 1
        _save(key, {"kind": "urllib", "url": url, "exc": "union: no successful recording"})
        raise urllib.error.URLError(f"union miss: {url[:120]}")
    if REC_MODE == "replay":
        rec = _load(key)
        if rec is None:
            raise urllib.error.URLError(f"replay miss: {url[:120]}")
        if rec.get("http_error"):
            raise urllib.error.HTTPError(url, rec["status"], rec.get("reason", ""), rec["headers"], io.BytesIO(rec["content"]))
        if rec.get("exc"):
            raise urllib.error.URLError(f"replayed: {rec['exc']}")
        return _FakeResp(rec["content"], rec["status"], rec["headers"], url, rec.get("reason", ""))
    try:
        resp = _orig_open(self, fullurl, data, timeout if timeout is not None else 60, **kw)
    except urllib.error.HTTPError as e:
        if REC_MODE in ("record", "hybrid"):
            try:
                body = e.read()
            except Exception:
                body = b""
            _save(key, {"kind": "urllib", "url": url, "http_error": True, "status": e.code, "reason": str(e.reason),
                        "headers": e.headers, "content": body})
            raise urllib.error.HTTPError(url, e.code, e.reason, e.headers, io.BytesIO(body))
        raise
    except Exception as e:
        if REC_MODE in ("record", "hybrid"):
            _save(key, {"kind": "urllib", "url": url, "exc": f"{type(e).__name__}: {e}"[:500]})
        raise
    if REC_MODE not in ("record", "hybrid"):
        return resp
    content = resp.read()
    status, headers, reason = getattr(resp, "status", 200), resp.headers, getattr(resp, "reason", "")
    resp.close()
    _save(key, {"kind": "urllib", "url": url, "status": status, "headers": headers, "content": content, "reason": reason})
    return _FakeResp(content, status, headers, url, reason)


urllib.request.OpenerDirector.open = _open

# ---- yfinance（curl_cffi，不走 python ssl）
_orig_mk = _yfd.YfData._make_request
_orig_cc = _yfd.YfData._get_cookie_and_crumb


class _FakeYfResp:
    def __init__(self, rec):
        self.status_code, self.content, self.headers, self.url = rec["status"], rec["content"], rec["headers"], rec["url"]
        self.ok = 200 <= self.status_code < 400
        self.reason = ""

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")

    def json(self, **kw):
        return json.loads(self.content)

    def raise_for_status(self):
        if not self.ok:
            raise requests.exceptions.HTTPError(f"{self.status_code}")


def _yf_mk(self, url, request_method, body=None, params=None, timeout=30, data=None):
    if MODE == "control":
        raise YFRateLimitError()
    _STATS["yfinance"] += 1
    pkey = json.dumps(params or {}, sort_keys=True, default=str)
    pkey = "&".join(p for p in pkey.split(",") if '"crumb"' not in p)   # crumb 每次不同，不进键
    key = _key("yfinance", str(request_method.__name__ if callable(request_method) else request_method), url + "|" + pkey,
               json.dumps(body, sort_keys=True, default=str).encode() if body else None)
    _hy = _prior(key) if REC_MODE in ("hybrid", "union") else None
    if _hy is not None:
        _save(key, _hy)
        return _FakeYfResp(_hy)
    if REC_MODE == "union":
        _STATS["union_miss"] = _STATS.get("union_miss", 0) + 1
        _save(key, {"kind": "yfinance", "url": url, "exc": "union: no successful recording"})
        # ⚠️ 不能抛 YFRateLimitError：yf_gate 会因此进入 180s 全局冷却，后面所有 yfinance 调用被闸掉
        # （首版 union 因此 iv_rank 8/30、unusual_flow 6/30）。按连接失败处理——录制轮里这些请求本来就是超时 / TLS 失败。
        raise requests.exceptions.ConnectionError(f"union miss (curl: (28) timeout in recording): {url[:120]}")
    if REC_MODE == "replay":
        rec = _load(key)
        if rec is None or rec.get("exc"):
            raise requests.exceptions.ConnectionError(f"replayed failure: {(rec or {}).get('exc')}")
        return _FakeYfResp(rec)
    try:
        for _attempt in range(4):
            try:
                r = _orig_mk(self, url, request_method, body=body, params=params, timeout=timeout, data=data)
                break
            except Exception as _e_t:  # noqa: BLE001
                if _attempt == 3 or not any(w in str(_e_t) for w in ("Timeout", "SSL", "Connection", "curl: (")):
                    raise
                _STATS["yf_transient_retry"] = _STATS.get("yf_transient_retry", 0) + 1
                _time.sleep(2 + 2 * _attempt)
    except Exception as e:
        if REC_MODE in ("record", "hybrid"):
            _save(key, {"kind": "yfinance", "url": url, "exc": f"{type(e).__name__}: {e}"[:500]})
        raise
    if REC_MODE in ("record", "hybrid"):
        _save(key, {"kind": "yfinance", "url": url, "status": r.status_code, "content": r.content,
                    "headers": dict(r.headers)})
    return r


def _yf_cc(self, *a, **k):
    if MODE == "control":
        raise YFRateLimitError()
    if REC_MODE in ("replay", "union"):
        return ("cookie", "crumb", "basic")
    return _orig_cc(self, *a, **k)


_yfd.YfData._make_request = _yf_mk
_yfd.YfData._get_cookie_and_crumb = _yf_cc

if MODE == "control":
    ssl.SSLContext.wrap_socket = _ssl_eof

# ─────────────────────────────────────────────── 仓库模块
import logging  # noqa: E402
import yf_gate  # noqa: E402
yf_gate.install()
import llm_service  # noqa: E402
llm_service.disable()
import code_version  # noqa: E402
code_version.log_startup = lambda *a, **k: {}
import outcomes_fetcher  # noqa: E402
outcomes_fetcher.OutcomesFetcher.run = lambda self, *a, **k: {"skipped": "replay"}
import code_executor  # noqa: E402
if MODE == "corrected":
    _orig_exec = code_executor.CodeExecutor.execute_python

    def _exec_py(self, code, return_output=True):
        key = _key("codeexec", "PY", hashlib.sha256(code.encode()).hexdigest(), None)
        if REC_MODE == "replay":
            rec = _load(key)
            return rec["result"] if rec else {"success": False, "stdout": "", "stderr": "replay miss", "return_value": None,
                                              "execution_time": 0, "exit_code": -1, "error": "replay miss"}
        if REC_MODE in ("hybrid", "union"):
            for d in REC_PRIORS:
                p = os.path.join(d, f"{key}_0.pkl")
                if os.path.exists(p):
                    with open(p, "rb") as f:
                        rec = pickle.load(f)
                    if rec["result"].get("success"):
                        _STATS["codeexec_reused"] = _STATS.get("codeexec_reused", 0) + 1
                        _save(key, rec)
                        return rec["result"]
        if REC_MODE == "union":
            res = {"success": False, "stdout": "", "stderr": "union: no successful recording", "return_value": None,
                   "execution_time": 0, "exit_code": -1, "error": "union: no successful recording"}
            _save(key, {"kind": "codeexec", "url": "codeexec", "status": 599, "result": res})
            _STATS["union_miss"] = _STATS.get("union_miss", 0) + 1
            return res
        res = None
        for _attempt in range(4):
            res = _orig_exec(self, code, return_output)
            if res.get("success"):
                break
            _STATS["codeexec_retry"] = _STATS.get("codeexec_retry", 0) + 1
            _time.sleep(float(os.environ.get("CE_RETRY_SLEEP", "3")) * (_attempt + 1))
        if REC_MODE in ("record", "hybrid"):
            _save(key, {"kind": "codeexec", "url": "codeexec", "status": 200 if res.get("success") else 599, "result": res})
        return res
    code_executor.CodeExecutor.execute_python = _exec_py
if MODE == "control":
    import subprocess as _sp
    _orig_popen = _sp.Popen

    class _DeadProxyPopen(_orig_popen):
        def __init__(self, *a, env=None, **k):
            env = dict(env or os.environ)
            for kk in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
                env[kk] = "http://127.0.0.1:1"
            super().__init__(*a, env=env, **k)
    code_executor.subprocess.Popen = _DeadProxyPopen
if MODE == "corrected":
    import edgar_rss  # noqa: E402
    from datetime import datetime as _DT, timezone as _TZ
    _orig_parse = edgar_rss.EdgarRSSClient._parse_atom
    _CUT = _DT(2026, 10, 12, 21, 11, 56, tzinfo=_TZ.utc)

    def _parse_atom(self, xml_text):
        out = []
        for e in _orig_parse(self, xml_text):
            try:
                ts = _DT.fromisoformat(e.get("updated_ts", "").replace("Z", "+00:00"))
                if ts.tzinfo is None or ts > _CUT:
                    _STATS["rss_filtered"] += 1
                    continue
            except ValueError:
                _STATS["rss_filtered"] += 1
                continue
            out.append(e)
        return out
    edgar_rss.EdgarRSSClient._parse_atom = _parse_atom

if REC_MODE in ("union", "replay"):
    # 离线模式不真出网：Massive 的 5 次/分钟限流（acquire 超时 40s ⇒ 退回 Yahoo）在离线快放下会被假性打满，
    # 首版 union 因此 12/30 只退回 fallback（录制轮 30/30 都是 massive）。离线时限流无意义，放行。
    import newsapi_client  # noqa: E402
    newsapi_client._massive_limiter.acquire = lambda timeout=None: True
from alpha_hive_daily_report import AlphaHiveDailyReporter  # noqa: E402
import contextlib  # noqa: E402

t0 = _time.monotonic()
reporter = AlphaHiveDailyReporter()
assert reporter.date_str == "2026-10-12", reporter.date_str
tickers = ONLY or TICKERS
if MODE == "corrected":
    import cloud_snapshot_loader as csl
    ctx = csl.snapshot_mode("2026-10-12")
else:
    ctx = contextlib.nullcontext()
err = None
report = None
try:
    with ctx:
        report = reporter.run_swarm_scan(focus_tickers=tickers)
except BaseException as e:  # noqa: BLE001
    import traceback
    err = traceback.format_exc()
    print(err, file=sys.stderr)

res = {"mode": MODE, "rec_mode": REC_MODE, "elapsed_s": round(_time.monotonic() - t0, 1), "stats": _STATS, "error": err,
       "tickers": tickers}
try:
    import data_pipeline
    res["history_fill_stats"] = data_pipeline.history_fill_stats()
except Exception as e:  # noqa: BLE001
    res["history_fill_stats"] = repr(e)
sr = os.path.join(SANDBOX, ".swarm_results_2026-10-12.json")
if os.path.exists(sr):
    import shutil
    shutil.copy(sr, os.path.join(OUT, f"swarm_results_{MODE}.json"))
if report is not None:
    with open(os.path.join(OUT, f"report_{MODE}.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, default=str)
with open(os.path.join(OUT, f"run_{MODE}.json"), "w") as f:
    json.dump(res, f, ensure_ascii=False, indent=1, default=str)
print("REPLAY_DONE", json.dumps({k: res[k] for k in ("mode", "elapsed_s", "stats")}, default=str), "error" if err else "ok", flush=True)
logging.shutdown()
os._exit(0 if not err else 3)
