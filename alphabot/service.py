"""Alpha Bot 服务层：前端所需的全部数据都从这里出（v0.45.388）。

**全仓第三个、也是本包唯一一个** import 卖权选择器的模块（`tests/test_sell_strike_integration.py`
的 `ALLOWED_IMPORTERS` 显式登记）。规矩与 MCP 工具相同：

  · **对卖权账本只读**：只调 `sell_strike_report` 的只读函数（现算 / 按日期读行 / 就绪度），
    不调记录、结算、写本地报告；assess 一律 freeze=False——冻结的唯一写者仍是日报钩子。
  · **盲期**：行视图的结算字段、历史水平叠价格，全部由 `sell_strike_report` 按「全部 tenor 冻结」判定，
    本模块不自己判、不自己拼（少一份判据就少一次漂移）。
  · 本模块**自己**写的只有 `PATHS.alphabot_state`（设置、盘中快照与当日原始链存档），调用时求值。

取数慢且串行（每只 ~1.5MB、4–7 秒，进程内同一时刻只有一个外发请求），所以：现算结果按票缓存
`LIVE_TTL_SEC` 秒、同票并发合并成一次；总览读账本的收盘后行，不逐只现拉。
"""
from __future__ import annotations

import gzip
import json
import math
import os
import re
import tempfile
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import sell_strike_report as R
import tail_budget as TB
from alphabot import beta_long as _BL
from alphabot import chat as CH
from alphabot import deep_report as DR
from alphabot import hedge as HG
from alphabot import howto as HW
from alphabot import intraday_activity as IA
from alphabot import intraday_heat as IH
from alphabot import macos_app as MA
from alphabot import money_flow as MF
from alphabot import options_compare as OC
from alphabot import premium as PM
from alphabot import sector_map as SM
from alphabot import price_intraday as PI
from alphabot import straddle as SD
from alphabot import strangle_compare as SC
from alphabot import tail_hedge as TH
from alphabot import volume_board as VB
from alphabot import thesis_auto as TA
from alphabot import wheel as WH
from alphabot import wheel_prices as WP
from alphabot import wheel_screen as WS
from hive_logger import PATHS, get_logger

_log = get_logger("alphabot")

ET = ZoneInfo("America/New_York")
LIVE_TTL_SEC = 60.0
#: 盘中分钟线（v0.45.763）同一只票多久内不重取：盘中略短于页面的刷新间隔（开两个窗口也只发一次）；不在盘中时没有新线可取
MINUTE_TTL_LIVE_SEC = 55.0
MINUTE_TTL_IDLE_SEC = 1800.0
#: 「盯盘」（v0.45.764）：开着盘中图的那一只票每隔这么久拍一张快照（其余关注票仍按 `interval_min`）。
#: CBOE 延迟行情约每分钟更新一次（2026-11-11 从 NVDA / TSLA 各 21 张快照的落后幅度推断），更密拿到的多是同一份
WATCH_INTERVAL_SEC = 60.0
#: 页面每次续期后盯盘再维持多久。页面约每 60 秒续一次；窗口关了 / 切走了没人续，过了这段时间自己停——不靠页面「记得说再见」
WATCH_LEASE_SEC = 150.0
#: 盯盘生效时轮询线程多久醒一次（平时 30 秒一醒，60 秒的节拍会被拉成 60 / 90 秒交替）
WATCH_POLL_SEC = 5.0
#: 失败结果也短暂缓存：CBOE 不可达时别让每次切页都再排 15 秒 × 3 次重试
FAIL_TTL_SEC = 15.0
RAW_KEEP = 4             # 成交榜：内存里最多留几只票的原始链（v0.45.776）
MAX_FOCUS = 5
DEFAULT_FOCUS = ("NVDA", "TSLA", "MSFT", "META", "AMZN")
DEFAULT_INTERVAL_MIN = 10
#: 快照里逐行权价 GEX 只存现价 ±这么多（热力图 / 回看点够用；不存原始链）
SNAPSHOT_STRIKE_BAND = 0.12
# 当日原始链存档（v0.45.815）：每张落盘的快照把这一拍现算用的原始链整份覆盖写到
# `intraday/<日期>/<T>.chain.json.gz` ⇒ 一天只留最后一份（收盘前最后一拍）。快照本身只存汇总值，
# 没有逐合约 OI / IV 就没法做「换掉昨天的 OI 重算 Zero Gamma」这类日间拆解（11-24 NVDA 实测只能用排除法）。
CHAIN_ARCHIVE_SUFFIX = ".chain.json.gz"
#: CBOE 报 `stale_vintage`（文件还停在上一交易日）时，这些票改拉 yfinance（`alphabot.yf_chain`，v0.45.603）。
#: NVDL（2026-10-22）：它的 CBOE 文件整个上午不更新，同一时刻 yfinance 有 15 分钟延迟的成交。
#: BILI（2026-10-27）：CBOE 文件盘中不更新（一个时点实测：12:47 ET 时文件还是昨天 23:22 ET 生成的；刷新时刻待验证），
#: 卖权账本约 20 个扫描日里 15 个记 `stale_vintage`。⚠️ BILI 的 Yahoo 链出过坏数据（10-12 OI 清零、put/call 298.46），
#: 所以备用源对**每一张**链先过 `oi_staleness`、OI 陈旧 / 判不了就拒收（见 `yf_chain` docstring）。要加票改这里；
#: **只对 Alpha Bot 的页面生效**——卖权账本 / 溢价页 / 日报仍只认 CBOE（BILI 在账本里仍会记不可得，这是预注册的数据源，不换）。
#: CBOE 一恢复就自动改回 CBOE。
YF_FALLBACK_TICKERS = frozenset({"NVDL", "BILI"})
#: 「β 窗口对照」里 250 日 β 的进程内缓存时长（秒）：β 一天内几乎不变，取一次要十几个限流请求；「重新拉取」跳过缓存
BETA_TTL_SEC = 6 * 3600.0
#: 这些失败原因是「这只标的本身判不了」（历史不足 / 基准无波动），重取没用：不再重取
PERMANENT_BETA_REASONS = _BL.PERMANENT_REASONS
#: 取数失败但不是限流（网络抖动等）时，隔多久才值得再试（秒）：不让每次打开页面都为同一个失败干等十几秒
BETA_FAIL_BACKOFF_SEC = 30.0
#: Yahoo 限流时给页面的「再试」时刻 = 闸门冷却剩余 + 这点余量（秒）。**不设固定下限**：冷却一结束就能再试，别让人干等
#: （闸门冷却期内任何请求都会被它当场拒绝，等多久完全由冷却决定；此前我加的 60 秒下限与 5 分钟缓存只是多等）
YF_RETRY_MIN_SEC = 5.0
_BETA_SENS_REASONS = {"ledger_unreadable": "对冲账本读不了，没法对照", "no_audit_rows": "还没有带逐行暴露的放行审计"}
#: 指数期权（v0.45.689）：键 = CBOE 文件名（`_SPX` / `_XSP`，裸 `SPX` 在 CBOE 是 403）。`label` 只用于展示；
#: `am_root` = 这条链里**上午结算**的那个根（SPX 月度：到期日开盘按特别开盘价结算，当天盘中仍以 dte=0 留在链里，
#: 已经不是活的 gamma ⇒ `drop_am_settled_expiring` 丢掉）。SPXW / XSP 是下午结算，不动。
#: 只在 Alpha Bot 里存在：不进 WATCHLIST、不进卖权账本 / 预注册（样本定义不变）。
INDEX_TICKERS = {"_SPX": {"label": "SPX", "am_root": "SPX"}, "_XSP": {"label": "XSP", "am_root": None}}
INDEX_ALIASES = {f"{p}{x}": f"_{x}" for x in ("SPX", "XSP") for p in ("", "^", "$")}
INDEX_HOWTO_NOTE = ("指数期权：欧式、现金结算——不会被提前指派，到期按结算价差额现金交割、不会接货或交出股票；"
                    "下面讲「接货 / 持有 100 股 / 提前行权 / 备兑」的步骤不适用。名义 = 指数 × 100 / 张"
                    "（SPX 约为 SPY 的 10 倍；XSP = SPX ÷ 10）。SPX 月度合约在到期日开盘结算，当天不计入。")
