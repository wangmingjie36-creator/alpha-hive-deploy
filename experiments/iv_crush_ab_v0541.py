#!/usr/bin/env python3
"""v0.45.541：ChronosBee 去掉 IV-crush 前后的 A/B（录制 → 离线重放，逐位比对）。

为什么只跑 ChronosBee：本版只改了 `swarm_agents/chronos_bee.py`（扫描侧）。ChronosBee 的产出（AgentResult + 它发到信息素板上的条目）
在新旧两份代码里逐位相同 ⇒ 下游（蜂后、档案、快照、网站）收到的输入逐位相同。10-12 那份整日录制已经不在了，
重录整日要一次完整的实盘扫描；这里只录 ChronosBee 需要的请求，网络层沿用 `replay_1012_full_day_v0484.py` 的录制 / 重放实现。

用法（三步，同一个冻结时刻）：
    A 录制：  python3 iv_crush_ab_v0541.py record <codeA> <sandboxA0> <rec> <outA0.json> "2026-10-19 10:30:00"
    A 重放：  python3 iv_crush_ab_v0541.py replay <codeA> <sandboxA1> <rec> <outA1.json> "2026-10-19 10:30:00"
    B 重放：  python3 iv_crush_ab_v0541.py replay <codeB> <sandboxB>  <rec> <outB.json>  "2026-10-19 10:30:00"
然后 `python3 iv_crush_ab_v0541.py compare outA0.json outA1.json outB.json`。

⚠️ **两次重放要用同一个 `PYTHONHASHSEED`**（如都设 0）。ChronosBee 发板的 `details.catalyst_types` 是 `list(set)`，
字符串哈希按进程随机 ⇒ 同一份代码两个进程的顺序有一半概率不同（v0.45.541 实测：旧代码 seed 0 / 1 两次重放就互相翻转；
不固定 seed 时 B 与 A 恰好翻转，判成 FAIL）。该字段零读者，是既有的进程级随机，与本版无关。

判据：
- A 重放 vs A 录制（对照）：说明重放本身是确定的；两者不同的字段记为「噪声」（时间戳之类），不拿来下结论。
- B vs A 重放：除「允许的差异」（`details.iv_crush` / `details.iv_crush_summary` / 财报催化剂上的
  `iv_crush_avg_move` / `implied_move` / `beat_rate`）外必须逐位相同；score / direction / confidence / dimension /
  data_quality / discovery、板上条目一律不许有任何差异（噪声字段也不许——噪声在这几个字段上本身就该是 0）。
- B 只能少发请求、不能多发：B 在录制里找不到的请求必须 ⊆ A 重放找不到的（URL 带当前时刻的请求两边同样找不到）。

只写沙箱与 out；LLM 关闭；Slack 一律拒绝；回放模式不出网（缺录制即按连接失败）。
"""
import hashlib
import io
import json
import os
import pickle
import sys
import threading
import time as _time

ALLOWED_DETAIL_KEYS = ("iv_crush", "iv_crush_summary")
ALLOWED_CATALYST_KEYS = ("iv_crush_avg_move", "implied_move", "beat_rate")
BOARD_TIME_FIELDS = ("timestamp", "pheromone_strength")
_TIME_LEAF = __import__("re").compile(r"(_at|timestamp|_time)$")
STRICT_FIELDS = ("score", "direction", "confidence", "dimension", "data_quality", "discovery", "source", "error")


# ═════════════════════════════════════════════════════════════════ compare（不导入任何仓库代码）
def _strip_allowed(r):
    r = json.loads(json.dumps(r))
    det = r.get("details") or {}
    for k in ALLOWED_DETAIL_KEYS:
        det.pop(k, None)
    for c in det.get("catalysts") or []:
        if isinstance(c, dict):
            for k in ALLOWED_CATALYST_KEYS:
                c.pop(k, None)
    return r


