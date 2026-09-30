"""Alpha Bot 服务层：前端所需的全部数据都从这里出（v0.45.387）。

**全仓第三个、也是本包唯一一个** import 卖权选择器的模块（`tests/test_sell_strike_integration.py`
的 `ALLOWED_IMPORTERS` 显式登记）。规矩与 MCP 工具相同：

  · **对卖权账本只读**：只调 `sell_strike_report` 的只读函数（现算 / 按日期读行 / 就绪度），
    不调记录、结算、写本地报告；assess 一律 freeze=False——冻结的唯一写者仍是日报钩子。
  · **盲期**：行视图的结算字段、历史水平叠价格，全部由 `sell_strike_report` 按「全部 tenor 冻结」判定，
    本模块不自己判、不自己拼（少一份判据就少一次漂移）。
  · 本模块**自己**写的只有 `PATHS.alphabot_state`（设置与盘中快照），调用时求值。

取数慢且串行（每只 ~1.5MB、4–7 秒，进程内同一时刻只有一个外发请求），所以：现算结果按票缓存
`LIVE_TTL_SEC` 秒、同票并发合并成一次；总览读账本的收盘后行，不逐只现拉。
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import sell_strike_report as R
from hive_logger import PATHS, get_logger

_log = get_logger("alphabot")

ET = ZoneInfo("America/New_York")
LIVE_TTL_SEC = 60.0
#: 失败结果也短暂缓存：CBOE 不可达时别让每次切页都再排 15 秒 × 3 次重试
FAIL_TTL_SEC = 15.0
MAX_FOCUS = 5
DEFAULT_FOCUS = ("NVDA", "TSLA", "MSFT", "META", "AMZN")
DEFAULT_INTERVAL_MIN = 10
#: 快照里逐行权价 GEX 只存现价 ±这么多（热力图 / 回看点够用；不存原始链）
SNAPSHOT_STRIKE_BAND = 0.12
_TICKER_RE = re.compile(r"^[A-Z_][A-Z0-9.\-]{0,9}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BadRequest(ValueError):
    """参数非法（服务端映射成 400）。"""


def norm_ticker(t) -> str:
    s = str(t or "").upper().strip()
    if not _TICKER_RE.match(s):
        raise BadRequest(f"代码不合法：{t!r}")
    return s


def norm_date(d) -> str:
    s = str(d or "").strip()
    if not _DATE_RE.match(s):
        raise BadRequest(f"日期应为 YYYY-MM-DD：{d!r}")
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError as exc:
        raise BadRequest(f"日期不合法：{d!r}") from exc
    return s


def clean_json(obj):
    """NaN / inf → None（JSON 没有这两个值；原样外露会让浏览器 JSON.parse 整体失败）。"""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean_json(v) for v in obj]
    return obj


def now_et() -> datetime:
    return datetime.now(ET)


def market_session(now: Optional[datetime] = None) -> dict:
    """美东此刻是否在常规交易时段（交易日历取 `is_trading_day`；它不可用时只按工作日判并注明）。"""
    now = (now or now_et()).astimezone(ET)
    d = now.date()
    close_t, calendar = dtime(16, 0), "is_trading_day"
    try:
        import is_trading_day as itd
        trading, why = itd.is_trading_day(d)
        close_t = itd.session_close_et(d) or close_t
    except Exception as exc:  # noqa: BLE001 - 日历坏了：按工作日判，但要说出来
        trading, why, calendar = d.weekday() < 5, f"calendar_unavailable:{type(exc).__name__}", "weekday_only"
    open_t = dtime(9, 30)
    live = bool(trading) and open_t <= now.time() < close_t
    return {"now_et": now.isoformat(timespec="seconds"), "date_et": d.isoformat(), "trading_day": bool(trading),
            "reason": why, "open": open_t.isoformat(timespec="minutes"),
            "close": close_t.isoformat(timespec="minutes"), "live": live, "calendar": calendar}


def _atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def default_watchlist() -> List[str]:
    try:
        from config import WATCHLIST
        return [str(t).upper() for t in WATCHLIST]
    except Exception as exc:  # noqa: BLE001 - 没有 config 就没有默认列表，页面照样能搜代码
        _log.warning("读 config.WATCHLIST 失败：%s", exc)
        return []


def _default_bars(ticker: str) -> dict:
    """日线收盘（价格页用）。只走 Twelve Data 共享入口（每日一次、有进程内缓存）；没配 key 就如实说不可得，
    **不**换别的源——别的源（本地价格索引）首次读取会写迁移标记，只读页面不该造成写入。"""
    try:
        import twelve_data
        if not twelve_data.is_configured():
            return {"available": False, "reason": "twelve_data_not_configured", "bars": []}
        rows = twelve_data.fetch_bars(ticker, twelve_data.SHARED_BARS_WINDOW)
        if not rows:
            return {"available": False, "reason": "twelve_data_returned_nothing", "bars": []}
        return {"available": True, "source": "twelve_data",
                "bars": [{"date": str(r.get("date"))[:10], "close": r.get("close")} for r in rows]}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"exception:{type(exc).__name__}: {exc}", "bars": []}


class AlphaBotService:
    """前端的数据面。`fetch_fn` / `bars_fn` / `clock` 可注入（测试、`--demo`）。"""

    def __init__(self, *, fetch_fn=None, bars_fn: Optional[Callable[[str], dict]] = None,
                 clock: Callable[[], float] = time.monotonic, demo: bool = False,
                 ledger_dir=None, live_ttl: float = LIVE_TTL_SEC, state_dir=None):
        self.fetch_fn = fetch_fn
        self.bars_fn = bars_fn or _default_bars
        self.clock = clock
        self.demo = demo
        self.ledger_dir = ledger_dir          # None ⇒ 账本自己解析（生产数据根）；只读
        self.live_ttl = float(live_ttl)
        self._state_override = Path(state_dir) if state_dir else None   # --demo 用临时目录，不碰真状态
        self._cache: Dict[str, tuple] = {}
        self._bars_cache: Dict[tuple, dict] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._settings_lock = threading.Lock()
        self.stats = {"live_computed": 0, "live_cache_hits": 0, "live_failures": 0}

    # ── 路径（调用时求值）
    def state_dir(self) -> Path:
        return self._state_override or PATHS.alphabot_state

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    # ── 现算
    def live(self, ticker, *, force: bool = False) -> dict:
        t = norm_ticker(ticker)
        hit = self._fresh(t)
        if hit is not None and not force:
            self.stats["live_cache_hits"] += 1
            return self._stamp(hit)
        with self._lock_for("live:" + t):
            hit = self._fresh(t)                       # 排队期间别人可能刚算完：同票并发只算一次
            if hit is not None and not force:
                self.stats["live_cache_hits"] += 1
                return self._stamp(hit)
            res = clean_json(R.compute_live_detail(t, fetch_fn=self.fetch_fn, state_dir=self.ledger_dir))
            ok = bool(res.get("data_available"))
            self.stats["live_computed" if ok else "live_failures"] += 1
            if not ok:
                _log.warning("[%s] 现算不可得：%s", t, res.get("reason"))
            self._cache[t] = (self.clock(), res)
            return self._stamp(self._cache[t])

    def _fresh(self, t: str):
        ent = self._cache.get(t)
        if ent is None:
            return None
        ttl = self.live_ttl if ent[1].get("data_available") else FAIL_TTL_SEC
        return ent if (self.clock() - ent[0]) < ttl else None

    def _stamp(self, ent) -> dict:
        out = dict(ent[1])
        out["cache_age_sec"] = round(max(0.0, self.clock() - ent[0]), 1)
        out["demo"] = self.demo
        if self.demo and "assess" in out:
            out["assess"] = {k: {"status": "demo", "summary": "演示模式：不读账本，没有就绪度"} for k in out["assess"]}
        return out

    # ── 账本（只读）
    def ledger_dates(self) -> dict:
        return clean_json({"dates": R.ledger_dates(state_dir=self.ledger_dir),
                           "state_dir": R.LG.state_dir_status(self.ledger_dir)})

    def overview(self, date: Optional[str] = None) -> dict:
        """总览：某日（缺省 = 账本最新一日）全部标的的精简行。demo 模式没有账本 ⇒ 用合成链现算出同形的行。"""
        if self.demo:
            return self._demo_overview()
        if date is None:
            dates = R.ledger_dates(state_dir=self.ledger_dir, limit=1)
            if not dates:
                return clean_json({"data_available": False, "as_of": None, "tenors": {"monthly": [], "weekly": []},
                                   "reason": "no_ledger_rows", "state_dir": R.LG.state_dir_status(self.ledger_dir),
                                   "source": "ledger"})
            date = dates[0]
        out = R.rows_for_date_view(norm_date(date), state_dir=self.ledger_dir, compact=True)
        out["source"] = "ledger"
        return clean_json(out)

    def _demo_overview(self) -> dict:
        tenors = {"monthly": [], "weekly": []}
        as_of = None
        for t in self.watchlist()["tickers"]:
            d = self.live(t)
            if not d.get("data_available"):
                continue
            as_of = d.get("as_of")
            for tenor, view in (d.get("tenors") or {}).items():
                tenors.setdefault(tenor, []).append(clean_json(R.row_brief(view, blind=False)))
        return {"data_available": any(tenors.values()), "as_of": as_of, "tenors": tenors, "source": "demo",
                "settlement_blinded": True, "state_dir": {"path": None, "exists": False, "hint": "演示模式不读账本"}}

    def ledger_rows(self, date, *, full: bool = False) -> dict:
        return clean_json(R.rows_for_date_view(norm_date(date), state_dir=self.ledger_dir, compact=not full))

    def ticker_ledger(self, ticker, date) -> dict:
        return clean_json(R.rows_for_ticker(norm_date(date), norm_ticker(ticker), state_dir=self.ledger_dir))

    def assess(self) -> dict:
        return clean_json(R.assess_overview(state_dir=self.ledger_dir))

    def env_history(self, ticker) -> dict:
        return clean_json(R.env_history(norm_ticker(ticker), state_dir=self.ledger_dir))

    def meta_texts(self) -> dict:
        """口径页：单位、符号约定、局限、收益口径、路由规则常量（都取代码里那一份，不在前端另抄）。"""
        C = R.C
        return clean_json({
            "disclaimer": R.DISCLAIMER, "caveats": list(R.CAVEATS), "yield_note": R.YIELD_NOTE,
            "route": {"rule_version": C.ROUTE_RULE_VERSION, "view": C.ROUTE_VIEW,
                      "flip_buffer_pct": C.FLIP_BUFFER_PCT, "base_rung": C.BASE_RUNG, "far_rung": C.FAR_RUNG,
                      "min_sweep_contracts": C.MIN_SWEEP_CONTRACTS},
            "ladder": {"deltas": list(C.LADDER_DELTAS), "delta_tol": C.DELTA_TOL,
                       "max_spread_pct": C.MAX_SPREAD_PCT, "wing_width_sigma": C.WING_WIDTH_SIGMA,
                       "tenors": C.TENORS, "structures": list(C.STRUCTURES)},
            "prereg": {k: (list(v) if isinstance(v, tuple) else v) for k, v in R.LG.PREREG.items()},
        })

    # ── 价格
    def bars(self, ticker) -> dict:
        t = norm_ticker(ticker)
        key = (t, now_et().date().isoformat())
        with self._lock_for("bars:" + t):
            if key not in self._bars_cache:
                res = self.bars_fn(t)
                if not res.get("available"):
                    return clean_json({**res, "ticker": t})     # 失败不入缓存，下次再试
                self._bars_cache[key] = res
            return clean_json({**self._bars_cache[key], "ticker": t})

    # ── 设置（自选 / 盘中关注列表）
    def _settings_path(self) -> Path:
        return self.state_dir() / "settings.json"

    def settings(self) -> dict:
        base = {"favorites": [], "focus": list(DEFAULT_FOCUS), "interval_min": DEFAULT_INTERVAL_MIN,
                "poll_enabled": True}
        p = self._settings_path()
        if p.is_file():
            try:
                saved = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    base.update({k: saved[k] for k in base if k in saved})
            except (OSError, ValueError) as exc:
                _log.warning("Alpha Bot 设置文件读不了（按缺省处理，页面会显示）：%s", exc)
                base["settings_error"] = f"{type(exc).__name__}: {exc}"
        return base

    def update_settings(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise BadRequest("设置须为 JSON 对象")
        with self._settings_lock:
            cur = self.settings()
            cur.pop("settings_error", None)
            if "favorites" in patch:
                favs = [norm_ticker(t) for t in (patch["favorites"] or [])]
                cur["favorites"] = list(dict.fromkeys(favs))[:60]
            if "focus" in patch:
                focus = list(dict.fromkeys(norm_ticker(t) for t in (patch["focus"] or [])))
                if len(focus) > MAX_FOCUS:
                    raise BadRequest(f"盘中关注最多 {MAX_FOCUS} 只（每只每拍约 1.5MB、串行 4–7 秒）")
                cur["focus"] = focus
            if "interval_min" in patch:
                try:
                    iv = int(patch["interval_min"])
                except (TypeError, ValueError) as exc:
                    raise BadRequest("interval_min 须为整数") from exc
                if not 5 <= iv <= 60:
                    raise BadRequest("快照间隔须在 5–60 分钟")
                cur["interval_min"] = iv
            if "poll_enabled" in patch:
                cur["poll_enabled"] = bool(patch["poll_enabled"])
            _atomic_write_json(self._settings_path(), cur)
            return cur

    def watchlist(self) -> dict:
        wl = default_watchlist()
        favs = [t for t in self.settings().get("favorites", []) if t not in wl]
        return {"watchlist": wl, "favorites": favs, "tickers": wl + favs}

    # ── 盘中快照
    def _intraday_dir(self, date_et: str) -> Path:
        return self.state_dir() / "intraday" / norm_date(date_et)

    @staticmethod
    def snapshot_from(detail: dict, ts_et: str) -> dict:
        """一张盘中快照 = 现算结果的聚合数值（不存原始链、不存梯子）。"""
        S = detail.get("underlying_price")
        views = detail.get("views") or {}
        snap = {"ts": ts_et, "payload_last_trade_time": detail.get("payload_last_trade_time"),
                "underlying_price": S, "underlying_price_source": detail.get("underlying_price_source"),
                "session_live": detail.get("session_live"), "iv30": detail.get("iv30"), "views": {}}
        for name in ("le_45dte", "next_expiry"):
            v = views.get(name) or {}
            zg = v.get("zero_gamma") or {}
            vol_by_k = {r.get("strike"): r.get("net_gex_usd_per_1pct") for r in v.get("volume_strikes") or []}
            snap["views"][name] = {
                "zero_gamma": {k: zg.get(k) for k in ("curve_state", "sign_at_spot", "total_at_spot", "nearest",
                                                      "nearest_below", "nearest_above")},
                "majors": v.get("majors"),
                "net_gex_total": (v.get("totals") or {}).get("net_gex_usd_per_1pct"),
                "strikes": [[r.get("strike"), r.get("net_gex_usd_per_1pct"), vol_by_k.get(r.get("strike"))]
                            for r in v.get("strikes") or []
                            if S and r.get("strike") and abs(r["strike"] / S - 1.0) <= SNAPSHOT_STRIKE_BAND],
            }
        snap["route"] = {tenor: {k: (tv.get("route") or {}).get(k) for k in ("put", "call", "flag_put", "flag_call")}
                         for tenor, tv in (detail.get("tenors") or {}).items()}
        return clean_json(snap)

    def take_snapshot(self, ticker, *, now: Optional[datetime] = None) -> dict:
        """现拉一次并追加一张快照。payload 的最后成交时刻与上一张相同（CBOE 文件没更新）⇒ 不重复记。"""
        t = norm_ticker(ticker)
        now = (now or now_et()).astimezone(ET)
        detail = self.live(t, force=True)
        if not detail.get("data_available"):
            return {"ticker": t, "recorded": False, "reason": detail.get("reason")}
        snap = self.snapshot_from(detail, now.isoformat(timespec="seconds"))
        path = self._intraday_dir(now.date().isoformat()) / f"{t}.jsonl"
        with self._lock_for("snap:" + t):
            last = self._last_line(path)
            if last and last.get("payload_last_trade_time") and \
                    last.get("payload_last_trade_time") == snap.get("payload_last_trade_time") and \
                    last.get("underlying_price") == snap.get("underlying_price"):
                return {"ticker": t, "recorded": False, "reason": "payload_unchanged"}
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(snap, ensure_ascii=False) + "\n")
        return {"ticker": t, "recorded": True, "ts": snap["ts"]}

    @staticmethod
    def _last_line(path: Path) -> Optional[dict]:
        if not path.is_file():
            return None
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        try:
            return json.loads(lines[-1]) if lines else None
        except ValueError:
            return None

    def intraday(self, ticker, date_et: Optional[str] = None) -> dict:
        """某票某个美东日的全部快照（**只给单日**：跨日拼接会把当日路由和其后价格放在一起，见预注册 §7）。"""
        t = norm_ticker(ticker)
        d = norm_date(date_et) if date_et else now_et().date().isoformat()
        if self.demo:
            return self._demo_intraday(t, d)
        path = self._intraday_dir(d) / f"{t}.jsonl"
        snaps, bad = [], 0
        if path.is_file():
            for ln in path.read_text(encoding="utf-8").splitlines():
                if not ln.strip():
                    continue
                try:
                    snaps.append(json.loads(ln))
                except ValueError:
                    bad += 1
        return {"ticker": t, "date": d, "snapshots": snaps, "n_bad_lines": bad}

    def _demo_intraday(self, t: str, d: str) -> dict:
        """演示：用合成链在 09:30–16:00 每 10 分钟编一张快照（现价走一条确定性的小路径）。"""
        from alphabot import synthetic as syn
        key = ("demo_intraday", t, d)
        if key not in self._bars_cache:
            snaps = []
            for i in range(40):
                shift = 0.012 * math.sin(i / 6.0 + (syn._seed(t) % 7)) + 0.0006 * i
                raw = syn.demo_payload(t, d, intraday_shift=shift)
                hh, mm = divmod(9 * 60 + 30 + 10 * i, 60)
                raw["payload_last_trade_time"] = f"{d}T{hh:02d}:{mm:02d}:00"
                raw["session_live"] = True
                detail = clean_json(R.compute_live_detail(t, fetch_fn=lambda *_a, _r=raw, **_k: (_r, None),
                                                          state_dir=self.ledger_dir))
                snaps.append(self.snapshot_from(detail, f"{d}T{hh:02d}:{mm:02d}:00-04:00"))
            self._bars_cache[key] = snaps
        return {"ticker": t, "date": d, "snapshots": self._bars_cache[key], "n_bad_lines": 0, "demo": True}

    def intraday_dates(self, ticker) -> dict:
        t = norm_ticker(ticker)
        if self.demo:
            return {"ticker": t, "dates": [now_et().date().isoformat()], "demo": True}
        root = self.state_dir() / "intraday"
        dates = []
        if root.is_dir():
            dates = sorted((p.name for p in root.iterdir() if p.is_dir() and _DATE_RE.match(p.name)
                            and (p / f"{t}.jsonl").is_file()), reverse=True)
        return {"ticker": t, "dates": dates}


class IntradayPoller:
    """盘中定时快照（服务进程内的后台线程）。只在常规交易时段、`poll_enabled` 时对关注列表拍照。

    失败怎么被看见：每轮结果记在 `status`（`/api/meta` 返回、页面显示），失败另打 WARNING——
    不是「后台默默没拍到」。"""

    def __init__(self, service: AlphaBotService, *, session_fn=market_session, sleep_sec: float = 30.0):
        self.service = service
        self.session_fn = session_fn
        self.sleep_sec = sleep_sec
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_round: Optional[float] = None
        self.status = {"running": False, "last_round_et": None, "last_results": [], "rounds": 0, "errors": 0}

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="alphabot-intraday", daemon=True)
            self._thread.start()
            self.status["running"] = True

    def stop(self) -> None:
        self._stop.set()
        self.status["running"] = False

    def due(self, now_mono: float, interval_min: int) -> bool:
        return self._last_round is None or (now_mono - self._last_round) >= interval_min * 60.0

    def tick(self, *, now_mono: Optional[float] = None) -> Optional[list]:
        """跑一轮（到点且在盘中才拍）；返回本轮结果或 None（没到点 / 不在盘中 / 关闭）。"""
        st = self.service.settings()
        if not st.get("poll_enabled"):
            return None
        sess = self.session_fn()
        if not sess.get("live"):
            return None
        now_mono = time.monotonic() if now_mono is None else now_mono
        if not self.due(now_mono, int(st.get("interval_min") or DEFAULT_INTERVAL_MIN)):
            return None
        self._last_round = now_mono
        results = []
        for t in (st.get("focus") or [])[:MAX_FOCUS]:
            try:
                results.append(self.service.take_snapshot(t))
            except Exception as exc:  # noqa: BLE001 - 一只失败不挡其余；计数 + 日志
                self.status["errors"] += 1
                _log.warning("[%s] 盘中快照失败：%s: %s", t, type(exc).__name__, exc)
                results.append({"ticker": t, "recorded": False, "reason": f"exception:{type(exc).__name__}"})
        failed = [r for r in results if not r.get("recorded") and r.get("reason") != "payload_unchanged"]
        if failed:
            _log.warning("盘中快照本轮 %d/%d 只未记录：%s", len(failed), len(results),
                         [(r["ticker"], r.get("reason")) for r in failed])
        self.status.update({"last_round_et": sess.get("now_et"), "last_results": results,
                            "rounds": self.status["rounds"] + 1})
        return results

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - 后台线程不能死；死了页面上就只剩旧状态
                self.status["errors"] += 1
                _log.warning("盘中快照线程异常：%s: %s", type(exc).__name__, exc, exc_info=True)
            self._stop.wait(self.sleep_sec)
