"""Alpha Bot 服务层：前端所需的全部数据都从这里出（v0.45.388）。

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
from alphabot import hedge as HG
from alphabot import howto as HW
from alphabot import premium as PM
from alphabot import straddle as SD
from alphabot import strangle_compare as SC
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
    """参数非法（服务端映射成 400）。`param` = 出错的参数名（有就随 400 一起回给前端）。"""

    def __init__(self, msg="", param: Optional[str] = None):
        super().__init__(msg)
        self.param = param


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
        # 自己的码（不叫 exception：溢价页把那个前缀读成「计算出错」，会把取数失败指错层）
        return {"available": False, "reason": f"bars_fetch_exception:{type(exc).__name__}: {exc}", "bars": []}


class AlphaBotService:
    """前端的数据面。`fetch_fn` / `bars_fn` / `clock` 可注入（测试、`--demo`）。"""

    def __init__(self, *, fetch_fn=None, bars_fn: Optional[Callable[[str], dict]] = None,
                 clock: Callable[[], float] = time.monotonic, demo: bool = False,
                 ledger_dir=None, live_ttl: float = LIVE_TTL_SEC, state_dir=None,
                 straddle_root=None, straddle_quote_fn=None, now_fn: Optional[Callable[[], datetime]] = None,
                 premium_root=None):
        self.fetch_fn = fetch_fn
        self.bars_fn = bars_fn or _default_bars
        self.clock = clock
        self.demo = demo
        self.ledger_dir = ledger_dir          # None ⇒ 账本自己解析（生产数据根）；只读
        self.live_ttl = float(live_ttl)
        self._state_override = Path(state_dir) if state_dir else None   # --demo 用临时目录，不碰真状态
        # 跨式账本页（v0.45.424）：数据根 None ⇒ PATHS.home（调用时求值）；报价函数 None ⇒ cboe_options.quote_held
        self.straddle_root = straddle_root
        self.straddle_quote_fn = straddle_quote_fn
        self.now_fn = now_fn or now_et
        self._straddle_cache: Dict[str, tuple] = {}
        # 溢价页（v0.45.492）读的 IV 历史索引 / VRP 账本的数据根；None ⇒ 调用时的 PATHS.home（财报日读卖权账本 ledger_dir）
        self.premium_root = premium_root
        self._premium_cache: Dict[str, tuple] = {}
        self._premium_rules_cache: Optional[dict] = None     # 规则常量与文案表进程内不变：算一次
        self._cache: Dict[str, tuple] = {}
        self._bars_cache: Dict[tuple, dict] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._settings_lock = threading.Lock()
        self.stats = {"live_computed": 0, "live_cache_hits": 0, "live_failures": 0,
                      "premium_computed": 0, "premium_cache_hits": 0, "premium_failures": 0,
                      "howto_failures": 0}

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
            if ok:
                res["howto"] = self._howto(t, res)
            self.stats["live_computed" if ok else "live_failures"] += 1
            if not ok:
                _log.warning("[%s] 现算不可得：%s", t, res.get("reason"))
            self._cache[t] = (self.clock(), res)
            return self._stamp(self._cache[t])

    def _howto(self, t: str, res: dict) -> dict:
        """「怎么卖」：同一份报价翻译成下单腿 / 限价 / 买回价 / 标的价位（纯函数，见 `alphabot/howto.py`）。
        算不出来不许吞成「没有这一栏」：写 `available: False` + 原因，前端显示原因，日志留 warning。"""
        try:
            return clean_json(HW.build(res, R.C.structure_quote, R.C.BASE_RUNG))
        except Exception as exc:  # noqa: BLE001
            self.stats["howto_failures"] += 1
            _log.warning("[%s] 「怎么卖」现算失败：%s: %s", t, type(exc).__name__, exc)
            return {"available": False, "reason": f"exception:{type(exc).__name__}: {exc}", "tenors": {}}

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
            "straddle": self.straddle_rules(),
            "hedge": self.hedge_rules(),
            "premium": self.premium_rules(),
            "howto": HW.public_rules(),
        })

    # ── 财报跨式账本（v0.45.424，只读）
    def straddle_rules(self) -> dict:
        """跨式账本页与帮助页用的规则常量——取代码里那一份，不在前端另抄。
        懒 import + 兜底：这两个模块 import 时就求值数据根，环境没设好时不能连坐整个 Alpha Bot；取不到就写明原因。"""
        out = {"directional_delta_warn": SD.DIRECTIONAL_DELTA_WARN,
               "ledger_ready_et": SD.LEDGER_READY_ET.isoformat(timespec="minutes")}
        try:
            import earnings_vol_signal as evs
            out.update({k: evs.CONFIG.get(k) for k in ("rich_ratio", "cheap_ratio", "max_spread_pct",
                                                       "min_events", "history_n")})
        except Exception as exc:  # noqa: BLE001
            out["signal_rules_error"] = f"{type(exc).__name__}: {exc}"
        try:
            import options_paper_leg as opl
            out.update({k: opl.CONFIG.get(k) for k in ("starting_capital", "risk_per_trade_pct", "max_open",
                                                       "expiry_buffer_days")})
        except Exception as exc:  # noqa: BLE001
            out["ledger_rules_error"] = f"{type(exc).__name__}: {exc}"
        try:
            import straddle_gex_prereg as SP
            out["prereg"] = SP.public_constants()
        except Exception as exc:  # noqa: BLE001
            out["prereg_error"] = f"{type(exc).__name__}: {exc}"
        return out

    def _straddle_ledger(self, now: datetime) -> dict:
        if self.demo:
            from alphabot import synthetic as syn
            return syn.demo_straddle_ledger(now.date().isoformat())
        return SD.load_ledger(self.straddle_root)

    def _straddle_shadow(self, signals: list) -> dict:
        """GEX 影子记录的进度（**只有计数**，见 straddle_gex_prereg 的盲期规则）。"""
        if self.demo:
            return {"available": False, "reason": "演示模式没有影子记录"}
        try:
            import straddle_gex_prereg as SP
            return SP.progress(signals, root=self.straddle_root)
        except Exception as exc:  # noqa: BLE001 - 进度算不出来如实说，不当成「0 个事件」
            _log.warning("跨式 GEX 影子记录进度不可得：%s: %s", type(exc).__name__, exc)
            return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    def straddle(self) -> dict:
        now = self.now_fn()
        led = self._straddle_ledger(now)
        return clean_json(SD.build_overview(led, now_et=now, rules=self.straddle_rules(),
                                            shadow=self._straddle_shadow(led.get("signals") or [])))

    # ── 对冲账本（v0.45.459，只读）：组合 Greeks 的纸面 SPY 覆盖
    def _hedge_view(self, now: datetime) -> dict:
        """数据只经账本属主的只读视图 `portfolio_greeks.ledger_view`（历史走 `load_history` 的放行闸）。
        数据根与跨式页同一个（`straddle_root`；None ⇒ 调用时的 PATHS.home）。懒 import：它 import 时就求值数据根，
        环境没设好时不能连坐整个 Alpha Bot；取不到就写明原因。"""
        if self.demo:
            from alphabot import synthetic as syn
            return syn.demo_hedge_view(now.date().isoformat())
        try:
            import portfolio_greeks as pg
            return pg.ledger_view(self.straddle_root)
        except Exception as exc:  # noqa: BLE001 - 如实说读不了，不当成空账本
            _log.warning("对冲账本视图不可得：%s: %s", type(exc).__name__, exc)
            return {"exists": False, "state_dir": None, "errors": {"view": f"{type(exc).__name__}: {exc}"}}

    def hedge_rules(self) -> dict:
        """对冲页与帮助页用的规则常量——取 `portfolio_greeks.CONFIG` 那一份，不在前端另抄。"""
        try:
            import portfolio_greeks as pg
            return {k: pg.CONFIG.get(k) for k in pg.PUBLIC_RULE_KEYS} | {"measurement_start": pg.MEASUREMENT_START}
        except Exception as exc:  # noqa: BLE001
            return {"rules_error": f"{type(exc).__name__}: {exc}"}

    def hedge(self) -> dict:
        now = self.now_fn()
        return clean_json(HG.build_overview(self._hedge_view(now), now_et=now))

    def _straddle_quote(self, t: str, symbols: List[str]) -> dict:
        if self.straddle_quote_fn is not None:
            return self.straddle_quote_fn(t, symbols)
        if self.demo:
            from alphabot import synthetic as syn
            return syn.demo_quote_held(t, symbols, self.now_fn().date().isoformat())
        import cboe_options
        return cboe_options.quote_held(t, symbols)

    def straddle_live(self, ticker, *, force: bool = False) -> dict:
        """一笔持仓的盘中参考报价 + 当天水平。**只读、不写账本**。
        先走 `live()`（现算当天水平；生产路径会清该票进程缓存再拉一份新 payload），再报价：`quote_held` 读同一个
        进程缓存，于是两者通常出自同一份 payload——不同时 `same_payload=False`，页面照实标出。"""
        t = norm_ticker(ticker)
        now = self.now_fn()
        if self.demo:
            positions = self._straddle_ledger(now).get("positions") or []
        else:
            positions = SD.load_positions(self.straddle_root)     # 只读持仓文件，不重读整本账本
        pos = next((p for p in positions if str(p.get("ticker") or "").upper() == t), None)
        if pos is None:
            raise BadRequest(f"{t} 在跨式账本里没有持仓")
        key = f"{t}|{pos.get('call_symbol')}|{pos.get('put_symbol')}"
        with self._lock_for("straddle:" + t):
            ent = self._straddle_cache.get(key)
            if ent is not None and not force and (self.clock() - ent[0]) < self.live_ttl:
                return {**ent[1], "cache_age_sec": round(self.clock() - ent[0], 1)}
            detail = self.live(t, force=force)
            syms = [str(pos.get("call_symbol")), str(pos.get("put_symbol"))]
            try:
                q = self._straddle_quote(t, syms)
            except Exception as exc:  # noqa: BLE001 - 报价失败如实返回，不拿账本 mark 顶上
                _log.warning("[%s] 跨式盘中报价失败：%s: %s", t, type(exc).__name__, exc)
                q = {"available": False, "reason": f"exception:{type(exc).__name__}", "quotes": {}}
            res = clean_json(SD.live_view(pos, q, SD.levels_brief(detail), now_et=now))
            res["demo"] = self.demo
            if not res.get("available"):
                _log.warning("[%s] 跨式盘中报价不可得：%s", t, res.get("reason"))
            self._straddle_cache[key] = (self.clock(), res)
            return {**res, "cache_age_sec": 0.0}

    def straddle_compare(self, ticker, as_of=None) -> dict:
        """「跨式 vs 宽跨」并排对比卡（v0.45.584）。**只读、只并排事实、不给结论**（见 `alphabot.strangle_compare`）。
        数据全部来自本机已有文件：当天快照里的 `quote_set`（ATM 两腿 + 25Δ 两腿）、`earnings_cache/<票>_moves.json`
        （历史财报涨跌）、跨式账本的信号行与净值——不联网。`as_of` 缺省 = 账本最近一个扫描日；给持仓的入场日即「入场时的对比」。
        定仓用账本自己的 `options_paper_leg.size_contracts`（懒 import；取不到就写明原因、张数留空）。"""
        t = norm_ticker(ticker)
        now = self.now_fn()
        if self.demo:
            from alphabot import synthetic as syn
            led = self._straddle_ledger(now)
            day = norm_date(as_of) if as_of else (SD.ledger_as_of(led) or now.date().isoformat())
            qs, raw = syn.demo_compare_inputs(t, day)
            qs_err = hist_err = None
            sig, nav = SD.pick_signal_and_nav(led.get("signals"), led.get("equity"), t, day)
        else:
            root = self.straddle_root
            if as_of:
                day = norm_date(as_of)
            else:
                meta, _ = SD.read_json((Path(root) if root is not None else PATHS.home) / SD.LEDGER_DIRNAME / "meta.json")
                day = str((meta or {}).get("last_run_date") or SD.expected_ledger_date(now)[0])
            qs, qs_err = SD.load_quote_set(t, day, root)
            raw, hist_err = SD.load_hist_moves(t, root)
            sig, nav = SD.load_signal_and_nav(t, day, root)

        def size_fn(premium, nav_):
            try:
                import options_paper_leg as opl
                return opl.size_contracts("short", premium, nav_)
            except Exception as exc:  # noqa: BLE001 - 定仓函数取不到：张数留空并说明，不编一个
                return 0, f"sizing unavailable: {type(exc).__name__}"
        res = clean_json(SC.compare(t, day, qs=qs, qs_error=qs_err, raw_moves=raw, hist_error=hist_err, signal=sig,
                                    nav=nav, size_fn=size_fn, max_spread_frac=self.straddle_rules().get("max_spread_pct")))
        res["demo"] = self.demo
        return res

    # ── 溢价（v0.45.492，只读）：单票全链按 IV ÷ 期限匹配 RV 排序
    def _premium_deps(self) -> "PM.Deps":
        """premium.py 不 import 卖权模块（火墙）：Δ / 平值 IV / 财报分类都从这里注入**卖权页、期限页用的同一个函数**，
        三个页面的数对得上（改名或改行为由 tests/test_alphabot_premium.py 的对拍测试接住）。"""
        import bars_integrity
        import iv_history
        import vrp_signal
        r = R.L.RISK_FREE_RATE
        return PM.Deps(leg=lambda c, S: R.C._leg(c, S, r), atm_iv=R._atm_iv, classify_earnings=R.C.classify_earnings,
                       year_fraction=R.L.year_fraction, rv_from_closes=vrp_signal.realized_vol_from_closes,
                       trading_days=bars_integrity.trading_days, find_gaps=bars_integrity.find_gaps,
                       rank_fn=iv_history.iv_rank_from_history, max_spread_pct=R.C.MAX_SPREAD_PCT,
                       iv_rank_min_days=iv_history.IV_RANK_MIN_DAYS, iv_rank_window=iv_history.IV_RANK_FULL_WINDOW,
                       vrp_min_obs=int(vrp_signal.CONFIG["min_obs_per_ticker"]))

    def premium_rules(self) -> dict:
        """规则常量与文案表（进程内不变 ⇒ 只算一次；取不到不缓存，下次再试）。"""
        if self._premium_rules_cache is None:
            try:
                self._premium_rules_cache = PM.public_rules(self._premium_deps()) | {"cache_ttl_sec": self.live_ttl}
            except Exception as exc:  # noqa: BLE001 - 取不到就写明原因，不连坐 /api/method
                return {"rules_error": f"{type(exc).__name__}: {exc}"}
        return self._premium_rules_cache

    def _premium_home(self) -> Path:
        return Path(self.premium_root) if self.premium_root is not None else PATHS.home

    def _premium_cache_dir(self) -> Path:
        """= `PATHS.cache_dir` 的解析规则，但**不 mkdir**（那个属性一读就建目录，只读页面不该造成写入）。
        显式给了 premium_root（测试）⇒ 只认它下面的 cache/。"""
        if self.premium_root is not None:
            return Path(self.premium_root) / "cache"
        return Path(os.environ.get("ALPHA_HIVE_CACHE_DIR", str(PATHS.home / "cache")))

    @staticmethod
    def _local_name(t: str) -> str:
        """本地文件按扫描侧代码命名（BRK-B），CBOE 侧写 BRK.B。"""
        return t.replace(".", "-")

    def _premium_fetch(self):
        """溢价页取链的唯一入口。生产 = `sell_strike_ledger._default_fetch`（与卖权页同一个取数函数），
        **不清进程缓存**：页面先走过 live()（它清缓存再拉一份），这里命中同一份 payload，不多发请求。"""
        return self.fetch_fn or R.LG._default_fetch

    def _premium_earnings(self, t: str, as_of: str, deps) -> dict:
        """财报日 ← 卖权账本（日报钩子按本轮 ChronosBee 写进每行的 earnings_date）里该票最近一次有信息的观测。
        只读、与总览 / 卖权页读的是同一个账本目录（`ledger_dir`）。**不读** earnings_cache 的兜底缓存（理由见
        `premium.earnings_state`）。每个 tenor 只读最近两个月分片、**各读一次**（与 `ledger_dates` 同一覆盖范围；
        逐日期调 rows_for_date 会把 MB 级分片重复解析几十次），挑哪一行由 `premium.earnings_observation` 判。"""
        try:
            rows: List[dict] = []
            for tenor in R.LG.TENORS:
                for path in R.LG._shard_paths(tenor, self.ledger_dir)[-2:]:
                    rows.extend(R.LG._load_shard(path)[0])     # 坏行由账本自己的 assess 计数，这里只用好行
        except Exception as exc:  # noqa: BLE001 - 账本读不了如实说，不当成「没有财报」
            _log.warning("[%s] 溢价页读卖权账本财报日失败：%s: %s", t, type(exc).__name__, exc)
            return PM.earnings_state(None, PM._why("scan_ledger_unreadable", type(exc).__name__), as_of=as_of, deps=deps)
        entry = PM.earnings_observation(rows, ticker=self._local_name(t).upper(), lookback=PM.EARNINGS_SCAN_LOOKBACK)
        return PM.earnings_state(entry, None, as_of=as_of, deps=deps)

    def _premium_iv_index(self, t: str, as_of: str, deps) -> dict:
        """IV 历史索引。只读：用 `_read_index`，**不用** `load_iv_history`（后者会写迁移标记）。"""
        import iv_history
        cdir = self._premium_cache_dir()
        name = self._local_name(t)
        if not os.path.exists(iv_history._index_path(name, str(cdir))):
            return PM.iv_rank_block(None, PM._why("no_index_file"), as_of=as_of, deps=deps)
        return PM.iv_rank_block(iv_history._read_index(name, str(cdir)), None, as_of=as_of, deps=deps)

    def _premium_vrp(self, t: str, as_of: str, deps) -> dict:
        """VRP 账本里这只票最新一行。路径 = 数据根下与 `vrp_signal.SIGNALS_FILE` 同一个相对位置（调用时求值；
        那个常量本身在 import 期就冻住了数据根）。坏账本如实报原因，不当成「没有记录」。"""
        import ledger_io
        import vrp_signal
        rel = vrp_signal.SIGNALS_FILE.relative_to(vrp_signal.BASE_DIR)
        try:
            rows, err = ledger_io.load_jsonl(self._premium_home() / rel), None
        except Exception as exc:  # noqa: BLE001 - LedgerCorrupt / OSError：显示原因
            rows, err = None, PM._why("ledger_unreadable", type(exc).__name__)
            _log.warning("[%s] VRP 账本读不了：%s: %s", t, type(exc).__name__, exc)
        return PM.vrp_block(rows, err, ticker=self._local_name(t).upper(), as_of=as_of, deps=deps)

    def _premium_full(self, t: str, force: bool) -> dict:
        with self._lock_for("premium:" + t):
            ent = self._premium_cache.get(t)
            if ent is not None and not force:
                ttl = self.live_ttl if ent[1].get("data_available") else FAIL_TTL_SEC
                if (self.clock() - ent[0]) < ttl:
                    self.stats["premium_cache_hits"] += 1
                    return {**ent[1], "cache_age_sec": round(max(0.0, self.clock() - ent[0]), 1)}
            try:
                raw, reason = self._premium_fetch()(t, as_of=None)
                if raw is None:
                    full = {"data_available": False, "reason": reason or PM._why("fetch_returned_none")}
                else:
                    deps = self._premium_deps()
                    today = self.now_fn().date()
                    as_of = PM.chain_as_of(raw)
                    if self.demo:
                        blocks = PM.unavailable_blocks(PM._why("demo_mode"), ticker=t, as_of=as_of, deps=deps)
                    else:
                        blocks = {"earnings": self._premium_earnings(t, as_of, deps),
                                  "iv_index": self._premium_iv_index(t, as_of, deps),
                                  "vrp": self._premium_vrp(t, as_of, deps)}
                    full = PM.build(raw, bars=self.bars(t), today=today, deps=deps, **blocks)
            except Exception as exc:  # noqa: BLE001 - 算不出来如实说，不给半张表
                _log.warning("[%s] 溢价页计算失败：%s: %s", t, type(exc).__name__, exc)
                full = {"data_available": False, "reason": PM._why("exception", f"{type(exc).__name__}: {exc}")}
            ok = bool(full.get("data_available"))
            self.stats["premium_computed" if ok else "premium_failures"] += 1
            if not ok:
                _log.warning("[%s] 溢价页不可得：%s", t, full.get("reason"))
            self._premium_cache[t] = (self.clock(), full)
            return {**full, "cache_age_sec": 0.0}

    def premium(self, ticker, params: Optional[dict] = None) -> dict:
        """溢价页。params = 查询参数（字符串）；`force=1` ⇒ 重算（不清 CBOE 进程缓存：顶部「重新拉取」先走过
        live(force) 拉了新链，这里复用同一份）；`refresh=1` ⇒ 先清该票的 CBOE 进程缓存再重算——给不经过顶部
        「重新拉取」的盘中自动刷新用，否则 force 只会拿缓存里同一 vintage 日内可活 4 小时的旧链重算一遍。
        只清缓存、不走 live()：后者为了暖缓存要把标的页整套明细（GEX / 梯子 / 怎么卖）算一遍，且它取链失败时
        这里会再取一次、重试翻倍（v0.45.539）。
        参数错（含「链上没有这个到期日」）⇒ BadRequest，带上出错的参数名，前端只撤销那一个。"""
        t = norm_ticker(ticker)
        q = dict(params or {})
        force = str(q.pop("force", "") or "") == "1"
        refresh = str(q.pop("refresh", "") or "") == "1"
        try:
            p = PM.parse_params(q)                       # 先验参数：参数错不该白拉一次链
            if refresh:
                if self.fetch_fn is None:                # 注入的取数函数（演示 / 测试）本来就每次现取，没有这层缓存
                    R._invalidate_cached_payload(t)
                force = True
            out = PM.select(self._premium_full(t, force), p)
        except PM.ParamError as exc:
            raise BadRequest(str(exc), param=exc.param) from exc
        out.update({"ticker": t, "demo": self.demo, "rules": self.premium_rules(), "disclaimer": PM.DISCLAIMER})
        return clean_json(out)

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