def _diff_paths(a, b, path=""):
    if type(a) is not type(b):
        return [path or "<root>"]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}.{k}")
            else:
                out += _diff_paths(a[k], b[k], f"{path}.{k}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}[len]"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += _diff_paths(x, y, f"{path}[{i}]")
        return out
    return [] if a == b else [path]


def compare(p_a0, p_a1, p_b):
    a0, a1, b = (json.load(open(p)) for p in (p_a0, p_a1, p_b))
    tickers = list(a1["results"])
    assert set(tickers) == set(a0["results"]) == set(b["results"]), "三份结果的标的集合不一致"
    noise, bad, allowed_hits, strict_noise, tolerated = {}, {}, 0, [], {}
    for t in tickers:
        n = _diff_paths(a0["results"][t], a1["results"][t])
        if n:
            noise[t] = n
            strict_noise += [f"{t}{p}" for p in n if p.split(".")[1].split("[")[0] in STRICT_FIELDS]
        ra, rb = a1["results"][t], b["results"][t]
        allowed_hits += sum(1 for k in ALLOWED_DETAIL_KEYS if k in (ra.get("details") or {}))
        d = _diff_paths(_strip_allowed(ra), _strip_allowed(rb))
        # 墙钟时刻字段：对照组（同一份代码跑两遍）里同一路径也不同、且名字就是时刻 ⇒ 记为噪声、列出来，不算差异
        tol = [x for x in d if x in set(n) and _TIME_LEAF.search(x)]
        if tol:
            tolerated[t] = tol
        d = [x for x in d if x not in tol]
        if d:
            bad[t] = d
    # 板上条目带写入时刻与按时衰减的强度：两次运行天然不同（对照组也不同），比对时去掉这两项，并把对照组的差异如实列出
    def _board(x):
        return [{k: v for k, v in e.items() if k not in BOARD_TIME_FIELDS} for e in x["board"]]
    board_noise = _diff_paths(a0["board"], a1["board"])
    board_diff = _diff_paths(_board(a1), _board(b))
    print(json.dumps({
        "tickers": len(tickers),
        "control_A0_vs_A1_noise_tickers": len(noise),
        "control_noise_paths_sample": {t: v[:5] for t, v in list(noise.items())[:3]},
        "control_noise_on_strict_fields": strict_noise[:10],
        "B_vs_A1_disallowed_diff_tickers": len(bad),
        "B_vs_A1_disallowed_diff": {t: v[:8] for t, v in bad.items()},
        "B_vs_A1_tolerated_wallclock_paths": sorted({p for v in tolerated.values() for p in v}),
        "A1_tickers_with_iv_crush_details": allowed_hits // 2 if allowed_hits else 0,
        "board_entries_A1_B": [len(a1["board"]), len(b["board"])],
        "board_diff_paths": board_diff[:10],
        "board_control_noise_fields": sorted({p.rsplit(".", 1)[-1] for p in board_noise}),
        "net": {k: {"A0": a0["stats"].get(k), "A1": a1["stats"].get(k), "B": b["stats"].get(k)}
                for k in ("requests", "urllib", "yfinance", "replay_miss")},
    }, ensure_ascii=False, indent=1, sort_keys=True))
    extra_miss = sorted(set(b.get("missed_keys", [])) - set(a1.get("missed_keys", [])))
    print("B_missed_not_in_A1", len(extra_miss), "A1_missed", len(a1.get("missed_keys", [])))
    ok = not strict_noise and not bad and not board_diff and not extra_miss
    print("VERDICT", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "compare":
    sys.exit(0 if compare(*sys.argv[2:5]) else 1)


# ═════════════════════════════════════════════════════════════════ record / replay
REC_MODE, CODE, SANDBOX, REC, OUT, FROZEN = sys.argv[1:7]
assert REC_MODE in ("record", "replay")
os.makedirs(SANDBOX, exist_ok=True)
os.makedirs(REC, exist_ok=True)
os.environ["ALPHA_HIVE_HOME"] = SANDBOX
os.environ["ALPHA_HIVE_MODEL_SNAPSHOT_DISABLE"] = "1"
os.environ["ANONYMIZED_TELEMETRY"] = "False"
if REC_MODE == "replay":
    os.environ["ALPHA_HIVE_YF_RATE"] = "10"     # 只改等待时长（离线没有真实配额），不改结果
for k in ("ALPHA_HIVE_CACHE_DIR", "ALPHA_HIVE_DB_PATH", "ALPHA_HIVE_TARGET_DATE"):
    os.environ.pop(k, None)
sys.path.insert(0, CODE)
os.chdir(CODE)

import numpy  # noqa: E402,F401
import pandas  # noqa: E402,F401
import pytz  # noqa: E402,F401
import yfinance  # noqa: E402,F401
import requests  # noqa: E402,F401
import datetime as _dtm  # noqa: E402

# 钟：FROZEN="none" ⇒ 不冻结（本机没装 freezegun，不为一次 A/B 改用户的 Python 环境）。三次运行在同一天、相隔几分钟，
# 按日期算的字段一致；秒级时间戳落进对照组噪声；URL 里带当前时刻的请求在 A 重放与 B 重放里**同样**找不到录制 ⇒
# 判据是「B 缺失的请求 ⊆ A 缺失的请求」，而不是缺失为 0。
if FROZEN != "none":
    import freezegun  # noqa: E402
    _FZ = freezegun.freeze_time(FROZEN, tick=True)      # FROZEN 按 UTC 解释
    _FZ.start()
    from freezegun.api import FakeDatetime as _FakeDT  # noqa: E402
    _orig_fake_now = _FakeDT.now.__func__

    def _fake_now(cls, tz=None):        # 同 replay_1012：naive now() = 冻结 UTC 换算到本机时区
        utc = _orig_fake_now(cls, _dtm.timezone.utc)
        return utc.astimezone().replace(tzinfo=None) if tz is None else utc.astimezone(tz)

    _FakeDT.now = classmethod(_fake_now)

import requests.adapters  # noqa: E402
import urllib.request  # noqa: E402
import urllib.error  # noqa: E402
import yfinance.data as _yfd  # noqa: E402

_LOCK = threading.Lock()
_COUNTS = {}
_STATS = {"requests": 0, "urllib": 0, "yfinance": 0, "blocked_slack": 0, "replay_miss": 0}
_MISSED = set()


def _key(kind, method, url, body):
    return hashlib.sha256(f"{kind}|{method}|{url}|".encode() + (body or b"")).hexdigest()[:24]


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
    with _LOCK, open(os.path.join(REC, "_index.jsonl"), "a") as f:
        f.write(json.dumps({"key": key, "n": n, "kind": rec["kind"], "url": rec["url"][:300],
                            "status": rec.get("status"), "exc": rec.get("exc")}) + "\n")


def _load(key):
    n = _next_n(key)
    p = _rec_path(key, n)
    if not os.path.exists(p):
        k = n - 1                                   # 调用次数多于录制：返回最后一次（同一请求内容相同）
        while k >= 0 and not os.path.exists(_rec_path(key, k)):
            k -= 1
        if k < 0:
            _STATS["replay_miss"] += 1
            _MISSED.add(key)
            return None
        p = _rec_path(key, k)
    with open(p, "rb") as f:
        return pickle.load(f)


_orig_send = requests.adapters.HTTPAdapter.send


def _send(self, request, **kw):
    url = request.url
    if "slack.com" in url:
        _STATS["blocked_slack"] += 1
        raise requests.exceptions.ConnectionError("ab: slack blocked")
    _STATS["requests"] += 1
    body = request.body if isinstance(request.body, bytes) else (request.body or "").encode()
    key = _key("requests", request.method, url, body)
    if REC_MODE == "replay":
        rec = _load(key)
        if rec is None:
            raise requests.exceptions.ConnectionError(f"replay miss: {url[:120]}")
        if rec.get("exc"):
            raise requests.exceptions.ConnectionError(f"replayed: {rec['exc']}")
        r = requests.models.Response()
        r.status_code, r._content, r.url, r.encoding, r.reason = rec["status"], rec["content"], rec["url"], rec.get("encoding"), rec.get("reason")
        r.headers = requests.structures.CaseInsensitiveDict(rec["headers"])
        r.request = request
        r.elapsed = _dtm.timedelta(0)
        return r
    try:
        r = _orig_send(self, request, **kw)
        _ = r.content
    except Exception as e:
        _save(key, {"kind": "requests", "url": url, "exc": f"{type(e).__name__}: {e}"[:500]})
        raise
    _save(key, {"kind": "requests", "url": url, "status": r.status_code, "content": r.content,
                "headers": dict(r.headers), "encoding": r.encoding, "reason": r.reason})
    return r


requests.adapters.HTTPAdapter.send = _send
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
    if "slack.com" in url:
        _STATS["blocked_slack"] += 1
        raise urllib.error.URLError("ab: slack blocked")
    _STATS["urllib"] += 1
    key = _key("urllib", method, url, data if isinstance(data, bytes) else None)
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
        try:
            body = e.read()
        except Exception:
            body = b""
        _save(key, {"kind": "urllib", "url": url, "http_error": True, "status": e.code, "reason": str(e.reason),
                    "headers": e.headers, "content": body})
        raise urllib.error.HTTPError(url, e.code, e.reason, e.headers, io.BytesIO(body))
    except Exception as e:
        _save(key, {"kind": "urllib", "url": url, "exc": f"{type(e).__name__}: {e}"[:500]})
        raise
    content = resp.read()
    status, headers, reason = getattr(resp, "status", 200), resp.headers, getattr(resp, "reason", "")
    resp.close()
    _save(key, {"kind": "urllib", "url": url, "status": status, "headers": headers, "content": content, "reason": reason})
    return _FakeResp(content, status, headers, url, reason)


urllib.request.OpenerDirector.open = _open
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
    _STATS["yfinance"] += 1
    pkey = json.dumps(params or {}, sort_keys=True, default=str)
    pkey = "&".join(p for p in pkey.split(",") if '"crumb"' not in p)   # crumb 每次不同，不进键
    key = _key("yfinance", str(request_method.__name__ if callable(request_method) else request_method), url + "|" + pkey,
               json.dumps(body, sort_keys=True, default=str).encode() if body else None)
    if REC_MODE == "replay":
        rec = _load(key)
        if rec is None or rec.get("exc"):
            # 不抛 YFRateLimitError：yf_gate 会进 180s 全局冷却，把后面所有 yfinance 都闸掉（replay_1012 实测）
            raise requests.exceptions.ConnectionError(f"replayed failure: {(rec or {}).get('exc')}")
        return _FakeYfResp(rec)
    try:
        r = _orig_mk(self, url, request_method, body=body, params=params, timeout=timeout, data=data)
    except Exception as e:
        _save(key, {"kind": "yfinance", "url": url, "exc": f"{type(e).__name__}: {e}"[:500]})
        raise
    _save(key, {"kind": "yfinance", "url": url, "status": r.status_code, "content": r.content, "headers": dict(r.headers)})
    return r


def _yf_cc(self, *a, **k):
    if REC_MODE == "replay":
        return ("cookie", "crumb", "basic")
    return _orig_cc(self, *a, **k)


_yfd.YfData._make_request = _yf_mk
_yfd.YfData._get_cookie_and_crumb = _yf_cc

# ───────────────────────────────────────────────── 仓库模块：与生产 run_swarm_scan 同样的装配
import yf_gate  # noqa: E402
yf_gate.install()
import llm_service  # noqa: E402
llm_service.disable()
from config import WATCHLIST  # noqa: E402
from pheromone_board import PheromoneBoard  # noqa: E402
from swarm_agents import ChronosBeeHorizon, prefetch_shared_data, inject_prefetched  # noqa: E402

TICKERS = [t for t in os.environ.get("AB_TICKERS", "").split(",") if t] or list(WATCHLIST)   # 冒烟可只跑几只
t0 = _time.monotonic()
board = PheromoneBoard()
bee = ChronosBeeHorizon(board, retriever=None)
target_date = _dtm.datetime.now().strftime("%Y-%m-%d")
prefetched = prefetch_shared_data(TICKERS, None, target_date=target_date)
inject_prefetched([bee], prefetched)
results, errors = {}, {}
for t in TICKERS:                       # 逐只顺序跑，请求次序可复现
    try:
        results[t] = bee.analyze(t)
    except Exception as e:  # noqa: BLE001
        errors[t] = f"{type(e).__name__}: {e}"
        results[t] = {"error": errors[t]}
board_entries = [e for e in board.snapshot()]
res = {"rec_mode": REC_MODE, "code": CODE, "frozen": FROZEN, "elapsed_s": round(_time.monotonic() - t0, 1),
       "stats": _STATS, "missed_keys": sorted(_MISSED), "errors": errors, "results": results, "board": board_entries}
with open(OUT, "w") as f:
    json.dump(json.loads(json.dumps(res, default=str, sort_keys=True)), f, ensure_ascii=False, indent=1, sort_keys=True)
print("AB_DONE", json.dumps({"rec_mode": REC_MODE, "elapsed_s": res["elapsed_s"], "stats": _STATS,
                             "errors": len(errors)}), flush=True)