_OCC_ROOT = re.compile(r"^([A-Z]+)\d{6}[CP]\d{8}$")
_TICKER_RE = re.compile(r"^[A-Z_][A-Z0-9.\-]{0,9}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BadRequest(ValueError):
    """参数非法（服务端映射成 400）。`param` = 出错的参数名（有就随 400 一起回给前端）。"""

    def __init__(self, msg="", param: Optional[str] = None):
        super().__init__(msg)
        self.param = param


def norm_ticker(t) -> str:
    s = str(t or "").upper().strip()
    s = INDEX_ALIASES.get(s, s)            # SPX / ^SPX / $SPX → _SPX（CBOE 的文件名）
    if not _TICKER_RE.match(s):
        raise BadRequest(f"代码不合法：{t!r}")
    return s


def _chain_source(raw: Optional[dict]) -> Optional[str]:
    """取数源（成交量只在同一源的相邻两拍之间可比）：yfinance 备用链带 `data_source`，CBOE 链不带 ⇒ "cboe"。"""
    if not isinstance(raw, dict):
        return None
    return str(raw.get("data_source") or "cboe")


def activity_gamma(c: dict, S: float) -> Optional[float]:
    """成交活跃度用的逐合约 gamma——与 `sell_strike_levels.strike_profile` **同一条**取数规则：
    CBOE gamma（非 None 且 > 0）优先，否则 BS（要 IV 与 dte）。两边一致由测试按 GEX 重建逐行权价核对。"""
    L = R.L
    g = L._pos(c.get("gamma"))
    if g is not None:
        return g
    K, iv, T = L._pos(c.get("strike")), L._pos(c.get("iv")), L._contract_T(c)
    if K is None or iv is None or T is None:
        return None
    return L.bs_gamma(S, K, T, L.RISK_FREE_RATE, iv)


def drop_am_settled_expiring(raw: dict, am_root: str) -> dict:
    """丢掉上午结算根在到期日（dte == 0）的合约：开盘已按特别开盘价结算，留着会把一份已结算的 OI 当成当天的 gamma。
    返回新 dict（不改入参），丢了几张记在 `n_am_settled_expiry_day_excluded`（0 也写，页面 / 测试能看出这一步跑过）。"""
    kept, n = [], 0
    for c in raw.get("contracts") or []:
        m = _OCC_ROOT.match(str(c.get("symbol") or "")) if isinstance(c, dict) else None
        if m and m.group(1) == am_root and c.get("dte") == 0:
            n += 1
            continue
        kept.append(c)
    return {**raw, "contracts": kept, "n_am_settled_expiry_day_excluded": n}


def vol_block(rows, err: Optional[str], as_of: Optional[str]) -> dict:
    """总览的 IV / RV 列（v0.45.691）：VRP 账本（每日扫描 `vrp_signal.record_day` 写的）里 **as_of 那天**每只票的
    `iv` 与 `rv_30d`——同一天、同一对定义，扫描自己就拿这一对算 `vrp_ex_ante`。不打网、不换别的日期顶替：
    那天没有某票的行 ⇒ 该票不在 `by_ticker` 里（页面显示「—」）；同一天同一票有两行 ⇒ 不猜留哪行，记 `duplicate`。
    账本读不了 ⇒ `available: False` + 原因（不当成「没有记录」）。"""
    out = {"available": False, "reason": None, "date": as_of, "by_ticker": {}}
    if err:
        return out | {"reason": err}
    if not as_of:
        return out | {"reason": "no_as_of"}

    def num(x):
        return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None
    by: Dict[str, dict] = {}
    for r in rows or []:
        if not isinstance(r, dict) or r.get("date") != as_of:
            continue
        t = str(r.get("ticker") or "").upper()
        if not t:
            continue
        if t in by:
            by[t] = {"iv": None, "rv_30d": None, "iv_rv": None, "iv_source": None, "duplicate": True}
            continue
        iv, rv = num(r.get("iv")), num(r.get("rv_30d"))
        by[t] = {"iv": iv, "rv_30d": rv, "iv_rv": round(iv / rv, 3) if iv is not None and rv and rv > 0 else None,
                 "iv_source": r.get("iv_source")}
    return out | {"available": bool(by), "reason": None if by else "vrp_no_rows_for_date", "by_ticker": by}


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


def _lag_sec(fetched_at: Optional[str], last_trade_ts) -> Optional[float]:
    """取数时刻（带时区的文本）− 最后成交（epoch 秒）。判不了 / 为负（钟不同步）⇒ None，不猜。"""
    try:
        lag = datetime.fromisoformat(fetched_at).timestamp() - float(last_trade_ts)
    except (TypeError, ValueError):
        return None
    return round(lag, 0) if lag >= 0 else None


def _is_trading_iso(day: str) -> bool:
    """`YYYY-MM-DD` 是不是交易日（`is_trading_day`）。日历坏了就抛——调用方（`sector_map.lookback_change`）按「判不了、不出数」处理。"""
    import is_trading_day as itd
    return bool(itd.is_trading_day(datetime.fromisoformat(day).date())[0])


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


def _atomic_write_gzip_json(path: Path, obj) -> None:
    """整份覆盖写一个 gzip 压缩的 JSON（临时文件 + os.replace，读者永远看不到写了一半的文件）。
    非 JSON 原生值按 str 落盘（防御：取链结果应当全是 JSON 原生值，万一混进别的类型不让整份存档失败）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as raw_f, gzip.GzipFile(fileobj=raw_f, mode="wb", mtime=0) as f:
            f.write(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def W_CONFIG_UNIVERSE() -> List[str]:
    """接货账本现行名单（`sell_put_wheel.CONFIG`，调用时取，不存模块级常量）。"""
    import sell_put_wheel
    return list(sell_put_wheel.CONFIG["universe"])


def default_watchlist() -> List[str]:
    try:
        from config import WATCHLIST
        return [str(t).upper() for t in WATCHLIST]
    except Exception as exc:  # noqa: BLE001 - 没有 config 就没有默认列表，页面照样能搜代码
        _log.warning("读 config.WATCHLIST 失败：%s", exc)
        return []


def _default_bars(ticker: str) -> dict:
    """日线收盘 + 成交量（价格页用；`volume` 是 v0.45.762 的纯新增，共享入口的行本来就带 `vol`；开高低另由 `AlphaBotService._with_ohlc` 拼）。只走 Twelve Data 共享入口（每日一次、有进程内缓存）；没配 key 就如实说不可得，
    **不**换别的源——别的源（本地价格索引）首次读取会写迁移标记，只读页面不该造成写入。"""
    try:
        import twelve_data
        if not twelve_data.is_configured():
            return {"available": False, "reason": "twelve_data_not_configured", "bars": []}
        rows = twelve_data.fetch_bars(ticker, twelve_data.SHARED_BARS_WINDOW)
        if not rows:
            return {"available": False, "reason": "twelve_data_returned_nothing", "bars": []}
        return {"available": True, "source": "twelve_data",
                "bars": [{"date": str(r.get("date"))[:10], "close": r.get("close"), "volume": r.get("vol")} for r in rows]}
    except Exception as exc:  # noqa: BLE001
        # 自己的码（不叫 exception：溢价页把那个前缀读成「计算出错」，会把取数失败指错层）
        return {"available": False, "reason": f"bars_fetch_exception:{type(exc).__name__}: {exc}", "bars": []}


class AlphaBotService:
    """前端的数据面。`fetch_fn` / `bars_fn` / `clock` 可注入（测试、`--demo`）。"""

    def __init__(self, *, fetch_fn=None, bars_fn: Optional[Callable[[str], dict]] = None,
                 clock: Callable[[], float] = time.monotonic, demo: bool = False,
                 ledger_dir=None, live_ttl: float = LIVE_TTL_SEC, state_dir=None,
                 straddle_root=None, straddle_quote_fn=None, now_fn: Optional[Callable[[], datetime]] = None,
                 premium_root=None, beta_long_fn=None, wheel_prices_fn=None,
                 completed_session_fn: Optional[Callable[[], Optional[str]]] = None, compare_root=None,
                 ohlc_fn=None, minute_fn=None, thesis_fetch_fn=None, thesis_config_path=None,
                 sector_holdings_fn=None, sector_quotes_fn=None, sector_etf_fn=None,
                 flow_stock_fn=None, flow_etf_fn=None, hedge_quotes_fn=None, wheel_quote_fn=None):
        self.fetch_fn = fetch_fn
        self.bars_fn = bars_fn or _default_bars
        # 价格页 K 线的开高低（v0.45.762）：`ohlc_fn(标的) -> ({日期: {open, high, low, close}}, {n, reason, …})`。
        # None 且日线也是缺省源 ⇒ `alphabot.price_ohlc.fetch_ohlc`（yfinance）；注入了 `bars_fn`（测试 / 演示）而没给它 ⇒ 不取，
        # 免得测试里悄悄出网。成功的按 (标的, 美东日) 缓存；失败不入缓存，下次打开再试
        self.ohlc_fn = ohlc_fn
        self._ohlc_default = ohlc_fn is None and bars_fn is None and not demo
        self._ohlc_cache: Dict[tuple, dict] = {}
        # 价格页盘中 1 分钟 K 线（v0.45.763）：`minute_fn(标的, 日期, 美东此刻) -> (分钟线, {n, reason, …})`。缺省接线规则同 `ohlc_fn`
        # （注入了日线而没给它 ⇒ 不取，免得测试出网；演示 ⇒ 合成）。`{(标的, 日期): (取到的时刻, 分钟线, 是否已定, 取的时候在不在盘中)}`
        self.minute_fn = minute_fn
        self._minute_default = minute_fn is None and bars_fn is None and not demo
        self._minute_cache: Dict[tuple, tuple] = {}
        # 深度页证伪条件的自动核对（v0.45.777）：`thesis_fetch_fn(份, 标的, 今天) -> (数据, {reason, …})`。缺省接线规则同 `ohlc_fn`
        # （注入了日线而没给它 ⇒ 不取，免得测试出网；演示 ⇒ 不核对）。`{份的键: (取到的时刻, 数据, meta)}`，成功留
        # `TA.RULES["cache_ttl_sec"]`、失败留 `fail_ttl_sec`（限流时按冷却剩余），免得每次开页都去撞。
        # 配置读代码检出里的 `thesis_breaks_config.json`（与扫描同一份；按 mtime 重读，外部每周任务改了不用重启）
        self.thesis_fetch_fn = thesis_fetch_fn
        self._thesis_default = thesis_fetch_fn is None and bars_fn is None and not demo
        self._thesis_cache: Dict[str, tuple] = {}
        self._thesis_config_path = Path(thesis_config_path) if thesis_config_path else None
        self._thesis_config_cache: Optional[tuple] = None
        self._thesis_lock = threading.Lock()
        # 「板块」页（v0.45.783）：`sector_holdings_fn() -> (成分表, meta)`、`sector_quotes_fn(代码表) -> (报价, meta)`。缺省接线规则同
        # `ohlc_fn`（注入了日线而没给它们 ⇒ 不取，免得测试出网；演示 ⇒ 不取）。成分表落盘在状态目录（重启不用重下 12 份文件），
        # 报价只在内存：`(取到的时刻, 报价, 美东时刻文本)`；`*_retry_at` = 上次失败后最早什么时候再试（self.clock() 时刻）
        # 对冲页「此刻浮动」（v0.45.800）：`hedge_quotes_fn(代码表) -> (报价, meta)`，与板块页同一个 Yahoo 报价接口（一次请求带几只）。
        # 缺省接线规则同 `ohlc_fn`（注入了日线而没给它 ⇒ 不取，免得测试出网；演示 ⇒ 合成报价）。
        # `_hedge_quotes` = `(取到的时刻, {代码: 报价}, 美东时刻文本, 代码表)`；`_hedge_quotes_fail` = `(最早再试时刻, meta)`
        self.hedge_quotes_fn = hedge_quotes_fn
        self._hedge_quotes_default = bars_fn is None and not demo
        self._hedge_quotes: Optional[tuple] = None
        self._hedge_quotes_fail: Optional[tuple] = None
        self._hedge_live_lock = threading.Lock()
        # 接货页「此刻浮动」（v0.45.801）：`wheel_quote_fn(标的, OCC 符号表) -> cboe_options.quote_held 同形`。缺省接线规则同上
        # （注入了日线而没给它 ⇒ 不取）。`_wheel_live` = `{标的: (取到的时刻, 报价, 美东时刻文本, 符号表)}`
        self.wheel_quote_fn = wheel_quote_fn
        self._wheel_quote_default = bars_fn is None and not demo
        self._wheel_live: Dict[str, tuple] = {}
        self._wheel_live_lock = threading.Lock()
        self.sector_holdings_fn = sector_holdings_fn
        self.sector_quotes_fn = sector_quotes_fn
        self._sector_default = bars_fn is None and not demo
        self._sector_holdings: Optional[dict] = None
        self._sector_holdings_note: Optional[dict] = None
        self._sector_holdings_retry_at = 0.0
        self._sector_quotes: Optional[tuple] = None
        self._sector_quotes_fail: Optional[tuple] = None
        # 顶部那排的 5 日 / 1 月（v0.45.792）：`sector_etf_fn(代码表) -> ({代码: {日期: 收盘}}, meta)`，接线规则同上。
        # `_sector_etf` = `(已收盘场次, 日线, meta)`：场次没变就一直用（一天 12 个请求）；有基金没取到的那份与整体失败都过
        # `etf_fail_ttl_sec` 再试
        self.sector_etf_fn = sector_etf_fn
        self._sector_etf: Optional[tuple] = None
        self._sector_etf_note: Optional[dict] = None
        self._sector_etf_retry_at = 0.0
        self._sector_lock = threading.Lock()
        # 「资金」子页（v0.45.795）：`flow_stock_fn(标的, known_market=) -> (逐日行, meta)`（东方财富）、
        # `flow_etf_fn(代码表) -> ({代码: 净值历史}, meta)`（道富）。缺省接线规则同 `sector_*`（注入了日线而没给它们 ⇒ 不取，
        # 免得测试出网；演示 ⇒ 不取）。只在内存：`_flow_stock = {标的: (取到的时刻, 逐日行, meta, 美东时刻文本)}`、
        # `_flow_fail = {标的: (最早再试的时刻, meta)}`、`_flow_market = {标的: 东方财富市场代码}`（试中的记下，下次先试它）；
        # `_flow_etf = (场次, 取到的时刻, 数据, meta, 美东时刻文本)`
        self.flow_stock_fn = flow_stock_fn
        self.flow_etf_fn = flow_etf_fn
        self._flow_default = bars_fn is None and not demo
        self._flow_stock: Dict[str, tuple] = {}
        self._flow_fail: Dict[str, tuple] = {}
        self._flow_market: Dict[str, int] = {}
        self._flow_etf: Optional[tuple] = None
        self._flow_etf_fail: Optional[tuple] = None
        self._flow_lock = threading.Lock()
        # 盯盘（v0.45.764）：`{"ticker", "until"（self.clock() 时刻）}`；同一时刻只盯一只、后来的为准。只在内存里，重启即无。
        # `_watch_consumer`：有没有轮询线程在消费它（演示 / `--no-poll` 没有 ⇒ 续期照收，但如实回「不会拍」）
        self._watch: Optional[dict] = None
        self._watch_consumer = False
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
        self._tail_cache: Dict[tuple, tuple] = {}
        # 「β 窗口对照」（v0.45.662）：长窗口 β 取数函数（None ⇒ `alphabot.beta_long.fetch_long_betas`，yfinance）；按审计日缓存
        self.beta_long_fn = beta_long_fn
        self._beta_cache: Dict[str, dict] = {}
        # 「接货 · 筛选」行情层（v0.45.722）：日线取数函数（None ⇒ `alphabot.wheel_prices.fetch_closes`，yfinance）；
        # `{标的: (取到的时刻, {日期: 收盘}, 取数那一刻的已收盘场次)}`，6 小时内不重取（只补缺的 / 过期的）。
        # 第三项（v0.45.744）：缓存可能是某一场收盘前取的——末根是半根；RV 只认 ≤ 这一场、且末根恰好是它（`sell_put_wheel.rv_asof`）
        self.wheel_prices_fn = wheel_prices_fn
        self.completed_session_fn = completed_session_fn
        self._wheel_closes: Dict[str, tuple] = {}
        # 「对比」页（v0.45.759）：每日扫描结果 `.swarm_results_<日期>.json` 所在目录；None ⇒ 调用时的 PATHS.home。
        # `{日期: ((mtime_ns, size), {标的: 读数})}`——文件没变就不重读（一份近 2 MB，缓存的读数只有几十 KB）
        self.compare_root = compare_root
        self._compare_cache: Dict[str, tuple] = {}
        # 「深度」页（v0.45.760）要原始结果（读数缓存里没有各蜂的明细）：只留最近读的两天 `{日期: ((mtime_ns, size), 原始结果)}`
        self._deep_raw: Dict[str, tuple] = {}
        # Yahoo 限流时各票「最早什么时候值得再试」（self.clock() 的时刻）：`live()` 把它折成 `retry_after_sec` 给页面
        self._yf_retry_until: Dict[str, float] = {}
        self._am_excluded: Dict[str, int] = {}      # 指数票最近一次现算丢掉的上午结算合约数（`_live_fetch` 写）
        # 盘中成交活跃度：各票上一张**落盘**快照的逐合约成交量（只在内存里；重启后第一拍没有可比的，见 intraday_activity）
        self._act_prev: Dict[str, dict] = {}
        # 成交榜（v0.45.776）：最近几次现算实际用到的原始链 `{标的: (取到的时刻, raw)}`——成交榜读它，不另取链
        # （另取会多打一次 CBOE；走 Yahoo 备用源的票还会多挨限流）。一份整链约 1–2 MB ⇒ 只留最近 `RAW_KEEP` 只
        self._raw_last: Dict[str, tuple] = {}
        self._premium_rules_cache: Optional[dict] = None     # 规则常量与文案表进程内不变：算一次
        self._cache: Dict[str, tuple] = {}
        self._bars_cache: Dict[tuple, dict] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._settings_lock = threading.Lock()
        self.stats = {"live_computed": 0, "live_cache_hits": 0, "live_failures": 0,
                      "premium_computed": 0, "premium_cache_hits": 0, "premium_failures": 0,
                      "howto_failures": 0, "yf_fallback_used": 0, "yf_fallback_failed": 0,
                      "beta_sens_computed": 0, "beta_sens_failures": 0, "yf_rate_limited": 0,
                      "activity_failures": 0, "ohlc_failures": 0, "chain_archive_failures": 0,
                      "minute_fetches": 0, "minute_cache_hits": 0, "minute_failures": 0,
                      "thesis_fetches": 0, "thesis_cache_hits": 0, "thesis_failures": 0,
                      "sector_quote_fetches": 0, "sector_cache_hits": 0, "sector_failures": 0,
                      "sector_holdings_fetches": 0, "sector_etf_fetches": 0, "sector_etf_gaps": 0, "sector_quote_fallbacks": 0,
                      "flow_stock_fetches": 0, "flow_etf_fetches": 0, "flow_cache_hits": 0, "flow_failures": 0,
                      "wheel_live_failures": 0}

    # ── 路径（调用时求值）
    def state_dir(self) -> Path:
        return self._state_override or PATHS.alphabot_state

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    # ── 现算
    def live(self, ticker, *, force: bool = False, _on_raw: Optional[Callable] = None) -> dict:
        """`_on_raw`：只给 `take_snapshot` 用——这次现算实际用到的原始链原样交给它（逐合约成交量要与快照同一份链，
        事后再读缓存可能已被别的现算换掉）。传了它就必然现算（`force` 语义）。"""
        t = norm_ticker(ticker)
        force = force or _on_raw is not None
        hit = self._fresh(t)
        if hit is not None and not force:
            self.stats["live_cache_hits"] += 1
            return self._stamp(hit)
        with self._lock_for("live:" + t):
            hit = self._fresh(t)                       # 排队期间别人可能刚算完：同票并发只算一次
            if hit is not None and not force:
                self.stats["live_cache_hits"] += 1
                return self._stamp(hit)
            base_fetch = self._live_fetch(t)
            got_raw = []

            def fetch(ticker, *, as_of=None):
                raw, reason = base_fetch(ticker, as_of=as_of)
                if raw is not None:
                    got_raw.append(True)
                    self._keep_raw(t, raw)
                    if _on_raw is not None:
                        _on_raw(raw)
                return raw, reason
            try:
                res = clean_json(R.compute_live_detail(t, fetch_fn=fetch, state_dir=self.ledger_dir))
            finally:
                # 这次现算没取到链 ⇒ 之前留的链不许冒充这次的（成交榜如实说没有）。取到之前不清：现算进行中别的请求
                # 读到的仍是上一次那份（与此刻 live 缓存里的结果是同一次），清早了只会让它们以为链被挤掉、再多打一次取数
                if not got_raw:
                    with self._locks_guard:
                        self._raw_last.pop(t, None)
            ok = bool(res.get("data_available"))
            ru = self._yf_retry_until.pop(t, None)
            if not ok and res.get("reason") == "yf_rate_limited" and ru is not None:
                res["retry_until_clock"] = ru                # `_stamp` 折成 retry_after_sec（缓存命中时也按「现在」重算）
            if ok:
                res["howto"] = self._howto(t, res)
                if t in INDEX_TICKERS:
                    res["index"] = {"label": INDEX_TICKERS[t]["label"],
                                    "n_am_settled_expiry_day_excluded": self._am_excluded.pop(t, None)}
                    if res["howto"].get("available"):
                        res["howto"]["index_note"] = INDEX_HOWTO_NOTE
            self.stats["live_computed" if ok else "live_failures"] += 1
            if not ok:
                _log.warning("[%s] 现算不可得：%s", t, res.get("reason"))
            self._cache[t] = (self.clock(), res)
            return self._stamp(self._cache[t])

    def _keep_raw(self, t: str, raw: dict) -> None:
        with self._locks_guard:                   # 轮询线程与页面请求会同时现算不同的票
            self._raw_last.pop(t, None)
            self._raw_last[t] = (self.clock(), raw)
            while len(self._raw_last) > RAW_KEEP:
                self._raw_last.pop(next(iter(self._raw_last)))

    def volume_board(self, ticker, params: Optional[dict] = None) -> dict:
        """成交榜（v0.45.776）：同一只票「标的」页现算用的那份链，按当日累计成交量排合约（纯函数 `volume_board`）。
        先走 `live()`（缓存内直接命中，`force=1` 时重拉）——标的页刚算过时不多发请求；现算有结果、链却不在（被别的票
        挤出了 `RAW_KEEP`）⇒ 再强制现算一次。参数错 ⇒ BadRequest（带参数名）。"""
        t = norm_ticker(ticker)
        q = dict(params or {})
        force = str(q.pop("force", "") or "") == "1"
        try:
            p = VB.parse_params(q)                    # 先验参数：参数错不该白拉一次链
        except VB.ParamError as exc:
            raise BadRequest(str(exc), param=exc.param) from exc
        live = self.live(t, force=force)
        ent = self._raw_last.get(t)
        if live.get("data_available") and ent is None:
            live = self.live(t, force=True)
            ent = self._raw_last.get(t)
        if not live.get("data_available"):
            out = {"data_available": False, "reason": live.get("reason"), "fetch_failed": True,
                   "retry_after_sec": live.get("retry_after_sec")}
        else:
            try:
                out = VB.select(VB.build(ent[1] if ent else None), p)
            except VB.ParamError as exc:
                raise BadRequest(str(exc), param=exc.param) from exc
            out["chain_age_sec"] = round(max(0.0, self.clock() - ent[0]), 1) if ent else None
        out.update({"ticker": t, "demo": self.demo, "params": p, "rules": VB.public_rules(),
                    "disclaimer": VB.DISCLAIMER})
        return clean_json(out)

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
        ru = out.pop("retry_until_clock", None)
        if ru is not None:
            out["retry_after_sec"] = round(max(0.0, ru - self.clock()), 0)
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
        # 账本文件不在 ⇒ 报「账本不在」，不是「那天没有行」（load_jsonl 对不存在的文件返回 []）
        rows, err = self._vrp_rows() if self._vrp_path().is_file() else (None, "vrp_ledger_missing")
        out["vol"] = vol_block(rows, err, out.get("as_of"))
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
                "settlement_blinded": True, "state_dir": {"path": None, "exists": False, "hint": "演示模式不读账本"},
                "vol": {"available": False, "reason": "demo", "date": as_of, "by_ticker": {}}}

    def ledger_rows(self, date, *, full: bool = False) -> dict:
        return clean_json(R.rows_for_date_view(norm_date(date), state_dir=self.ledger_dir, compact=not full))

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
            "wheel": {**WH.public_rules(), "screen": WS.public_rules()},
            "compare": OC.public_rules(),
            "deep": DR.public_rules(),
            "volume_board": VB.public_rules() | {"raw_keep": RAW_KEEP},
            "money_flow": MF.public_rules(),
            # 帮助页「程序类」里给人抄的命令：进本代码目录、用运行本服务的解释器（从 .app 打开时就是 .app 记下的那个），
            # 不写死缺省、不假设人站在代码目录里（v0.45.611）
            "app": {"launcher": MA.here_command("-m alphabot.launcher"),
                    "pip_install": MA.here_command("-m pip install -r requirements.txt")},
            "chat": CH.public_rules(),
        })

    # ── 财报跨式账本（v0.45.424，只读）
    def straddle_rules(self) -> dict:
        """跨式账本页与帮助页用的规则常量——取代码里那一份，不在前端另抄。
        懒 import + 兜底：这两个模块 import 时就求值数据根，环境没设好时不能连坐整个 Alpha Bot；取不到就写明原因。"""
        out = {"directional_delta_warn": SD.DIRECTIONAL_DELTA_WARN,
               "ledger_ready_et": SD.LEDGER_READY_ET.isoformat(timespec="minutes"),
               "open_lag_min": SD.OPEN_LAG_MIN}
        try:
            import earnings_vol_signal as evs
            out.update({k: evs.CONFIG.get(k) for k in ("rich_ratio", "cheap_ratio", "max_spread_pct",
                                                       "min_events", "history_n")})
        except Exception as exc:  # noqa: BLE001
            out["signal_rules_error"] = f"{type(exc).__name__}: {exc}"
        try:
            import options_paper_leg as opl
            out.update({k: opl.CONFIG.get(k) for k in ("starting_capital", "risk_per_trade_pct", "max_open",
                                                       "expiry_buffer_days", "trade_cheap",
                                                       "entry_max_sessions_before_event")})
        except Exception as exc:  # noqa: BLE001
            out["ledger_rules_error"] = f"{type(exc).__name__}: {exc}"
        try:
            import straddle_gex_prereg as SP
            out["prereg"] = SP.public_constants()
        except Exception as exc:  # noqa: BLE001
            out["prereg_error"] = f"{type(exc).__name__}: {exc}"
        try:
            import straddle_strangle_shadow as SS
            out["strangle_prereg"] = SS.public_constants()
        except Exception as exc:  # noqa: BLE001
            out["strangle_prereg_error"] = f"{type(exc).__name__}: {exc}"
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

    def _strangle_shadow(self, led: dict) -> dict:
        """影子宽跨记录的进度（v0.45.586，**冻结前只有计数**，见 straddle_strangle_shadow 的盲期规则：任何一笔的影子盈亏 /
        与跨式盈亏的差都不出现在这里）；检验冻结之后多一键 `revealed`（每笔配对 + 冻结的结果，v0.45.599）。"""
        if self.demo:
            return {"available": False, "reason": "演示模式没有影子记录"}
        try:
            import straddle_strangle_shadow as SS
            closed = led.get("closed") or []
            out = SS.progress(closed, led.get("positions") or [], root=self.straddle_root)
            # 冻结之后才有（v0.45.599）：`reveal` 在未冻结 / 冻结文件坏了时返回 None ⇒ 这一键不出现
            revealed = SS.reveal(closed, root=self.straddle_root)
            if revealed is not None:
                out["revealed"] = revealed
            return out
        except Exception as exc:  # noqa: BLE001 - 进度算不出来如实说，不当成「0 个单位」
            _log.warning("影子宽跨记录进度不可得：%s: %s", type(exc).__name__, exc)
            return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    def straddle(self) -> dict:
        now = self.now_fn()
        led = self._straddle_ledger(now)
        return clean_json(SD.build_overview(led, now_et=now, rules=self.straddle_rules(),
                                            shadow=self._straddle_shadow(led.get("signals") or []),
                                            strangle_shadow=self._strangle_shadow(led)))

    # ── 接货型卖 put 纸面账本（v0.45.721，只读）：数据只经账本属主的只读出口 `sell_put_wheel.load_state`
    def wheel(self) -> dict:
        """演示模式不读账本（合成数据不是这本账的记录）；读不了就如实报，不当成空账本。
        懒 import：账本模块 import 时不求值数据根，但环境没设好时也不能连坐整个 Alpha Bot。"""
        rules = WH.public_rules()
        if self.demo:
            return clean_json(WH.build_overview({"available": False, "state_reason": "demo"}, rules=rules))
        try:
            import sell_put_wheel as spw
            state = spw.load_state()
        except Exception as exc:  # noqa: BLE001
            _log.warning("接货型卖 put 账本不可得：%s: %s", type(exc).__name__, exc)
            state = {"available": False, "state_reason": f"{type(exc).__name__}: {exc}"}
        return clean_json(WH.build_overview(state, rules=rules))

    # ── 接货页「此刻浮动」（v0.45.801，只读）：账本持仓 × 现取 CBOE 报价，只显示、不写账本
    #: 同一只票的报价最多用多久（盘中 / 盘外）；「立即刷新」也至少隔 `WHEEL_LIVE_MIN_SEC`。CBOE 文件本身是延迟报价、
    #: 进程缓存 120 秒——这里再短也拿不到更新的数，只会多下载（每只约 1.5MB）
    WHEEL_LIVE_TTL_SEC = 90.0
    WHEEL_LIVE_IDLE_TTL_SEC = 900.0
    WHEEL_LIVE_MIN_SEC = 20.0
    #: 页面盘中自动刷新的间隔（前端读这个，不另抄）
    WHEEL_LIVE_REFRESH_SEC = 120

    def _wheel_quote(self, t: str, symbols: List[str]) -> dict:
        """一只票的此刻报价（`quote_held` 同形）。没有未到期合约、只持股的票走 Yahoo 报价只取标的现价（标明来源）。"""
        if self.wheel_quote_fn is not None:
            return self.wheel_quote_fn(t, symbols)
        if not self._wheel_quote_default:
            return {"available": False, "reason": "no_fetcher", "quotes": {s: None for s in symbols}}
        if symbols:
            import cboe_options
            return {**cboe_options.quote_held(t, symbols), "source": "cboe"}
        q, meta = SM.fetch_quotes([SM.yahoo_symbol(t)])
        row = (q or {}).get(SM.yahoo_symbol(t).upper())
        px = HG._num((row or {}).get("price"))
        if px is None or px <= 0:
            return {"available": False, "reason": (meta or {}).get("reason") or "price_unavailable", "quotes": {}}
        return {"available": True, "reason": None, "underlying_price": px, "quotes": {}, "source": "yahoo_quote",
                "session_live": market_session(self.now_fn())["live"]}

    def wheel_live(self, force: bool = False) -> dict:
        """接货型卖 put 账本此刻的浮动：每只有持仓的票取一份报价（串行），按账本同一口径重算（`alphabot.wheel.live_view`）。
        取不到的票如实写原因，**不拿账本的收盘 mark 顶上**；演示模式不展示（同 `wheel()`）。"""
        now_dt = self.now_fn()
        sess = market_session(now_dt)
        base = {"refresh_sec": self.WHEEL_LIVE_REFRESH_SEC, "session": sess}
        if self.demo:
            return clean_json({**base, "available": False, "state_reason": "demo"})
        try:
            import sell_put_wheel as spw
            state = spw.load_state()
        except Exception as exc:  # noqa: BLE001
            return clean_json({**base, "available": False, "state_reason": f"{type(exc).__name__}: {exc}"})
        want = WH.live_symbols(state)
        quotes, fetched = {}, {}
        with self._wheel_live_lock:
            ttl = self.WHEEL_LIVE_TTL_SEC if sess["live"] else self.WHEEL_LIVE_IDLE_TTL_SEC
            if force:
                ttl = min(ttl, self.WHEEL_LIVE_MIN_SEC)
            for t, syms in want.items():
                ent = self._wheel_live.get(t)
                if not (ent is not None and self.clock() - ent[0] < ttl and set(syms) <= set(ent[3])):
                    try:
                        q = self._wheel_quote(t, syms)
                    except Exception as exc:  # noqa: BLE001 - 报价失败如实返回，不拿账本 mark 顶上
                        q = {"available": False, "reason": f"exception:{type(exc).__name__}", "quotes": {}}
                    if not q.get("available"):
                        _log.warning("[%s] 接货页此刻报价不可得：%s", t, q.get("reason"))
                        self.stats["wheel_live_failures"] += 1
                    # 失败也记下（同样的 TTL）：免得一只票坏了，每次刷新都把它重下一遍
                    ent = self._wheel_live[t] = (self.clock(), q, now_dt.isoformat(timespec="seconds"), list(syms))
                quotes[t] = ent[1]
                fetched[t] = {"fetched_at": ent[2], "age_sec": round(self.clock() - ent[0], 0), "source": ent[1].get("source")}
        body = WH.live_view(state, quotes, today=sess["date_et"])
        return clean_json({**base, **body, "fetched": fetched})

    # ── 深度页证伪条件的自动核对（v0.45.777，只展示）
    def _thesis_config(self) -> Tuple[Optional[dict], Optional[str]]:
        """`(整份配置, 出错原因)`。按 mtime + 大小缓存；读坏了如实回原因（不当成「没有条件」）。"""
        path = self._thesis_config_path
        if path is None:
            import thesis_breaks  # noqa: PLC0415 - 只为拿配置路径（代码检出里的文件，与扫描同一份）
            path = Path(thesis_breaks._CONFIG_JSON_PATH)
        try:
            st = path.stat()
            key = (st.st_mtime_ns, st.st_size)
            if self._thesis_config_cache and self._thesis_config_cache[0] == key:
                return self._thesis_config_cache[1], None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return None, f"{type(e).__name__}"
        if not isinstance(data, dict):
            return None, "not_a_dict"
        self._thesis_config_cache = (key, data)
        return data, None

    def thesis_manual(self, ticker) -> Tuple[List[dict], Optional[str]]:
        """「让 Claude 核对」的条件清单（v0.45.794）：这只票不归机器判据、也不归 `thesis_auto` 的人工条件 → `(条件, 出错原因)`。"""
        from alphabot import thesis_claude as TC  # noqa: PLC0415 - 只有按钮那一页用到
        t = self._local_name(norm_ticker(ticker)).upper()
        cfg, err = self._thesis_config()
        if err:
            return [], err
        block = cfg.get(t)
        auto_ids = {i for (tk, i) in TA.CHECKS if tk == t}
        return TC.manual_conditions(block if isinstance(block, dict) else {}, auto_ids), None

    def _thesis_part(self, part: str, t: str, today) -> Tuple[object, dict]:
        key = part if part in TA.GLOBAL_PARTS else f"{t}:{part}"
        ent = self._thesis_cache.get(key)
        if ent is not None and self.clock() < ent[0]:
            self.stats["thesis_cache_hits"] += 1
            return ent[1], ent[2]
        if self.thesis_fetch_fn is not None:
            fn = self.thesis_fetch_fn
        elif self._thesis_default:
            fn = lambda p, tk, d: TA.fetch_part(p, tk, today=d)  # noqa: E731
        else:
            return None, {"reason": "no_fetcher"}
        self.stats["thesis_fetches"] += 1
        try:
            data, meta = fn(part, t, today)
        except Exception as e:  # noqa: BLE001 - 注入的取数函数也不许把页面打挂
            data, meta = None, {"reason": "fetch_error", "detail": type(e).__name__}
        meta = dict(meta or {})
        if meta.get("reason"):
            self.stats["thesis_failures"] += 1
            ttl = TA.RULES["fail_ttl_sec"]
            if meta["reason"] == "rate_limited" and isinstance(meta.get("retry_after_sec"), (int, float)):
                ttl = float(meta["retry_after_sec"])
        else:
            ttl = TA.RULES["cache_ttl_sec"]
            meta["fetched_at"] = self.now_fn().isoformat(timespec="minutes")
        self._thesis_cache[key] = (self.clock() + ttl, data, meta)
        return data, meta

    # ── 板块热力图（v0.45.783）
    def _sector_holdings_path(self) -> Path:
        return self.state_dir() / "sector_map" / "holdings.json"

    def _sector_load_holdings(self) -> Tuple[Optional[dict], Optional[dict]]:
        """成分表：内存 → 状态目录 → 现取。过期（`holdings_ttl_sec`）才重取；重取失败沿用旧的一份并说明（`note`）。
        返回 `(成分表 | None, 失败说明 | None)`。"""
        now = self.now_fn()
        if self._sector_holdings is None:
            try:
                d = json.loads(self._sector_holdings_path().read_text(encoding="utf-8"))
                if isinstance(d, dict) and isinstance(d.get("constituents"), list) and d["constituents"]:
                    self._sector_holdings = d
            except (OSError, ValueError):
                pass                                    # 没有 / 读不了 = 没缓存，下面现取；取不到会照实报
        cur = self._sector_holdings
        age = None
        if cur is not None:
            try:
                age = (now - datetime.fromisoformat(cur["fetched_at"])).total_seconds()
            except (KeyError, TypeError, ValueError):
                age = None
        # 缺了板块文件的那一份只当临时的：过 `holdings_fail_ttl_sec` 就再取，不等一整天
        ttl = SM.RULES["holdings_fail_ttl_sec"] if (cur or {}).get("failed_sectors") else SM.RULES["holdings_ttl_sec"]
        if cur is not None and age is not None and 0 <= age < ttl:
            return cur, None
        if self.clock() < self._sector_holdings_retry_at:
            return cur, self._sector_holdings_note
        fn = self.sector_holdings_fn or (SM.fetch_holdings if self._sector_default else None)
        if fn is None:
            return cur, {"reason": "no_fetcher"}
        self.stats["sector_holdings_fetches"] += 1
        try:
            data, meta = fn()
        except Exception as e:  # noqa: BLE001 - 注入的取数函数也不许把页面打挂
            data, meta = None, {"reason": "fetch_error", "detail": type(e).__name__}
        if data is None:
            self.stats["sector_failures"] += 1
            self._sector_holdings_retry_at = self.clock() + SM.RULES["holdings_fail_ttl_sec"]
            self._sector_holdings_note = dict(meta or {}) or {"reason": "holdings_unavailable"}
            _log.warning("板块页：持仓文件刷新失败（%s）%s", self._sector_holdings_note,
                         "，沿用旧的一份" if cur is not None else "")
            return cur, self._sector_holdings_note
        data = dict(SM.carry_sectors(data, cur)) | {"fetched_at": now.isoformat(timespec="seconds")}
        self._sector_holdings, self._sector_holdings_note = data, None
        try:
            _atomic_write_json(self._sector_holdings_path(), data)
        except OSError as e:                            # 落不了盘只是下次启动要重下，这一次照常用
            _log.warning("板块页：成分表写不进状态目录（%s）", e)
        return data, None

    def _sector_etf_closes(self) -> Tuple[Optional[dict], Optional[dict]]:
        """SPY + 板块基金的日线：`(日线 | None, 说明 | None)`。按**美东日期**缓存，一天取一次；取不到 / 缺了几只 ⇒ 说明里写原因，
        冷却期内不再去撞（限流时按闸门冷却剩余）。日线没取到不影响色块图，只是那排的 5 日 / 1 月写不出来。

        为什么按日期、不按「最近一场已收盘」（v0.45.797 二次检查）：只用「这一场之前」的收盘，早上取到的那份到收盘后照样够用，
        收盘那一刻不必再取一遍；而每天换一份，除息日当天拿到的就是 Yahoo 已经按这次分红复权过的历史（否则那天的数会少一个股息率）。
        「完整」只认 12 只都在——被限流时回来的半份不许当成完整的（原来只看有没有记 errors，限流那条路上 errors 是空的）。"""
        key = market_session(self.now_fn())["date_et"]
        want = {SM.BENCH, *SM.SECTORS}
        ent, now = self._sector_etf, self.clock()
        if ent is not None and ent[0] == key and want <= set(ent[1]):
            return ent[1], None
        # 手上那份：同一天的（缺了几只）可以拿来补；更早的只在这次整体取不到时顶一下——它少了最近的收盘，
        # 往回数会差一天，由 `lookback_change` 的交易日数核对拦成 `bars_gap`，不会出错数
        cur = ent[1] if ent is not None else None
        same = ent is not None and ent[0] == key
        note = self._sector_etf_note
        if now < self._sector_etf_retry_at:
            return cur, note
        fn = self.sector_etf_fn or (SM.fetch_etf_closes if self._sector_default else None)
        if fn is None:
            return cur, {"reason": "no_fetcher"}
        self.stats["sector_etf_fetches"] += 1
        try:
            data, meta = fn([SM.BENCH, *SM.SECTORS])
        except Exception as e:  # noqa: BLE001 - 注入的取数函数也不许把页面打挂
            data, meta = None, {"reason": "fetch_error", "detail": type(e).__name__}
        meta = dict(meta or {})
        if data is not None and cur and same:           # 这次缺的几只沿用同一天里先前取到的
            data = {**cur, **data}
            meta["errors"] = {s: e for s, e in (meta.get("errors") or {}).items() if s not in data}
        if data is not None and want <= set(data):      # 补齐了：先前那次的限流 / 报错不再算数
            meta["reason"], meta["errors"] = None, {}
        elif data is not None:                          # 没点名的缺口（限流后没去取的那几只）也要写出来
            meta["errors"] = {s: (meta.get("errors") or {}).get(s) or meta.get("reason") or "missing"
                              for s in sorted(want - set(data))}
        bad = data is None or not want <= set(data)
        if bad:
            self.stats["sector_failures"] += 1
            wait = SM.RULES["etf_fail_ttl_sec"]
            if meta.get("reason") == "rate_limited" and isinstance(meta.get("retry_after_sec"), (int, float)):
                wait = float(meta["retry_after_sec"])
            self._sector_etf_retry_at = now + wait
            self._sector_etf_note = meta if (data is None or meta.get("reason")) else {"reason": None, "errors": meta["errors"]}
            _log.warning("板块页：板块基金日线没取全（%s）", meta)
        else:
            self._sector_etf_note = None
        if data is not None:
            self._sector_etf = (key, data, meta)
        return (data if data is not None else cur), self._sector_etf_note

    def _sector_etf_gap_seen(self) -> None:
        """这一份日线算出了 `bars_gap`（往回数的交易日数对不上——多半是取的时候 Yahoo 还没放出最近一场的那根）：
        过冷却期后重取一次，别让那排的 5 日 / 1 月空一整天。真是日历对不上（临时休市）时会每个冷却期重取一次，页面照样写原因。"""
        ent, now = self._sector_etf, self.clock()
        if ent is None or ent[0] is None or now < self._sector_etf_retry_at:
            return
        self._sector_etf = (None, ent[1], ent[2])       # 键作废 ⇒ 冷却期后 `_sector_etf_closes` 会重取；期间沿用这一份
        self._sector_etf_retry_at = now + SM.RULES["etf_fail_ttl_sec"]
        self.stats["sector_etf_gaps"] += 1

    def sector_map(self, refresh: bool = False) -> dict:
        """标普 500 板块热力图：成分与面积取 SPY 持仓文件、板块取 11 只板块 SPDR 的持仓文件、涨跌取 Yahoo 报价
        （`alphabot.sector_map`）。`refresh` 只是「别等缓存到期」——同一份报价 `quote_fail_ttl_sec` 内仍不重取。
        报价刷新失败 ⇒ 沿用上一份并写明（`quotes.refresh_failed` + 真实年龄），一份都没有才 `available=False`。"""
        base = {"available": False, "rules": SM.public_rules(), "disclaimer": SM.DISCLAIMER}

        def fail(reason, **extra):
            return clean_json(base | {"reason": reason, "reason_zh": SM.REASONS.get(reason, reason)} | extra)
        if self.demo:
            return fail("demo")
        with self._sector_lock:
            holdings, hnote = self._sector_load_holdings()
            if holdings is None:
                return fail((hnote or {}).get("reason") or "holdings_unavailable", detail=(hnote or {}).get("detail"))
            # 日线先取、报价后取：日线每天首次要二十多秒，放在报价后面的话，页面拿到的报价一出来就已经旧了二十多秒
            closes, enote = self._sector_etf_closes()
            sess = market_session(self.now_fn())
            ttl = SM.RULES["quote_ttl_live_sec"] if sess["live"] else SM.RULES["quote_ttl_idle_sec"]
            if refresh:
                ttl = min(ttl, SM.RULES["quote_fail_ttl_sec"])
            ent, now = self._sector_quotes, self.clock()
            failed = None
            if ent is not None and now - ent[0] < ttl:
                self.stats["sector_cache_hits"] += 1
            elif self._sector_quotes_fail is not None and now < self._sector_quotes_fail[0]:
                failed = self._sector_quotes_fail[1]
            else:
                fn = self.sector_quotes_fn or (SM.fetch_quotes if self._sector_default else None)
                if fn is None:
                    failed = {"reason": "no_fetcher"}
                else:
                    self.stats["sector_quote_fetches"] += 1
                    try:
                        q, meta = fn(SM.symbols_for(holdings))
                    except Exception as e:  # noqa: BLE001
                        q, meta = None, {"reason": "fetch_error", "detail": type(e).__name__}
                    if q is None:
                        self.stats["sector_failures"] += 1
                        failed = dict(meta or {}) or {"reason": "fetch_error"}
                        wait = SM.RULES["quote_fail_ttl_sec"]
                        if failed.get("reason") == "rate_limited" and isinstance(failed.get("retry_after_sec"), (int, float)):
                            wait = float(failed["retry_after_sec"])
                        self._sector_quotes_fail = (now + wait, failed)
                    else:
                        ent = self._sector_quotes = (now, q, self.now_fn().isoformat(timespec="seconds"))
                        self._sector_quotes_fail = None
                        if (meta or {}).get("fallback"):        # 一次取全没成、退回了分批：请求量变成三倍，要看得见
                            self.stats["sector_quote_fallbacks"] += 1
                            _log.warning("板块页：报价一次取全没成（%s），本进程改为分批取", meta["fallback"])
            if ent is None:
                failed = failed or {"reason": "fetch_error"}
                return fail(failed["reason"], detail=failed.get("detail"), retry_after_sec=failed.get("retry_after_sec"))
            if failed is not None and failed.get("reason") == "rate_limited":
                failed = failed | {"retry_after_sec": max(0.0, round(self._sector_quotes_fail[0] - now, 0))}
            body = SM.build(holdings, ent[1], watchlist=self.watchlist()["tickers"], etf_closes=closes,
                            is_trading=_is_trading_iso)
            blocks = [body["bench"], *(s["etf"] for s in body["sectors"] if s.get("etf"))]
            if any(b.get(f"{SM.PERIODS[k]['field']}_reason") == "bars_gap" for b in blocks for k in SM.ETF_LOOKBACK):
                self._sector_etf_gap_seen()
            return clean_json(base | body | {
                "available": True, "session": sess, "refresh_sec": SM.RULES["quote_ttl_live_sec"],
                "quotes": {"fetched_at": ent[2], "age_sec": round(now - ent[0], 0),
                           # 取到这份报价的那一刻，最晚一笔成交已经过去多久（实测的新旧，不引用接口自报的延迟）
                           "trade_lag_sec": _lag_sec(ent[2], body.get("last_trade_ts")),
                           "chunked": bool(SM._QUOTE_MODE["chunked"]),
                           "refresh_failed": ({**failed, "reason_zh": SM.REASONS.get(failed.get("reason"), failed.get("reason"))}
                                              if failed is not None else None)},
                "etf_bars": {"available": closes is not None, "n": len(closes or {}),
                             "errors": (enote or {}).get("errors") or {},
                             "refresh_failed": ({**enote, "reason_zh": SM.REASONS.get(enote.get("reason"), enote.get("reason"))}
                                                if enote and enote.get("reason") else None)},
                "holdings": {"as_of": holdings.get("as_of"), "fetched_at": holdings.get("fetched_at"),
                             "skipped": holdings.get("skipped") or [], "cash_weight": holdings.get("cash_weight"),
                             "failed_sectors": holdings.get("failed_sectors") or [],
                             "carried": holdings.get("carried") or 0,
                             "multi_sector": holdings.get("multi_sector") or {},
                             "refresh_failed": ({**hnote, "reason_zh": SM.REASONS.get(hnote.get("reason"), hnote.get("reason"))}
                                                if hnote else None)}})

    # ── 「资金」子页（v0.45.795）
    def _flow_stock_part(self, t: str, sess: dict, refresh: bool) -> dict:
        """个股资金流（东方财富）。成功按盘中 / 闲时 TTL 缓存；失败冷却 `stock_fail_ttl_sec` 内不再试（`refresh` 也不破冷却）。
        刷新失败而手上有旧的一份 ⇒ 沿用旧的并写明（`refresh_failed` + 真实年龄），一份都没有才 `available=False`。"""
        def fail(reason, **extra):
            return {"available": False, "reason": reason, "reason_zh": MF.REASONS.get(reason, reason), **extra}
        if t in INDEX_TICKERS:
            return fail("index")
        now = self.clock()
        ttl = MF.RULES["stock_ttl_live_sec"] if sess["live"] else MF.RULES["stock_ttl_idle_sec"]
        ent, failed = self._flow_stock.get(t), None
        if ent is not None and now - ent[0] < ttl and not refresh:
            self.stats["flow_cache_hits"] += 1
        elif t in self._flow_fail and now < self._flow_fail[t][0]:
            failed = self._flow_fail[t][1]
        else:
            fn = self.flow_stock_fn or (MF.fetch_stock_flow if self._flow_default else None)
            if fn is None:
                failed = {"reason": "no_fetcher"}
            else:
                self.stats["flow_stock_fetches"] += 1
                try:
                    rows, meta = fn(t, known_market=self._flow_market.get(t))
                except Exception as e:  # noqa: BLE001 - 注入的取数函数也不许把页面打挂
                    rows, meta = None, {"reason": "fetch_error", "detail": type(e).__name__}
                meta = dict(meta or {})
                if rows:
                    ent = self._flow_stock[t] = (now, rows, meta, self.now_fn().isoformat(timespec="seconds"))
                    self._flow_fail.pop(t, None)
                    if isinstance(meta.get("market"), int):
                        self._flow_market[t] = meta["market"]
                else:
                    self.stats["flow_failures"] += 1
                    failed = meta if meta.get("reason") else {**meta, "reason": "empty"}
                    self._flow_fail[t] = (now + MF.RULES["stock_fail_ttl_sec"], failed)
                    _log.warning("[%s] 资金页：个股资金流取不到（%s）", t, failed)
        if ent is None:
            failed = failed or {"reason": "fetch_error"}
            return fail(failed["reason"], detail=failed.get("detail"))
        body = MF.build_stock(ent[1], today_et=sess["date_et"], live=sess["live"])
        return {"available": True, **body, "market": ent[2].get("market"), "fetched_at": ent[3],
                "age_sec": round(now - ent[0], 0),
                "refresh_failed": ({**failed, "reason_zh": MF.REASONS.get(failed.get("reason"), failed.get("reason"))}
                                   if failed is not None else None)}

    def _flow_etf_part(self, refresh: bool) -> dict:
        """SPY + 11 只板块 SPDR 的申赎（道富净值历史）。按「最近一场已收盘」缓存，同一场内最多 `etf_ttl_sec` 重取一次；
        缺了几只照出其余的并点名（`errors`）；失败冷却 `etf_fail_ttl_sec`。"""
        def fail(reason, **extra):
            return {"available": False, "reason": reason, "reason_zh": MF.REASONS.get(reason, reason), **extra}
        key = self._completed_session() or market_session(self.now_fn())["date_et"]
        now, ent, failed = self.clock(), self._flow_etf, None
        fresh = (ent is not None and ent[0] == key and now - ent[1] < MF.RULES["etf_ttl_sec"]
                 and not (ent[3] or {}).get("errors"))
        if fresh and not refresh:
            self.stats["flow_cache_hits"] += 1
        elif self._flow_etf_fail is not None and now < self._flow_etf_fail[0]:
            failed = self._flow_etf_fail[1]
        else:
            fn = self.flow_etf_fn or ((lambda syms: MF.fetch_etf_navhist(syms, read_rows=SM.read_xlsx_rows))
                                      if self._flow_default else None)
            if fn is None:
                failed = {"reason": "no_fetcher"}
            else:
                self.stats["flow_etf_fetches"] += 1
                try:
                    data, meta = fn(list(MF.ETFS))
                except Exception as e:  # noqa: BLE001
                    data, meta = None, {"reason": "fetch_error", "detail": type(e).__name__}
                meta = dict(meta or {})
                if data:
                    # 这次缺的几只，同一场里先前已取到过 ⇒ 沿用那一份（不然一次临时失败就把已有的数从页面上抹掉）；
                    # 换了场次不沿用：那是别的日子的数。沿用的照样记在 `errors` 里，过冷却期再取一次
                    if ent is not None and ent[0] == key and meta.get("errors"):
                        kept = {s: ent[2][s] for s in meta["errors"] if s in ent[2]}
                        if kept:
                            data = {**data, **kept}
                            meta["kept_from_earlier"] = sorted(kept)
                    ent = self._flow_etf = (key, now, data, meta, self.now_fn().isoformat(timespec="seconds"))
                    self._flow_etf_fail = None
                    if meta.get("errors"):           # 缺几只：照用这一份，但过冷却期再取一次补
                        self.stats["flow_failures"] += 1
                        self._flow_etf_fail = (now + MF.RULES["etf_fail_ttl_sec"], {"reason": None, "errors": meta["errors"]})
                        _log.warning("资金页：板块基金净值历史缺了几只（%s）", meta["errors"])
                else:
                    self.stats["flow_failures"] += 1
                    failed = meta if meta.get("reason") else {**meta, "reason": "fetch_error"}
                    self._flow_etf_fail = (now + MF.RULES["etf_fail_ttl_sec"], failed)
                    _log.warning("资金页：板块基金净值历史取不到（%s）", failed)
        if ent is None:
            failed = failed or {"reason": "fetch_error"}
            return fail(failed["reason"], detail=failed.get("detail"), errors=failed.get("errors") or {})
        body = MF.build_etf(ent[2], is_trading=_is_trading_iso)
        errors = (ent[3] or {}).get("errors") or {}
        return {"available": True, **body, "fetched_at": ent[4], "age_sec": round(now - ent[1], 0),
                "errors": {s: {"code": c, "zh": MF.REASONS.get(str(c).split(":")[0], c)} for s, c in errors.items()},
                "skipped": (ent[3] or {}).get("skipped") or {},
                "kept_from_earlier": (ent[3] or {}).get("kept_from_earlier") or [],
                "refresh_failed": ({**failed, "reason_zh": MF.REASONS.get(failed.get("reason"), failed.get("reason"))}
                                   if failed is not None and failed.get("reason") else None)}

    def money_flow(self, ticker, refresh: bool = False) -> dict:
        """「资金」子页：个股按单笔金额分档的资金流（东方财富）+ 板块 SPDR 申赎（道富）。两半各自取、各自失败（`alphabot.money_flow`）。
        只展示，不进评分。"""
        t = norm_ticker(ticker)
        base = {"ticker": t, "rules": MF.public_rules(), "disclaimer": MF.DISCLAIMER}
        if self.demo:
            off = {"available": False, "reason": "demo", "reason_zh": MF.REASONS["demo"]}
            return clean_json(base | {"stock": off, "etf": off})
        sess = market_session(self.now_fn())
        with self._flow_lock:
            stock = self._flow_stock_part(t, sess, refresh)
            etf = self._flow_etf_part(refresh)
        return clean_json(base | {"session": sess, "stock": stock, "etf": etf})

    def thesis_auto(self, ticker, date: Optional[str] = None) -> dict:
        """深度页「证伪条件」里登记过的几条人工条件的自动核对。只对**最新一天**（取到的是此刻的数据）。
        每条带 `verdict`（`TA` 的判定）、数据来源与取数时刻；没登记的条件不出现（页面照旧写「需人工判断」）。"""
        t = self._local_name(norm_ticker(ticker)).upper()
        base = {"available": False, "ticker": t, "date": None, "checks": [], "rules": TA.public_rules()}
        if self.demo:
            return base | {"reason": "demo", "reason_zh": TA.REASONS["demo"]}
        dates = self.compare_dates()
        latest = dates[-1] if dates else None
        target = norm_date(date) if date else latest
        cfg, err = self._thesis_config()
        if err:
            return base | {"date": target, "reason": "config_unreadable", "reason_zh": f"证伪条件配置读不了（{err}）"}
        matched = TA.match(t, cfg.get(t) if isinstance(cfg.get(t), dict) else {})
        out = base | {"available": True, "date": target, "latest_date": latest,
                      "applicable": latest is not None and target == latest}
        if not matched:
            return clean_json(out)
        today = self.now_fn().date()
        checks = []
        with self._thesis_lock:
            for m in matched:
                spec = m["spec"]
                row = {"id": m["id"], "label": m["label"], "level": m["level"], "trigger": m["trigger"],
                       "source": TA.NEEDS[spec["kind"]][1] if spec else None, "fetched_at": None}
                if not out["applicable"]:
                    row["verdict"] = TA._cant("not_latest_date", f"最新一天是 {latest}")
                elif spec is None:
                    row["verdict"] = TA._cant(m["reason"])
                else:
                    data, fail = {}, None
                    for part in TA.NEEDS[spec["kind"]][0]:
                        v, meta = self._thesis_part(part, t, today)
                        if meta.get("reason"):
                            fail = meta
                            break
                        data[part] = v
                        row["fetched_at"] = meta.get("fetched_at")
                    if fail:
                        wait = fail.get("retry_after_sec")
                        row["verdict"] = TA._cant(fail["reason"], (f"约 {round(wait)} 秒后再试" if isinstance(wait, (int, float))
                                                                    else str(fail.get("detail") or "")))
                    else:
                        try:
                            row["verdict"] = TA.judge(spec, data, today)
                        except Exception as e:  # noqa: BLE001 - 一条判挂了只丢它自己，计数可见
                            self.stats["thesis_failures"] += 1
                            row["verdict"] = TA._cant("fetch_error", f"判定出错 {type(e).__name__}")
                checks.append(row)
        return clean_json(out | {"checks": checks})

    # ── 期权结构跨日对比（v0.45.759，只读）：每日扫描落盘的 `.swarm_results_<日期>.json`，不联网、不写任何东西
    _SWARM_RE = re.compile(r"^\.swarm_results_(\d{4}-\d{2}-\d{2})\.json$")

    def _compare_home(self) -> Path:
        return Path(self.compare_root) if self.compare_root is not None else PATHS.home

    def compare_dates(self) -> List[str]:
        """有扫描结果文件的日期，升序。目录读不了 ⇒ 抛（由接口层报错），不当成「没有扫描」。"""
        home = self._compare_home()
        if not home.is_dir():
            return []
        return sorted(m.group(1) for m in (self._SWARM_RE.match(n) for n in os.listdir(home)) if m)

    def _compare_day(self, date: str):
        """某天全部标的的读数（`OC.views_of`）→ `(结果, 原因)`。读不了 / 不是对象 ⇒ `(None, "file_unreadable:…")`，不当成空的一天。"""
        path = self._compare_home() / f".swarm_results_{date}.json"
        try:
            st = path.stat()
            key = (st.st_mtime_ns, st.st_size)
            ent = self._compare_cache.get(date)
            if ent and ent[0] == key:
                return ent[1], None
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("顶层不是对象")
        except Exception as exc:  # noqa: BLE001 - 原因原样给页面
            _log.warning("扫描结果读不了（%s）：%s: %s", date, type(exc).__name__, exc)
            return None, f"file_unreadable:{type(exc).__name__}"
        day = OC.views_of(raw)
        self._compare_cache[date] = (key, day)
        return day, None

    def _compare_days(self, date: Optional[str]):
        """→ `(升序的 [(日期, 结果)], 目标日期, 全部日期, 读不了的 {日期: 原因})`。只取目标日及其前 `history_days` 个扫描日；
        读不了的日子**不放进序列**（「上一次」会越过它），但记在 `unreadable` 里给页面看。"""
        dates = self.compare_dates()
        if not dates:
            return [], None, dates, {}
        target = norm_date(date) if date else dates[-1]
        window = [d for d in dates if d <= target][-(int(OC.RULES["history_days"]) + 1):]
        days, bad = [], {}
        for d in window:
            day, err = self._compare_day(d)
            if err:
                bad[d] = OC.reason_zh(err)
            else:
                days.append((d, day))
        return days, target, dates, bad

    def _compare_unavailable(self, reason: str, **extra) -> dict:
        return {"available": False, "reason": reason, "reason_zh": OC.reason_zh(reason), "rows": [],
                "rules": OC.public_rules(), "dates": [], "unreadable": {}} | extra

    def compare(self, date: Optional[str] = None) -> dict:
        if self.demo:
            return self._compare_unavailable("demo")
        days, target, dates, bad = self._compare_days(date)
        if not dates:
            return self._compare_unavailable("no_scan_files")
        if target in bad:
            return self._compare_unavailable("file_unreadable", reason_zh=bad[target], date=target,
                                             dates=dates[-90:], unreadable=bad)
        return clean_json(OC.build_overview(days, target) | {"dates": dates[-90:], "unreadable": bad})

    def compare_ticker(self, ticker, date: Optional[str] = None) -> dict:
        t = self._local_name(norm_ticker(ticker)).upper()
        if self.demo:
            return self._compare_unavailable("demo", ticker=t)
        days, target, dates, bad = self._compare_days(date)
        if not dates:
            return self._compare_unavailable("no_scan_files", ticker=t)
        if target in bad:
            return self._compare_unavailable("file_unreadable", reason_zh=bad[target], date=target, ticker=t,
                                             dates=dates[-90:], unreadable=bad)
        return clean_json(OC.build_ticker(t, days, target) | {"dates": dates[-90:], "unreadable": bad})

    # ── 单票深度报告（v0.45.760，只读）：同一批 `.swarm_results_<日期>.json`，排成八个章节；不联网、不调 LLM
    def _scan_raw_day(self, date: str):
        """某天的原始扫描结果 → `(结果, 原因)`。只缓存最近读的两天（一份近 2 MB）；文件变了就重读。"""
        path = self._compare_home() / f".swarm_results_{date}.json"
        try:
            st = path.stat()
            key = (st.st_mtime_ns, st.st_size)
            ent = self._deep_raw.get(date)
            if ent and ent[0] == key:
                return ent[1], None
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("顶层不是对象")
        except Exception as exc:  # noqa: BLE001 - 原因原样给页面
            _log.warning("扫描结果读不了（%s）：%s: %s", date, type(exc).__name__, exc)
            return None, f"file_unreadable:{type(exc).__name__}"
        raw = {str(t).upper(): r for t, r in raw.items()}
        cache = {k: v for k, v in self._deep_raw.items() if k != date}
        while len(cache) >= 2:
            cache.pop(next(iter(cache)))
        cache[date] = (key, raw)
        self._deep_raw = cache
        return raw, None

    def deep(self, ticker, date: Optional[str] = None) -> dict:
        t = self._local_name(norm_ticker(ticker)).upper()
        off = {"available": False, "ticker": t, "date": None, "dates": [], "tickers": [], "rules": DR.public_rules()}

        def unavailable(reason, **extra):
            return off | {"reason": reason, "reason_zh": DR.reason_zh(reason)} | extra
        if self.demo:
            return unavailable("demo")
        dates = self.compare_dates()
        if not dates:
            return unavailable("no_scan_files")
        target = norm_date(date) if date else dates[-1]
        if target not in dates:
            return unavailable("no_such_date", date=target, dates=dates[-90:])
        raw, err = self._scan_raw_day(target)
        if err:
            return unavailable(err, date=target, dates=dates[-90:])
        # 这只票上一次有记录的扫描（v0.45.771，给「做市商与大额成交」做隔日对比）：往回最多找 3 个扫描日，找不到就不比
        cur, rank, names = raw.get(t), DR.rank_of(raw, t), sorted(raw)
        prev, prev_date = None, None
        for d in reversed(dates[: dates.index(target)][-3:]):
            praw, perr = self._scan_raw_day(d)
            if not perr and isinstance(praw.get(t), dict):
                prev, prev_date = praw[t], d
                break
        return clean_json(DR.build(t, target, cur, rank, prev=prev, prev_date=prev_date) | {"dates": dates[-90:], "tickers": names})

    # ── 接货 · 筛选（v0.45.722，只读）：我们手工做过的那张表——30 只标的里「卖 put 好不好做」的客观事实
    WHEEL_CLOSES_TTL = 6 * 3600.0

    def _wheel_ledger(self):
        """卖权账本最近一日的 monthly 行（full 形状：带 put 梯子）。只读梯子与财报日，不碰结算 / 路由。"""
        dates = R.ledger_dates(state_dir=self.ledger_dir, limit=1)
        if not dates:
            return None, {}, "no_ledger_rows"
        v = R.rows_for_date_view(dates[0], state_dir=self.ledger_dir, compact=False)
        rows = {r["ticker"]: r for r in ((v.get("tenors") or {}).get("monthly") or []) if r.get("status") == "recorded"}
        return dates[0], rows, None

    def _wheel_cached_closes(self, t: str):
        ent = self._wheel_closes.get(t)
        if ent and self.clock() - ent[0] < self.WHEEL_CLOSES_TTL and ent[1]:
            return ent[1]
        return None

    def _wheel_cached_completed(self, t: str) -> Optional[str]:
        """这份缓存日线取数那一刻的已收盘场次（判不了 ⇒ None ⇒ `rv_asof` 报 session_unknown，不猜）。"""
        ent = self._wheel_closes.get(t)
        return ent[2] if ent and len(ent) > 2 else None

    def _completed_session(self) -> Optional[str]:
        """最近一个已收盘的交易日（`yf_daily.latest_completed_session`，可注入 `completed_session_fn` 给测试钉钟）。"""
        try:
            if self.completed_session_fn is not None:
                return self.completed_session_fn()
            import yf_daily
            done = yf_daily.latest_completed_session()
            return done.isoformat() if done else None
        except Exception:  # noqa: BLE001 - 判不了 ⇒ None（下游报 session_unknown）
            return None

    def _wheel_universe(self, scope: str):
        st = self.settings()
        watch = list(st.get("wheel_watch") or W_CONFIG_UNIVERSE())
        if scope == "all":
            return default_watchlist(), watch, st
        return watch, watch, st

    def wheel_screen(self, scope: str = "watch") -> dict:
        """离线层：名单（或全部）里每只标的的账本梯子 + 已缓存的行情统计 + 用户的接货价。不联网、秒开。"""
        if scope not in ("watch", "all"):
            raise BadRequest("scope 只接受 watch / all", param="scope")
        rules = WS.public_rules()
        if self.demo:
            return clean_json({"available": False, "state_reason": "demo", "rules": rules})
        tickers, watch, st = self._wheel_universe(scope)
        as_of, rows, err = self._wheel_ledger()
        stats = {}
        for t in tickers:
            c = self._wheel_cached_closes(t)
            if c is not None:
                stats[t] = WS.price_stats(c, as_of)
        accepts = {k: v for k, v in (st.get("wheel_accept") or {}).items()}
        return clean_json({"available": True, "as_of": as_of, "scope": scope, "tickers": tickers, "watch": watch,
                           "wheel_universe": W_CONFIG_UNIVERSE(), "ledger_error": err, "accept": accepts,
                           "rows": WS.build_rows(tickers, rows, stats, accepts, as_of=as_of, in_universe=W_CONFIG_UNIVERSE()),
                           "stats_cached": sorted(stats), "rules": rules})

    def wheel_screen_prices(self, tickers, force: bool = False) -> dict:
        """行情层：补取缺的 / 过期的日线（6 小时缓存），回统计。被限流 ⇒ 带原因与 `retry_after_sec`，已有的照给。"""
        if self.demo:
            return clean_json({"available": False, "state_reason": "demo"})
        ts = list(dict.fromkeys(norm_ticker(t) for t in (tickers or [])))[:max(WS.MAX_WATCH, 40)]
        if not ts:
            raise BadRequest("没有给标的", param="tickers")
        as_of, _rows, _err = self._wheel_ledger()
        need = [t for t in ts if force or self._wheel_cached_closes(t) is None]
        info: Dict[str, dict] = {}
        if need:
            fn = self.wheel_prices_fn or WP.fetch_closes
            completed = self._completed_session()        # 取数**之前**记下：这份日线里哪一场之后的都可能是半根（v0.45.744）
            try:
                got, info = fn(need)
            except Exception as exc:  # noqa: BLE001 - 取数函数不该抛；抛了也如实报，不当空
                got, info = {}, {t: {"n": 0, "reason": f"exception:{type(exc).__name__}"} for t in need}
            for t, c in (got or {}).items():
                if c:
                    self._wheel_closes[t] = (self.clock(), c, completed)
        stats = {}
        for t in ts:
            c = self._wheel_cached_closes(t)
            if c is not None:
                stats[t] = WS.price_stats(c, as_of)
            else:
                why = (info.get(t) or {}).get("reason") or "unavailable"
                stats[t] = {"ok": False, "reason": why if why in WS.REASONS else "unavailable", "detail": why,
                            "retry_after_sec": (info.get(t) or {}).get("retry_after_sec")}
        waits = [s.get("retry_after_sec") for s in stats.values() if s.get("retry_after_sec")]
        return clean_json({"available": True, "as_of": as_of, "stats": stats, "retry_after_sec": max(waits) if waits else None,
                           "fetched": need})

    def wheel_screen_chain(self, ticker) -> dict:
        """取链层：对一只标的现取全链，按**接货账本同一条选合约规则**选那一张，并给净成本 / 占资金 / 权利金重放。"""
        if self.demo:
            return clean_json({"ok": False, "reason": "demo"})
        t = norm_ticker(ticker)
        raw, why = self._live_fetch(t)(t, as_of=None)
        if raw is None:
            return clean_json({"ok": False, "reason": "no_chain", "detail": why, "ticker": t})
        spot = raw.get("underlying_price")
        if not isinstance(spot, (int, float)) or spot <= 0:
            return clean_json({"ok": False, "reason": "no_price", "ticker": t})
        _as_of_ledger, rows, _err = self._wheel_ledger()
        accept = (self.settings().get("wheel_accept") or {}).get(t)
        closes = self._wheel_cached_closes(t)
        out = WS.pick_for_accept(raw["contracts"], float(spot), accept, raw.get("as_of") or self.now_fn().date().isoformat(),
                                 closes=closes, earnings_date=(rows.get(t) or {}).get("earnings_date"),
                                 closes_completed=self._wheel_cached_completed(t))
        out.update({"ticker": t, "chain_as_of": raw.get("as_of"), "session_live": raw.get("session_live"),
                    "price_source": raw.get("underlying_price_source"), "replay_needs_prices": closes is None})
        return clean_json(out)

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
            return pg.public_rules() | {"measurement_start": pg.MEASUREMENT_START}
        except Exception as exc:  # noqa: BLE001
            return {"rules_error": f"{type(exc).__name__}: {exc}"}

    def _stress_fn(self):
        """压力表函数（`portfolio_greeks.stress_table`，纯函数）：「压力模型 vs 实际净值」要用它对跨式腿重算网格。
        取不到就是 None——那一栏只缺压力网格那个预测，别的照给。"""
        try:
            import portfolio_greeks as pg
            return pg.stress_table
        except Exception as exc:  # noqa: BLE001
            _log.warning("压力表函数不可得，模型检验缺压力网格预测：%s: %s", type(exc).__name__, exc)
            return None

    def hedge(self) -> dict:
        now = self.now_fn()
        return clean_json(HG.build_overview(self._hedge_view(now), now_et=now, stress_fn=self._stress_fn()))

    # ── 对冲页「此刻浮动」（v0.45.800，只读）：现取报价 × 账本持仓，只显示、不写账本、不进对冲决定
    #: 同一份报价最多用多久（盘中 / 盘外）；「立即刷新」也至少隔 `HEDGE_LIVE_MIN_SEC` 才真去取（一个按钮连点不该连撞 Yahoo）
    HEDGE_LIVE_TTL_SEC = 30.0
    HEDGE_LIVE_IDLE_TTL_SEC = 600.0
    HEDGE_LIVE_MIN_SEC = 10.0
    HEDGE_LIVE_FAIL_SEC = 60.0
    #: 页面盘中自动刷新的间隔（前端读这个，不另抄）
    HEDGE_LIVE_REFRESH_SEC = 60

    def _demo_hedge_quotes(self, syms, view) -> Dict[str, dict]:
        """演示：拿账本标记价按分钟摆动 ±0.4% 造一份「此刻」报价（确定性、零网络）。"""
        import math
        now = self.now_fn()
        marks = {str(p.get("ticker")).upper(): HG._num(p.get("last_price"))
                 for p in list(view.get("positions") or []) + list(view.get("delta_positions") or []) if isinstance(p, dict)}
        wiggle = 0.004 * math.sin(now.hour * 60 + now.minute + now.second / 60.0)
        return {s: {"price": round(m * (1 + wiggle), 2), "prev_close": m, "time": now.timestamp(), "state": "REGULAR",
                    "delay_min": 0.0} for s, m in marks.items() if s in syms and m}

    def hedge_live(self, force: bool = False) -> dict:
        """对冲账本此刻的浮动盈亏：SPY 覆盖账 + 逐票对冲账的持仓 × 现取报价（`alphabot.hedge.live_marks`）。
        报价取不到 ⇒ 有上一份就沿用并写明 `refresh_failed`（带真实年龄），一份都没有 ⇒ 各行 `no_quote`，**不拿收盘标记价顶上**。"""
        now_dt = self.now_fn()
        sess = market_session(now_dt)
        view = self._hedge_view(now_dt)
        base = {"refresh_sec": self.HEDGE_LIVE_REFRESH_SEC, "demo": self.demo}
        if not view.get("exists"):
            return clean_json({**base, "available": False, "reason": "no_ledger", "session": sess,
                               "errors": dict(view.get("errors") or {})})
        syms = HG.live_symbols(view)
        failed = None
        with self._hedge_live_lock:
            ent, now = self._hedge_quotes, self.clock()
            ttl = self.HEDGE_LIVE_TTL_SEC if sess["live"] else self.HEDGE_LIVE_IDLE_TTL_SEC
            if force:
                ttl = min(ttl, self.HEDGE_LIVE_MIN_SEC)
            fresh = ent is not None and now - ent[0] < ttl and set(syms) <= set(ent[3])
            if not syms:
                ent = None
            elif fresh:
                pass
            elif self._hedge_quotes_fail is not None and now < self._hedge_quotes_fail[0]:
                failed = self._hedge_quotes_fail[1]
            else:
                if self.demo:
                    q, meta = self._demo_hedge_quotes(syms, view), {"reason": None}
                else:
                    fn = self.hedge_quotes_fn or (SM.fetch_quotes if self._hedge_quotes_default else None)
                    if fn is None:
                        q, meta = None, {"reason": "no_fetcher"}
                    else:
                        try:
                            q, meta = fn([SM.yahoo_symbol(x) for x in syms])
                        except Exception as e:  # noqa: BLE001 - 取数函数不该抛；抛了也如实报
                            q, meta = None, {"reason": "fetch_error", "detail": type(e).__name__}
                if q is None:
                    failed = dict(meta or {}) or {"reason": "fetch_error"}
                    wait = self.HEDGE_LIVE_FAIL_SEC
                    if failed.get("reason") == "rate_limited" and isinstance(failed.get("retry_after_sec"), (int, float)):
                        wait = float(failed["retry_after_sec"])
                    self._hedge_quotes_fail = (now + wait, failed)
                    _log.warning("对冲页此刻浮动：报价取不到：%s", failed)
                else:
                    # 报价键换回账本代码：按请求时的映射认（BRK.B ↔ BRK-B），不靠猜「-」原来是不是「.」
                    back = {SM.yahoo_symbol(x).upper(): x for x in syms}
                    q = {back.get(str(k).upper(), str(k).upper()): v for k, v in q.items()}
                    ent = self._hedge_quotes = (now, q, now_dt.isoformat(timespec="seconds"), list(syms))
                    self._hedge_quotes_fail = None
            if failed is not None and failed.get("reason") == "rate_limited" and self._hedge_quotes_fail is not None:
                failed = failed | {"retry_after_sec": max(0.0, round(self._hedge_quotes_fail[0] - now, 0))}
        body = HG.live_marks(view, ent[1] if ent else None, session=sess, tz=ET)
        return clean_json({**base, **body, "available": True,
                           "quotes": {"source": "demo" if self.demo else "yahoo_quote",
                                      "fetched_at": ent[2] if ent else None,
                                      "age_sec": round(self.clock() - ent[0], 0) if ent else None,
                                      "refresh_failed": ({**failed, "reason_zh": SM.REASONS.get(failed.get("reason"), failed.get("reason"))}
                                                         if failed is not None else None)}})

    # ── 尾部对冲候选（v0.45.643，只读）：SPY 认沽后比例价差叠到组合压力网格上
    def _tail_deps(self) -> "TH.Deps":
        """利率取 `portfolio_greeks.CONFIG`（与基线压力表同一个）、BS 价取 `greeks_engine`（压力表重定价用的同一个）、
        点差闸门取卖权 / 跨式用的同一把尺——候选的数与基线对得上。懒 import：环境没设好不连坐整个 Alpha Bot。"""
        import portfolio_greeks as pg
        from greeks_engine import bs_price
        return TH.Deps(bs_price=bs_price, risk_free=float(pg.CONFIG["risk_free"]), max_spread_pct=R.C.MAX_SPREAD_PCT)

    def _tail_chain(self, day: str, spot=None, *, force: bool = False):
        """SPY 全链。演示 = 合成链（补上 vega / theta，现货取审计里的 SPY 价，免得演示永远带「现货有差」）；
        生产 = Alpha Bot 的取链入口。`force`（页面「重新拉取」）**清 SPY 的 CBOE 进程缓存再拉**：该缓存同一 vintage 日内
        最长新鲜 4 小时，不清的话「重新拉取」会原样命中几小时前的 bid/ask（独立审查实测）；平常的打开 / 轮询不清、不多发请求。"""
        if self.demo:
            from alphabot import synthetic as syn
            return syn.demo_spy_chain(day, spot=spot), None
        return self._chain_fetch(invalidate=force)("SPY", as_of=None)

    def tail_hedge(self, force: bool = False) -> dict:
        """「尾部对冲候选」：对冲账本**最近一份放行的审计**（压力网格 / Greeks / NAV）× SPY 当前期权链。
        **只读、不写账本、没有推荐**（见 `alphabot.tail_hedge`）。取链失败如实带原因返回，不拿旧链顶上。"""
        now = self.now_fn()
        view = self._hedge_view(now)
        audits = ((view.get("history") or {}).get("audits")) or {}
        day = max(audits) if audits else None
        if day is None and view.get("errors"):
            # 账本读不了 ≠ 还没有记录（`_hedge_view` 自己的注释：读不了不能当成空账本）；不去取链、不缓存
            res = clean_json(TH.unavailable("ledger_unreadable", ledger_errors={k: str(v) for k, v in view["errors"].items()}))
            res["demo"] = self.demo
            res["meta"] = TH.public_rules()
            return res
        key = ("tail", day)
        with self._lock_for("tail"):
            ent = self._tail_cache.get(key)
            if ent is not None and not force and (self.clock() - ent[0]) < self.live_ttl:
                return {**ent[1], "cache_age_sec": round(self.clock() - ent[0], 1)}
            chain, reason = None, None
            if day is not None:
                try:
                    chain, reason = self._tail_chain(day, (audits.get(day) or {}).get("spy_price"), force=force)
                except Exception as exc:  # noqa: BLE001 - 取链失败如实说，不吞成「没有候选」
                    reason = f"exception:{type(exc).__name__}"
                    _log.warning("尾部对冲：SPY 期权链取不到：%s: %s", type(exc).__name__, exc)
            try:
                res = TH.build(day, audits.get(day), chain, self._tail_deps())
            except Exception as exc:  # noqa: BLE001 - 算不出就写明原因；页面其余部分不受影响
                _log.warning("尾部对冲：候选计算失败：%s: %s", type(exc).__name__, exc)
                res = TH.unavailable(f"exception:{type(exc).__name__}")
            if not res.get("available") and chain is None and reason:
                res["chain_reason"] = reason
            res = clean_json(res)
            res["demo"] = self.demo
            res["meta"] = TH.public_rules()          # 原因 / 旗标 / 提醒文案：前端只查这张表，不另抄
            self._tail_cache[key] = (self.clock(), res)
            return {**res, "cache_age_sec": 0.0}

    # ── β 窗口对照（v0.45.662，只展示）：压力网格的 60 日 β 与长窗口 β 并排
    def beta_sensitivity(self, force: bool = False) -> dict:
        """对冲账本**最近一份放行的审计**，用审计里的 60 日 β（现状）与 250 日 β 各算一遍压力网格 / 尾部归因（`alphabot.hedge.beta_sensitivity`）。
        **只读、不写审计 / 账本、不进告警、不改任何成交**；250 日 β 现取（yfinance，按审计日缓存 `BETA_TTL_SEC`），所以单独一个接口——
        取 β 慢（限流下约半分钟）不能拖住对冲页。取不到的标的沿用 60 日 β 并如实标出（混合口径），一个都取不到就写原因，不拿 60 日冒充对照。"""
        now = self.now_fn()
        view = self._hedge_view(now)
        audits = ((view.get("history") or {}).get("audits")) or {}
        day, audit = HG.beta_audit(audits)
        if audit is None:
            reason = "ledger_unreadable" if view.get("errors") else "no_audit_rows"
            return clean_json({**TB._sens_unavailable(reason), "date": None, "demo": self.demo,
                               "meta": self.beta_sens_meta(), "reason_zh": TB.REASONS.get(reason) or _BETA_SENS_REASONS.get(reason, reason)})
        with self._lock_for("beta_sens"):
            want = HG.beta_tickers(audit)
            now = self.clock()
            ent = self._beta_cache.setdefault(day, {"betas": {}, "info": {}, "at": {}, "retry_at": None})
            # 缓存按**标的**记：值、取到的时刻、最近一次的明细（成功 reason=None / 失败写原因）、绝对的「最早再试时刻」。
            # 谁要取：① 没值、且不是「永久取不到」（历史不足 / 基准无波动）；② 有值但已超过 BETA_TTL_SEC。
            # 好值的寿命只看它自己的 `at`——别的标的被重取不会给它续命；永久失败的标的不再每次重取（也不会让 SPY 被多取一次）。
            def _permanent(t):
                return ((ent["info"].get(t) or {}).get("reason")) in PERMANENT_BETA_REASONS

            def _due(t):
                if ent["betas"].get(t) is not None:
                    return now - ent["at"].get(t, now) >= BETA_TTL_SEC
                return not _permanent(t)
            todo = [t for t in want if _due(t)]
            if force and not todo:
                todo = [t for t in want if not _permanent(t)]             # 用户点「重新取 β」且什么都不缺：全部刷新
            cooling = ent["retry_at"] is not None and now < ent["retry_at"]
            if todo and not cooling:
                new_betas, new_info = self._fetch_long_betas(day, todo)
                new_info = new_info or {}
                waits, transient = [], False
                for t in todo:
                    nb, ni = new_betas.get(t), (new_info.get(t) or {})
                    if nb is not None:
                        ent["betas"][t], ent["at"][t], ent["info"][t] = nb, now, ni or {"n": 0, "reason": None}
                    else:
                        # 刷新失败：**保留旧值**（和它自己的 `at`，页面照实显示「N 分钟前取的」），但把失败写进明细——不假装刚取到
                        ent["betas"].setdefault(t, None)
                        ent["info"][t] = ni or {"n": 0, "reason": "no_value"}
                        reason = ent["info"][t].get("reason")
                        if reason == "rate_limited":
                            waits.append(float(ni.get("retry_after_sec") or YF_RETRY_MIN_SEC))
                        elif reason not in PERMANENT_BETA_REASONS:
                            transient = True
                ent["retry_at"] = (now + max(waits) if waits else (now + BETA_FAIL_BACKOFF_SEC if transient else None))
            betas = {t: ent["betas"].get(t) for t in want}
            info = {t: ent["info"][t] for t in want if t in ent["info"]}
            used_at = [ent["at"][t] for t in want if betas.get(t) is not None and t in ent["at"]]
            age = max(0.0, now - min(used_at)) if used_at else 0.0           # 用到的值里**最老**的那个有多老
            retry_after = max(0.0, ent["retry_at"] - now) if ent["retry_at"] is not None else None
            try:
                res = HG.beta_sensitivity(audits, betas, self._stress_fn(), info=info, retry_after_sec=retry_after)
                self.stats["beta_sens_computed"] += 1
            except Exception as exc:  # noqa: BLE001 - 算不出就写明原因，对冲页其余部分不受影响
                self.stats["beta_sens_failures"] += 1
                _log.warning("β 窗口对照：计算失败：%s: %s", type(exc).__name__, exc)
                res = {**TB._sens_unavailable(f"exception:{type(exc).__name__}"), "date": day}
            res = clean_json(res)
            res["demo"] = self.demo
            res["meta"] = self.beta_sens_meta()
            res["cache_age_sec"] = round(age, 1)
            return res

    @staticmethod
    def _yf_wait_sec() -> float:
        """Yahoo 限流后「最少等多久再试」：闸门冷却剩余，至少 `YF_RETRY_MIN_SEC`（冷却刚结束就再撞，大概率还是 429）。"""
        try:
            import yf_gate
            return float(yf_gate.retry_after_sec(YF_RETRY_MIN_SEC))
        except Exception:  # noqa: BLE001
            return YF_RETRY_MIN_SEC

    def _fetch_long_betas(self, day: str, tickers: List[str]):
        """`(β 表, 明细)`。演示 = 合成；注入的函数原样用；生产 = `alphabot.beta_long.fetch_long_betas`。永不抛。"""
        try:
            if self.beta_long_fn is not None:
                out = self.beta_long_fn(tickers, day)
                return (out if isinstance(out, tuple) else (out, {}))
            if self.demo:
                from alphabot import synthetic as syn
                return syn.demo_long_betas(tickers), {}
            from alphabot import beta_long
            return beta_long.fetch_long_betas(tickers, day)
        except Exception as exc:  # noqa: BLE001
            self.stats["beta_sens_failures"] += 1
            _log.warning("β 窗口对照：长窗口 β 取数失败：%s: %s", type(exc).__name__, exc)
            return {t: None for t in tickers}, {t: {"n": 0, "reason": f"exception:{type(exc).__name__}"} for t in tickers}

    def beta_sens_meta(self) -> dict:
        """前端文案 / 常量（不在前端另抄）：窗口名、「差异明显」判据、原因码中文。"""
        return {"alt_label": "250 日", "base_label": "60 日", "beta_gap_min": TB.BETA_GAP_MIN, "reasons": {**TB.REASONS, **_BETA_SENS_REASONS}}

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
        定仓用账本自己的 `options_paper_leg.size_contracts`（懒 import；取不到就写明原因、张数留空）；NAV 取账本当日
        **开仓前**的净值（`SD.sizing_nav`，v0.45.593：入场日那行净值是开仓之后写的，直接用会少 1 张）。"""
        t = norm_ticker(ticker)
        now = self.now_fn()
        if self.demo:
            from alphabot import synthetic as syn
            led = self._straddle_ledger(now)
            day = norm_date(as_of) if as_of else (SD.ledger_as_of(led) or now.date().isoformat())
            qs, raw = syn.demo_compare_inputs(t, day)
            qs_err = hist_err = None
            sig, nav, nav_info = SD.pick_signal_and_nav(led.get("signals"), led.get("equity"), t, day,
                                                        SD.entries_on(day, led.get("positions"), led.get("closed")))
        else:
            root = self.straddle_root
            if as_of:
                day = norm_date(as_of)
            else:
                meta, _ = SD.read_json((Path(root) if root is not None else PATHS.home) / SD.LEDGER_DIRNAME / "meta.json")
                day = str((meta or {}).get("last_run_date") or SD.expected_ledger_date(now)[0])
            qs, qs_err = SD.load_quote_set(t, day, root)
            raw, hist_err = SD.load_hist_moves(t, root)
            sig, nav, nav_info = SD.load_signal_and_nav(t, day, root)

        def size_fn(premium, nav_):
            try:
                import options_paper_leg as opl
                return opl.size_contracts("short", premium, nav_)
            except Exception as exc:  # noqa: BLE001 - 定仓函数取不到：张数留空并说明，不编一个
                return 0, f"sizing unavailable: {type(exc).__name__}"
        res = clean_json(SC.compare(t, day, qs=qs, qs_error=qs_err, raw_moves=raw, hist_error=hist_err, signal=sig,
                                    nav=nav, nav_info=nav_info, size_fn=size_fn,
                                    max_spread_frac=self.straddle_rules().get("max_spread_pct")))
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

    def _live_fetch(self, t: str):
        """现算用的取链：`_chain_fetch` + 指数链的上午结算根过滤（只对 `INDEX_TICKERS` 里有 `am_root` 的票）。"""
        base = self._chain_fetch(invalidate=True)
        am_root = (INDEX_TICKERS.get(t) or {}).get("am_root")
        if not am_root:
            return base

        def fetch(ticker, *, as_of=None):
            raw, reason = base(ticker, as_of=as_of)
            if raw is None:
                return None, reason
            raw = drop_am_settled_expiring(raw, am_root)
            self._am_excluded[t] = raw["n_am_settled_expiry_day_excluded"]
            return raw, reason
        return fetch

    def _chain_fetch(self, *, invalidate: bool):
        """Alpha Bot 取链的唯一入口：注入的取数函数（演示 / 测试）原样用；生产 = CBOE（与卖权页同一个取数函数），
        CBOE 报 `stale_vintage` 且该票在 `YF_FALLBACK_TICKERS` 里 ⇒ 改拉 yfinance（`alphabot.yf_chain`）。
        `invalidate`：先清该票的进程内 CBOE 缓存（`compute_live_detail` 缺省取数的行为：不清会拿 4 小时内的旧链重算）。
        失败如实带原因返回（页面显示「现算不可得：…」+ `stats.yf_fallback_failed`），不吞。"""
        if self.fetch_fn is not None:
            return self.fetch_fn

        def fetch(ticker, *, as_of=None):
            t = norm_ticker(ticker)
            if invalidate:
                R._invalidate_cached_payload(t)
            raw, reason = R.LG._default_fetch(t, as_of=as_of)
            if raw is None and reason == "stale_vintage" and t in YF_FALLBACK_TICKERS:
                from alphabot import yf_chain
                raw, reason = yf_chain.fetch_yf_raw_contracts(t, as_of=as_of)
                self.stats["yf_fallback_used" if raw is not None else "yf_fallback_failed"] += 1
                if raw is None and reason == "yf_rate_limited":
                    # 限流 ≠ 取数坏了：算出「最早什么时候值得再试」交给页面（自动重试 / 文案），并单独计数
                    wait = self._yf_wait_sec()
                    self.stats["yf_rate_limited"] += 1
                    self._yf_retry_until[t] = self.clock() + wait
                    _log.warning("[%s] CBOE 陈旧、yfinance 备用源被 Yahoo 限流，约 %.0f 秒后再试（本进程累计限流 %d 次）",
                                 t, wait, self.stats["yf_rate_limited"])
                elif raw is None:
                    _log.warning("[%s] CBOE 陈旧、yfinance 备用源也取不到：%s", t, reason)
                else:
                    self._yf_retry_until.pop(t, None)
                    chk = raw.get("oi_check") or {}
                    _log.info("[%s] CBOE 文件陈旧（stale_vintage），本次改用 yfinance 备用源（OI 占比 %s，%s / %s 个合约无可用 IV）",
                              t, chk.get("ratio"), raw.get("n_iv_missing"), len(raw.get("contracts") or []))
            return raw, reason
        return fetch

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

    def _vrp_path(self) -> Path:
        """VRP 账本路径 = 数据根下与 `vrp_signal.SIGNALS_FILE` 同一个相对位置（调用时求值；那个常量本身在 import 期
        就冻住了数据根）。"""
        import vrp_signal
        return self._premium_home() / vrp_signal.SIGNALS_FILE.relative_to(vrp_signal.BASE_DIR)

    def _vrp_rows(self):
        """VRP 账本全部行 → `(rows, err)`。坏账本如实报原因，不当成「没有记录」。溢价页与总览共用。
        ⚠️ 文件不存在时 `load_jsonl` 返回 []（溢价页沿用这个口径，测试钉住）；总览自己另判文件在不在。"""
        import ledger_io
        try:
            return ledger_io.load_jsonl(self._vrp_path()), None
        except Exception as exc:  # noqa: BLE001 - LedgerCorrupt / OSError：显示原因
            _log.warning("VRP 账本读不了：%s: %s", type(exc).__name__, exc)
            return None, PM._why("ledger_unreadable", type(exc).__name__)

    def _premium_vrp(self, t: str, as_of: str, deps) -> dict:
        """VRP 账本里这只票最新一行（`_vrp_rows` 读）。"""
        rows, err = self._vrp_rows()
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
                am_root = (INDEX_TICKERS.get(t) or {}).get("am_root")
                if raw is not None and am_root:              # 与 live() 同一条规则：已结算的上午结算根不进溢价排序
                    raw = drop_am_settled_expiring(raw, am_root)
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
            return clean_json({**self._with_ohlc(t, key, self._bars_cache[key]), "ticker": t})

    def _with_ohlc(self, t: str, key: tuple, res: dict) -> dict:
        """给日线拼上开 / 高 / 低（纯新增字段 + `ohlc` 说明块）。取不到不影响日线本身：`ohlc.available=False` + 原因，
        页面退回「前收 → 收盘」的涨跌柱并照实写；失败计 `stats.ohlc_failures`。"""
        bars = res.get("bars") or []
        if any(isinstance(r, dict) and r.get("open") is not None for r in bars):
            return {**res, "ohlc": {"available": True, "source": res.get("source"), "matched": len(bars)}}   # 日线源自带（演示）
        fn = self.ohlc_fn
        if fn is None:
            if not self._ohlc_default:
                return {**res, "ohlc": {"available": False, "reason": "not_wired"}}
            from alphabot import price_ohlc
            fn = price_ohlc.fetch_ohlc
        from alphabot import price_ohlc as PO
        if key not in self._ohlc_cache:
            try:
                rows, info = fn(t)
            except Exception as exc:  # noqa: BLE001 - 注入的函数抛了也不能拖垮日线
                rows, info = {}, {"n": 0, "reason": f"exception:{type(exc).__name__}"}
            if not rows:
                self.stats["ohlc_failures"] += 1
                return {**res, "ohlc": {"available": False, "source": PO.SOURCE, **info}}
            self._ohlc_cache[key] = rows
        merged, n = PO.merge(bars, self._ohlc_cache[key])
        ok = n["matched"] > 0
        if not ok:
            self.stats["ohlc_failures"] += 1
        return {**res, "bars": merged,
                "ohlc": {"available": ok, "source": PO.SOURCE, "close_tol": PO.CLOSE_TOL, **n,
                         **({} if ok else {"reason": "no_bar_matched"})}}

    def minute_bars(self, ticker, date_et: Optional[str] = None) -> dict:
        """某票某个美东日的 1 分钟 K 线 + 同一天盘中快照里的水平路径（**只给单日**，理由同 `intraday`）。

        取数失败不入缓存；手里有上一次取到的 ⇒ 照旧给出并写明 `refresh_failed`（图不清空），没有 ⇒ `available=False` + 原因。
        过去的日子取到一次就定了；今天的按 `MINUTE_TTL_*` 限频——页面刷得再勤，发出去的请求也不会比这更密。"""
        t = norm_ticker(ticker)
        now = self.now_fn()
        today = now.date().isoformat()
        d = norm_date(date_et) if date_et else today
        live_now = d == today and market_session(now)["live"]
        out = {"ticker": t, "date": d, "is_today": d == today, "source": PI.SOURCE, "interval": PI.INTERVAL,
               "session_live": live_now, "refresh_sec": PI.REFRESH_SEC if live_now else None,
               "level_path": PI.level_path(self.intraday(t, d).get("snapshots") or [], d),
               "reason_labels": PI.REASONS, "max_age_days": PI.MAX_AGE_DAYS}
        fn = self.minute_fn
        if fn is None:
            if self.demo:
                from alphabot import synthetic as syn

                def fn(tk, day, when):
                    rows, dropped = PI.drop_forming(syn.demo_minutes(tk, day), day, when)
                    return rows, {"n": len(rows), "reason": None if rows else "unavailable", "dropped_forming": dropped}
                out["source"] = "demo"
            elif self._minute_default:
                def fn(tk, day, when):
                    return PI.fetch_minutes(tk, day, now=when)
            else:
                return clean_json({**out, "available": False, "reason": "not_wired", "bars": []})
        key = (t, d)
        with self._lock_for("minute:" + t):
            hit = self._minute_cache.get(key)
            ttl = MINUTE_TTL_LIVE_SEC if live_now else MINUTE_TTL_IDLE_SEC
            # 盘中取的那份到收盘后就不完整了（缺收盘前最后几根）：时段一结束必须重取一次，不能套「不在盘中 30 分钟内不重取」
            went_idle = bool(hit) and hit[3] and not live_now
            if hit and (hit[2] or (not went_idle and self.clock() - hit[0] < ttl)):
                self.stats["minute_cache_hits"] += 1
                return clean_json({**out, "available": True, "reason": None, "bars": hit[1]})
            self.stats["minute_fetches"] += 1
            try:
                rows, info = fn(t, d, now)
            except Exception as exc:  # noqa: BLE001 - 注入的函数抛了也要带原因返回
                rows, info = [], {"n": 0, "reason": "exception", "detail": type(exc).__name__}
            if rows:
                self._minute_cache[key] = (self.clock(), rows, d < today, live_now)
                return clean_json({**out, "available": True, "reason": None, "bars": rows,
                                   "dropped_forming": info.get("dropped_forming", 0)})    # 成交量异常的根数页面自己数（缓存命中时也要有）
            self.stats["minute_failures"] += 1
            fail = {k: info[k] for k in ("reason", "retry_after_sec", "detail") if info.get(k) is not None}
            if hit:      # 刷新失败：上一次取到的照旧画，页面写明它停在哪
                return clean_json({**out, "available": True, "reason": None, "bars": hit[1], "refresh_failed": fail})
            return clean_json({**out, "available": False, "bars": [], **fail})

    # ── 盯盘（v0.45.764）
    def watch_ticker(self) -> Optional[str]:
        """此刻被盯的那只票（租约过期 ⇒ None）。"""
        w = self._watch
        return w["ticker"] if w and self.clock() < w["until"] else None

    def watch(self, ticker) -> dict:
        """续期盯盘：这只票在租约内每 `WATCH_INTERVAL_SEC` 拍一张快照。返回它**会不会真的被拍**与原因——
        续上了但没人拍（演示 / 没开轮询 / 盘中快照被关 / 不在交易时段）不能回成「盯上了」。"""
        t = norm_ticker(ticker)
        base = {"ticker": t, "interval_sec": WATCH_INTERVAL_SEC, "lease_sec": WATCH_LEASE_SEC}
        if self.demo:
            return {**base, "active": False, "reason": "demo"}
        if t.startswith("_"):
            return {**base, "active": False, "reason": "index_not_supported"}     # 指数一张链几十 MB，不逐分钟拉
        if t in YF_FALLBACK_TICKERS:
            # 这些票 CBOE 盘中是陈旧的，现算会改走 Yahoo 的期权链（一次十来个请求）：每分钟一拍很容易把 Yahoo 打到限流，
            # 一限流 Alpha Bot 里所有走 Yahoo 的功能一起停 180 秒。它们留在原来的间隔上
            return {**base, "active": False, "reason": "yf_fallback_ticker"}
        prev = self.watch_ticker()
        self._watch = {"ticker": t, "until": self.clock() + WATCH_LEASE_SEC}
        out = {**base, "replaced": prev if prev and prev != t else None}
        if not self._watch_consumer:
            return {**out, "active": False, "reason": "no_poller"}
        if not self.settings().get("poll_enabled"):
            return {**out, "active": False, "reason": "poll_disabled"}
        if not market_session(self.now_fn())["live"]:
            return {**out, "active": False, "reason": "session_closed"}
        return {**out, "active": True, "reason": None}

    # ── 设置（自选 / 盘中关注列表）
    def _settings_path(self) -> Path:
        return self.state_dir() / "settings.json"

    def settings(self) -> dict:
        # chat_enabled 缺省关（v0.45.614）：对话每问都花 API 费用，只能由人在抽屉里点开（CLAUDE.md：不许「有 key 就自动开」）
        base = {"favorites": [], "focus": list(DEFAULT_FOCUS), "interval_min": DEFAULT_INTERVAL_MIN,
                "poll_enabled": True, "chat_enabled": False,
                # 接货页「筛选」的名单与接货价（v0.45.722）：空名单 = 用接货账本的名单；接货价缺 = 占位（现价 × 90%）。只供显示，不改账本
                "wheel_watch": [], "wheel_accept": {}}
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
            if "wheel_watch" in patch:
                try:
                    cur["wheel_watch"] = WS.clean_watch(patch["wheel_watch"], norm=norm_ticker)
                except BadRequest:
                    raise
                except ValueError as exc:
                    raise BadRequest(str(exc), param="wheel_watch") from exc
            if "wheel_accept" in patch:
                try:
                    upd = WS.clean_accept(patch["wheel_accept"], norm=norm_ticker)
                except BadRequest:
                    raise
                except ValueError as exc:
                    raise BadRequest(str(exc), param="wheel_accept") from exc
                merged = dict(cur.get("wheel_accept") or {})
                for k, v in upd.items():
                    if v is None:
                        merged.pop(k, None)          # None = 清掉，回到占位
                    else:
                        merged[k] = v
                if len(merged) > WS.MAX_ACCEPT:
                    raise BadRequest(f"接货价最多 {WS.MAX_ACCEPT} 条", param="wheel_accept")
                cur["wheel_accept"] = merged
            if "poll_enabled" in patch:
                cur["poll_enabled"] = bool(patch["poll_enabled"])
            if "chat_enabled" in patch:
                if not isinstance(patch["chat_enabled"], bool):     # "false" 经 bool() 是 True：花钱的开关只认真布尔
                    raise BadRequest("chat_enabled 须为 true / false")
                cur["chat_enabled"] = patch["chat_enabled"]
            _atomic_write_json(self._settings_path(), cur)
            return cur

    def watchlist(self) -> dict:
        wl = default_watchlist()
        favs = [t for t in self.settings().get("favorites", []) if t not in wl]
        idx = list(INDEX_TICKERS)
        favs = [t for t in favs if t not in idx]
        return {"watchlist": wl, "favorites": favs, "indices": idx,
                "index_labels": {k: v["label"] for k, v in INDEX_TICKERS.items()}, "tickers": wl + favs}

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
        raws: List[dict] = []
        detail = self.live(t, force=True, _on_raw=raws.append)
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
            raw = raws[-1] if raws else None
            vols = self._attach_activity(t, snap, raw, date_et=now.date().isoformat(),
                                         last_ts=(last or {}).get("ts"))
            # 存档状态写进这一拍（ok / no_raw / failed）：存档坏了快照照落，但下游读快照就知道这天的链缺了
            snap["chain_archive"] = self._archive_chain(t, raw, now.date().isoformat())
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(snap, ensure_ascii=False) + "\n")
            # 只有落了盘的一拍才能当下一拍的「上一拍」（没落盘就比，增量会跨过文件里看不见的一拍）
            # 这一拍算不出（vols = None）也记下：下一拍报「上一拍没算出」，不冒充「刚启动」
            self._act_prev[t] = {"date": now.date().isoformat(), "source": _chain_source(raw),
                                 "ts": snap["ts"], "vols": vols}
        return {"ticker": t, "recorded": True, "ts": snap["ts"]}

    def _chain_archive_path(self, t: str, date_et: str) -> Path:
        return self._intraday_dir(date_et) / f"{t}{CHAIN_ARCHIVE_SUFFIX}"

    def _archive_chain(self, t: str, raw: Optional[dict], date_et: str) -> str:
        """把这一拍现算用的原始链整份覆盖写进当日存档（v0.45.815）。返回 `ok` / `no_raw` / `failed`。
        失败不挡快照落盘：计 `stats.chain_archive_failures`、日志 warning，并由调用方写进快照的 `chain_archive`。"""
        if raw is None:
            return "no_raw"
        try:
            _atomic_write_gzip_json(self._chain_archive_path(t, date_et), clean_json(raw))
            return "ok"
        except Exception as exc:  # noqa: BLE001 - 存档是附加取证材料，坏了不许连累快照本身
            self.stats["chain_archive_failures"] += 1
            _log.warning("[%s] 当日原始链存档写不进：%s: %s", t, type(exc).__name__, exc)
            return "failed"

    def archived_chain(self, ticker, date_et) -> dict:
        """读回某票某个美东日存档的原始链（当天最后一张落盘快照用的那份；v0.45.815）。
        没有 ⇒ `available=False, reason="missing"`；读坏 ⇒ `reason="corrupt"` + 异常文字（不当成没有）。
        给日间拆解（换 OI / IV / 到期日重算 Zero Gamma）用；没有页面读它。"""
        t, d = norm_ticker(ticker), norm_date(date_et)
        path = self._chain_archive_path(t, d)
        out = {"ticker": t, "date": d, "available": False, "reason": None, "raw": None}
        if not path.is_file():
            out["reason"] = "missing"
            return out
        try:
            with gzip.open(path, "rb") as f:
                raw = json.loads(f.read().decode("utf-8"))
        except (OSError, EOFError, ValueError) as exc:
            out["reason"] = "corrupt"
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        if not isinstance(raw, dict):
            out["reason"] = "corrupt"
            out["error"] = f"顶层不是对象：{type(raw).__name__}"
            return out
        out.update(available=True, raw=raw)
        return out

    def _attach_activity(self, t: str, snap: dict, raw: Optional[dict], *, date_et: str,
                         last_ts: Optional[str]) -> Optional[dict]:
        """把成交活跃度写进快照每个视图的 `activity`；返回这一拍的逐合约成交量（给下一拍比），算不了 ⇒ None。
        出错不挡快照落盘：每个视图写 `available: False` + `exception`，计 `stats.activity_failures`、日志 warning。"""
        views = snap.get("views") or {}
        try:
            if raw is None:
                for v in views.values():
                    v["activity"] = IA.unavailable("no_raw_chain")
                return None
            contracts = raw.get("contracts") or []
            S = snap.get("underlying_price")
            prev = self._act_prev.get(t)
            why = IA.compare_state(prev, date_et=date_et, source=_chain_source(raw), last_ts=last_ts)
            for name, v in views.items():
                if why is not None:
                    v["activity"] = IA.unavailable(why, since_ts=(prev or {}).get("ts"))
                    continue
                res = IA.build_view(prev["vols"], R.L.view_contracts(contracts, name), S,
                                    gamma_fn=activity_gamma, band=SNAPSHOT_STRIKE_BAND)
                v["activity"] = clean_json({"available": True, "reason": None, "since_ts": prev["ts"], **res})
            return IA.volume_map(contracts)
        except Exception as exc:  # noqa: BLE001 - 活跃度是附加展示，坏了不许连累快照本身
            self.stats["activity_failures"] += 1
            _log.warning("[%s] 盘中成交活跃度算不出：%s: %s", t, type(exc).__name__, exc)
            for v in views.values():
                v["activity"] = IA.unavailable("exception")
            return None

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
                    obj = json.loads(ln)
                except ValueError:
                    obj = None
                if isinstance(obj, dict):
                    snaps.append(obj)
                else:
                    bad += 1
        return {"ticker": t, "date": d, "snapshots": snaps, "n_bad_lines": bad}

    def intraday_heat(self, ticker, date_et: Optional[str] = None, *, view: str = "le_45dte",
                      basis: str = "oi", mode: str = "level") -> dict:
        """盘中热力图（叠现价 / Zero Gamma）：同一份单日快照交给 `intraday_heat.build` 排，不另取数。"""
        if view not in IH.VIEWS or (basis not in IH.BASES and basis not in IH.ACTIVITY_BASES) or mode not in IH.MODES:
            raise BadRequest(f"view / basis / mode 不认识：{view} / {basis} / {mode}")
        day = self.intraday(ticker, date_et)
        out = IH.build(day["snapshots"], view=view, basis=basis, mode=mode)
        out["rules"]["strike_band"] = SNAPSHOT_STRIKE_BAND
        out.update({"ticker": day["ticker"], "date": day["date"], "n_bad_lines": day.get("n_bad_lines", 0),
                    "reason_label": IH.REASONS.get(out["reason"]),
                    "activity_reason_labels": {**IA.REASONS, **IH.ACTIVITY_MISSING}})
        return clean_json(out)

    def _demo_intraday(self, t: str, d: str) -> dict:
        """演示：用合成链在 09:30–16:00 每 10 分钟编一张快照（现价走一条确定性的小路径）。"""
        from alphabot import synthetic as syn
        key = ("demo_intraday", t, d)
        if key not in self._bars_cache:
            snaps = []
            prev = None
            base_vol = {c["symbol"]: c["volume"] for c in syn.demo_payload(t, d)["contracts"]}
            for i in range(40):
                shift = 0.012 * math.sin(i / 6.0 + (syn._seed(t) % 7)) + 0.0006 * i
                raw = syn.demo_payload(t, d, intraday_shift=shift)
                for c in raw["contracts"]:      # 合成链的成交量是全天总量：按合约各自的节奏摊成随拍累计（单调不减）
                    full = base_vol.get(c["symbol"], c["volume"])     # 全天总量固定在不偏移的那份链上，否则会随现价忽大忽小
                    c["volume"] = float(int(full * ((i + 1) / 40.0) ** (0.6 + 0.8 * syn._rand(syn._seed(c["symbol"]), 7))))
                hh, mm = divmod(9 * 60 + 30 + 10 * i, 60)
                raw["payload_last_trade_time"] = f"{d}T{hh:02d}:{mm:02d}:00"
                raw["session_live"] = True
                detail = clean_json(R.compute_live_detail(t, fetch_fn=lambda *_a, _r=raw, **_k: (_r, None),
                                                          state_dir=self.ledger_dir))
                snap = self.snapshot_from(detail, f"{d}T{hh:02d}:{mm:02d}:00-04:00")
                for name, v in snap["views"].items():
                    if prev is None:
                        v["activity"] = IA.unavailable("first_of_day")
                        continue
                    res = IA.build_view(prev[1], R.L.view_contracts(raw["contracts"], name), snap["underlying_price"],
                                        gamma_fn=activity_gamma, band=SNAPSHOT_STRIKE_BAND)
                    v["activity"] = clean_json({"available": True, "reason": None, "since_ts": prev[0], **res})
                prev = (snap["ts"], IA.volume_map(raw["contracts"]))
                snaps.append(snap)
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
        self._last_watch: Optional[tuple] = None      # (标的, 上次给它拍的单调时刻)：盯盘的节拍
        service._watch_consumer = True
        self.status = {"running": False, "last_round_et": None, "last_results": [], "rounds": 0, "errors": 0,
                       "watch": {"ticker": None, "shots": 0, "last_result": None}}

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

    def _watch_tick(self, now_mono: float) -> Optional[dict]:
        """盯盘：租约内的那只票到点就拍一张（常规一轮刚拍过它也算数，不重复拍）。返回这次的结果或 None（没人盯 / 没到点）。"""
        t = self.service.watch_ticker()
        self.status["watch"]["ticker"] = t
        if t is None:
            self._last_watch = None
            return None
        if self._last_watch and self._last_watch[0] == t and now_mono - self._last_watch[1] < WATCH_INTERVAL_SEC:
            return None
        self._last_watch = (t, now_mono)
        try:
            res = self.service.take_snapshot(t)
        except Exception as exc:  # noqa: BLE001 - 盯盘失败不挡常规轮；计数 + 日志
            self.status["errors"] += 1
            _log.warning("[%s] 盯盘快照失败：%s: %s", t, type(exc).__name__, exc)
            res = {"ticker": t, "recorded": False, "reason": f"exception:{type(exc).__name__}"}
        self.status["watch"].update({"shots": self.status["watch"]["shots"] + 1, "last_result": res})
        return res

    def tick(self, *, now_mono: Optional[float] = None) -> Optional[list]:
        """跑一轮（到点且在盘中才拍）；返回本轮结果或 None（没到点 / 不在盘中 / 关闭）。盯盘的那一张不算进返回值，见 `status["watch"]`。"""
        st = self.service.settings()
        if not st.get("poll_enabled"):
            return None
        sess = self.session_fn()
        if not sess.get("live"):
            return None
        now_mono = time.monotonic() if now_mono is None else now_mono
        if not self.due(now_mono, int(st.get("interval_min") or DEFAULT_INTERVAL_MIN)):
            self._watch_tick(now_mono)
            return None
        self._last_round = now_mono
        results = []
        watched = self.service.watch_ticker()
        for t in (st.get("focus") or [])[:MAX_FOCUS]:
            if t == watched:
                self._last_watch = (t, now_mono)      # 常规轮这就拍它：盯盘的节拍从这里重新数
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
        self._watch_tick(now_mono)     # 被盯的票不在关注列表里时，这一轮没拍到它
        return results

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - 后台线程不能死；死了页面上就只剩旧状态
                self.status["errors"] += 1
                _log.warning("盘中快照线程异常：%s: %s", type(exc).__name__, exc, exc_info=True)
            # 有人盯盘时醒得勤一点，60 秒的节拍才不会被 30 秒的睡眠拉成 60 / 90 秒交替
            self._stop.wait(min(self.sleep_sec, WATCH_POLL_SEC) if self.service.watch_ticker() else self.sleep_sec)
