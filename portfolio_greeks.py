#!/usr/bin/env python3
"""
组合 Greeks 聚合 + β·Delta 带状对冲 + 现货×IV 联合压力测试（v0.45.103，期权路线图第 5 步 / 收尾）
=====================================================================================
做什么
------
把三本账**读到一张风险表上**，然后只做一件动作——用 SPY 股票把组合的 β·$Delta 拉回带内：
    ① 股票纸面组合   paper_portfolio.POSITIONS_FILE      （只读，不改它）
    ② 财报跨式腿     options_paper_leg.POSITIONS_FILE    （只读，不改它；两腿用 CBOE 重新报价）
    ③ 本模块自己的 SPY 覆盖账本  hedge_state/            （唯一会写的账本）
逐行算 $Delta / β·$Delta / $Gamma(1% 移动) / $Vega(每 vol 点) / $Theta(每日)，按合并 NAV 折成百分比，
给出 β·Delta 带状对冲建议，并把 现货冲击 × IV 冲击 的联合压力网格一起落盘审计。

为什么是带状（band）而不是连续对冲
------------------------------------
连续 delta 对冲在纸面上"最干净"，实盘里是**换手机器**：每天按残余 delta 交易，成本
与噪音成正比、与信息无关。这里的 β·$Delta 里装着股票纸面组合十几条持仓的方向观点（那是这本账
存在的意义），只有当组合整体的市场暴露超过 ±band（默认 NAV 的 15%）时才动手，
且只拉回到**带边**（`rebalance_to="edge"`，可切 "center"）——保留观点、砍掉尾巴。
带的宽度是 v1 的主观常数，不是校准结果；等 hedge_state/ 攒够天数再看该不该改。

为什么 v1 用 SPY 股票而不是指数认沽
------------------------------------
SPY 股票的对冲比 = β·$Delta / SPY 价，一行算式，无到期、无 IV 敞口、无滚仓，
点差 ~1 bp 可以忽略（本模块**没有点差模型**，成交价=当日收盘，下面明说）。
指数认沽会引入第二组 Greeks（对冲工具自己的 vega/theta），把「测量组合暴露」这件
事和「押 IV 方向」混在一起；等 vrp_signal（v0.45.102）证明卖/买 vol 有边际再谈。

CBOE Greeks 单位（2026-09-03 用 NVDA 实盘报价对 greeks_engine 校核，结论写死在这里）
-----------------------------------------------------------------------------------
    NVDA S=227.19，225C 2026-09-25（22 DTE）iv=0.3162：
        CBOE  delta 0.5736  gamma 0.0221  vega 0.2192  theta −0.1543
        BS    delta 0.5787  gamma 0.0222  vega 0.2182  theta −0.1720   （T=22/365, r=4.5%）
    225C 2026-10-02（29 DTE）iv=0.3207：CBOE vega 0.2517 / BS 0.2508；theta −0.1346 / BS −0.1536
  → **vega 是「每 1 个 vol 点（IV 变 0.01）」的每股价格变化**（若按每 1.00 vol 算应是 ~22，
    一张 $8 的期权不可能）；**theta 是「每日历日」的每股价格变化，多头为负**（CBOE 比 BS
    约小 10%，是日算法/股息约定差异，不是单位差异）；delta/gamma 每股，与 BS 差 <1%。
  所以：$Vega/pt = qty × vega；$Theta/日 = qty × theta；$Gamma(1%) = ½ · qty · gamma · (0.01·S)²。

诚实降级
--------
- β **不用** risk_engine._estimate_beta（失败时静默返回 1.0，与真实 β=1.0 同形——本项目
  明令禁止的「安全默认值」）。自己用 60 个交易日对 SPY 的对数收益 OLS 算，算不出就是
  `(None, None)`，聚合结果标 `partial=True`、`band_status="unknown"`，**不在部分数据上对冲**。
- 价格/报价/β 缺一行就少一行，`coverage` 里逐项计数；所有数值过 `_num()`（`bool(nan) is True`，
  真值判断挡不住 NaN），落盘前再 `_scrub()` 一遍。
- 合并 NAV 三个分量（股票账 / 跨式账 / SPY 覆盖）任一缺失 → None 并说出缺哪个。
- 压力网格里 β 缺失的行**剔除**并把该格标 `partial`，不用 1.0 顶上；剔除行的毛 |$Delta|
  记在 `excluded_dollar_delta` 并挂到 `worst_cell` 上——最差格那个数字会被单独引用，
  必须自带「少算了多少」。已到期（dte<=0）的合约同样剔除，不按 T=1/365 冒充活合约。
- IV 轴被 0 地板托住的格标 `iv_clamped` + 实际施加的 `iv_pts_effective`（8% IV 的票吃不下 −10pt）。
- 「从未启动」必须由净值文件与持仓文件**双双为空**证明；有持仓没净值行 = 数据缺失 → None。
- 账本里 shares 为 null/NaN 的行标成残行、不丢弃——丢掉之后 band_status 会报 "empty"。

标的价口径（v0.45.423）：只认 as_of 那一场的价，每个价都说得出自己属于哪一场
--------------------------------------------------------------------------------
v0.45.103~422 用 Twelve Data 日线「≤ as_of、5 个日历日内最后一根」当标的价。`twelve_data._drop_forming_bar`
按美东**日期**丢掉当日那根（那根当晚是本源的临时值，丢是对的），17:00 ET 的生产扫描于是天天拿到**前一交易日**的收盘，
配当天的 CBOE 期权报价算 $Delta，SPY 对冲按昨收「收盘成交」——2026-09-04~10-06 的 20 份审计文件
190/267 个价晚一个交易日，只有过了美东午夜才跑的那几天是对的。5 天容差把它当正常值放了过去。
病根在本模块：5 天容差把「前一交易日的收盘」改写成了「as_of 的价」。（v0.45.423 曾判在 twelve_data、把它的闸改成
收盘 + 30 分钟后照收；v0.45.438 实测当日那根收盘后仍是临时值——SPY 773.86 vs 官方 773.93、成交量 3.4%——已恢复按日期丢。
所以收盘后同日跑，as_of 的官方收盘**只有 CBOE 给得出**。）本模块这边：
- **期权行**的 S 取自与该合约报价**同一份** CBOE payload（`cboe_options.quote_contracts_with_underlying`）。
- **股票行 / SPY** 取 `_default_mark`：CBOE 官方收盘（last_trade 贴收盘）或盘中实时价；不行再要
  Twelve Data 上日期**恰为** as_of 的那根（已收完才算）；都不行就是**陈旧**。
- 陈旧价不进 $Delta / 净值 / 成交：行标 `price_stale`（看到的价留在 `stale_price` 供审计）、计入
  `coverage.n_price_stale` → partial → band unknown → 不对冲；ERROR 日志、日报小节、审计文件 `price_check`、
  `scan_timing.counters()["portfolio_greeks"]` → status.json → alert_manager 都会响。
- SPY 只有拿到 as_of 的**官方收盘**才成交（`fill: close` 名副其实）；盘中跑只算不成交。
- 周末 / 假日的 as_of 用之前最近一个交易日那一场（那天没有行情），见 `_expected_session`。
- 「陈旧」之外的「缺」（v0.45.435）：两源都取不到价、缺报价、缺 β、缺 NAV ⇒ 对冲决定做不出来
  （`price_check.hedge_undecided`），同一条观测链（ERROR / status.json / alert_manager P2）也会响。
  此前只有陈旧会红：2026-09-15~25 连续 7 天 unknown、覆盖层停摆，零告警。
- **读账本历史一律走 `load_history()`**（v0.45.440）：每条记录自带定价场次证明（成交 `price_session` /
  `price_source` / `price_at_close`，净值行 `spy_price_session` / `spy_price_at_close`，审计文件 `price_check`），
  证不出的不放行并计数。2026-09-04~10-07 的旧记录原样保留、不重算（用户决定），它们证不出 ⇒ 不放行；
  起点从数据里推（生产 = `MEASUREMENT_START` 2026-10-08）。别的模块直读 `hedge_state/` 历史会红
  （`tests/test_hedge_history_gate.py`）。

没做的事（已知局限）
--------------------
- 没有 SPY 点差/冲击成本模型（~1 bp，明说不建模）；没有融资利息（做空 SPY 的现金记正）。
- SPY 覆盖账的对冲工具只有 SPY 股票；不做指数认沽、不对冲 vega/gamma（只告警）。
  逐票 delta 对冲 v0.45.526 起有了（`delta_hedge.py`；缺省 shadow 只算不成交，纸面成交要单独改 `delta_hedge_mode`）。
- 期权重定价用 BS 平价面（每张合约各用自己的 IV 平移），不建模 skew 变化、不建模股息。
- 股票价用 CBOE 官方收盘（与 paper_portfolio 的 yfinance 收盘应逐分相等；CBOE 文件盘中陈旧那几天
  该行判陈旧、当天不对冲，不退到 yfinance——本模块不碰 yfinance 限流）。
- β 的 60 日 OLS 仍用 Twelve Data 日线：同日跑窗口止于前一交易日（当日那根当晚是临时值、被丢；少最后一个收益，
  对 β 无实质影响）；过了美东午夜补跑才含 as_of 那根。
- CBOE 文件是盘中陈旧的那几天（`cboe_options.close_verdict` 判 STALE_INTRADAY），股票行收盘后同日跑**没有**第二个 as_of 官方收盘来源
  ⇒ 判陈旧、当天不对冲、P2。这是诚实的代价：Twelve Data 当晚只有临时值，旧实现在这里静默用的是前一交易日。
- 压力网格里期权的 (0,0) 格不是 0：BS 价 − 市场 mid 的模型基差按合约单列在 `bs_vs_mid_gap`，
  不强行归零——那是模型诊断，不是 P&L。
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import delta_hedge as dh
import ledger_io
from hive_logger import PATHS, get_logger

_log = get_logger("portfolio_greeks")

CONFIG = {
    "beta_delta_band_pct": 15.0,     # β·$Delta 允许偏离 target 的带宽（% NAV）
    "beta_delta_target_pct": 0.0,    # 带中心（% NAV）；0 = 市场中性
    "vega_alert_pct": 1.0,           # |$Vega/pt| 超过 NAV 的 1% → 告警（不对冲）
    "gamma_alert_pct": 0.5,          # |$Gamma(1%)| 超过 NAV 的 0.5% → 告警（不对冲）
    "hedge_instrument": "SPY",
    "rebalance_to": "edge",          # edge = 拉回最近带边 | center = 拉回带中心
    "stress_spot_pct": [-10, -5, 0, 5, 10],
    "stress_iv_pts": [-10, 0, 10, 20],
    "risk_free": 0.045,
    "beta_window": 60,               # OLS 用的交易日数
    "beta_cache_trading_days": 5,    # β 缓存有效期（交易日）
    # 不进对冲计算的标的（v0.45.545，用户决定）：它的股票仓 / 跨式腿不进 $Delta、聚合、压力表、逐票对冲，
    # 也就**不再因它缺价而让整天判「数据不全」**。⚠️ 这不是「它没有敞口」——敞口照旧存在，只是 SPY 覆盖不管它；
    # 每天按建仓价估算它的名义额与 % NAV 写进审计 `excluded_from_hedge`（日报 / 对冲页都看得到），别让它悄悄变大。
    # BILI：CBOE 里它的标的价 last_trade 恒落后一天、Twelve Data 当晚又不给当天那根 ⇒ 10-15、10-16 连两天只它一只缺价。
    "hedge_exclude_tickers": ["BILI"],
    # ── 逐票 Delta 对冲（v0.45.526；规则与理由见 `delta_hedge.py`）──
    "delta_hedge_mode": "shadow",    # off | shadow（只算不成交，缺省）| paper（纸面成交，改这一行单独提交）
    "delta_hedge_band_pct": 3.0,     # 每只标的 |跨式腿净 $Delta + 对冲市值| 超过 NAV 的这么多 % 才动手（v1 主观常数）
    "delta_hedge_rebalance_to": "center",   # center = 回 0 | edge = 只拉回带边
}

BASE_DIR = PATHS.home
STATE_DIR = BASE_DIR / "hedge_state"
POSITIONS_FILE = STATE_DIR / "positions.jsonl"
TRADES_FILE = STATE_DIR / "trades.jsonl"
EQUITY_FILE = STATE_DIR / "equity_curve.jsonl"
META_FILE = STATE_DIR / "meta.json"
BETA_CACHE_FILE = STATE_DIR / "beta_cache.json"

_VERSION = "0.45.423"     # 口径世代：此前的审计文件 / 成交用的是前一交易日的标的价（见模块头）

#: 本账本「能自证定价场次」的记录从这天起才有（v0.45.423 首次在生产跑）。**只作文档与对账**：读历史一律走
#: `load_history()`，它按每条记录**自带**的场次证明放行，不按这个日期切——日期是从数据里推出来的结论
#: （`first_verified`），这里写下来只为让 `load_history()` 能报「推出来的起点与记载不符」。
#: 此前（2026-09-04 起）的记录：标的价 / SPY 成交价晚一个交易日（20 份审计 190/267 个价），净值曲线晚一天盯市，
#: 10-07→10-08 那一步含两天的 SPY 变动 ⇒ 第一个干净的**日收益**是 10-09。历史文件按用户决定原样保留、不重算。
MEASUREMENT_START = "2026-10-08"
_GREEK_KEYS = ("dollar_delta", "beta_dollar_delta", "gamma_dollar_per_1pct",
               "vega_dollar_per_pt", "theta_dollar_per_day")


# ══════════════════════════════════════════════════════════════════════════════
# 数值守卫 / 小工具
# ══════════════════════════════════════════════════════════════════════════════

def _num(v) -> Optional[float]:
    """float 且有限 → float；否则 None。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pos(v) -> Optional[float]:
    f = _num(v)
    return f if f is not None and f > 0 else None


def _scrub(obj, _path: str = ""):
    """落盘前最后一道闸：任何非有限 float 变 None 并 error 日志。"""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            _log.error("[PortfolioGreeks] 非有限值被拦在落盘前：%s=%r → None", _path, obj)
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _scrub(v, f"{_path}.{k}") for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, f"{_path}[{i}]") for i, v in enumerate(obj)]
    return obj


def _r(v, nd=2) -> Optional[float]:
    f = _num(v)
    return round(f, nd) if f is not None else None


def _days_between(d1: str, d2: str) -> Optional[int]:
    try:
        return (datetime.strptime(d2, "%Y-%m-%d") - datetime.strptime(d1, "%Y-%m-%d")).days
    except (TypeError, ValueError):
        return None


def _weekdays_between(d0: str, d1: str) -> Optional[int]:
    """(d0, d1] 内的工作日数；d1 < d0 → 负数（调用方据此拒绝「来自未来」的缓存）。"""
    try:
        a = datetime.strptime(d0, "%Y-%m-%d").date()
        b = datetime.strptime(d1, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    if b < a:
        return -1
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


# 账本读写一律经 `ledger_io`（v0.45.448）：加锁 + 原子替换 + 严格序列化 + 写后回读——代码写不出坏行；
# 读到坏行 ⇒ `LedgerCorrupt`（外部损坏），由 `run_for_date` 记进 price_check_stats()["error"] ⇒ P2，不再静默跳过。
# 此前追加是裸 `open("a")`（写到一半崩溃 = 半行），读时坏行被无声丢掉（持仓文件坏一行 = 少一个仓位照样对冲）。

def _load_jsonl(path: Path) -> List[Dict]:
    return ledger_io.load_jsonl(path)


def _write_jsonl(path: Path, records: List[Dict]) -> None:
    ledger_io.write_jsonl(path, [_scrub(r) for r in records])


def _append_jsonl(path: Path, record: Dict) -> None:
    ledger_io.append_jsonl(path, _scrub(record))


def _load_meta() -> Dict:
    # v0.45.452：meta 坏了抛 LedgerCorrupt（run_for_date 记进 price_check_stats()["error"] ⇒ P2），不再「按新账本
    # 处理」——那会把对冲腿 cash 重置成 0 并在本轮末写回，真实现金就此丢失。
    meta = ledger_io.load_json(META_FILE, default=None)
    if meta is not None:
        return meta
    return {"version": _VERSION, "starting_date": None, "cash": 0.0,
            "last_run_date": None, "config_snapshot": dict(CONFIG)}


def _save_meta(meta: Dict) -> None:
    ledger_io.write_json(META_FILE, _scrub(meta))


# ══════════════════════════════════════════════════════════════════════════════
# 默认数据源（测试一律注入；生产走 Twelve Data，兜底本地价格索引）
# ══════════════════════════════════════════════════════════════════════════════

_BARS_CACHE: Dict[Tuple[str, str], Optional[List[dict]]] = {}
# v0.45.105：100 → 120，与 `twelve_data.SHARED_BARS_WINDOW` 对齐。β 仍然只用最后
# 61 根（`beta_window`+1），多出来的历史一根都用不上——提到 120 纯粹是为了让本模块、
# vrp_signal（它的 `settle_window_bars` 真的要 120 根）、options_paper_leg 三方
# **请求同一个窗口**，从而共享 `twelve_data` 的进程内缓存：窗口不齐的话，先跑的
# 小窗口喂不饱后跑的大窗口，缓存等于白设。多取 20 根不多花配额（同一次请求的
# outputsize），Twelve Data 按调用次数计费、不按行数。
_BARS_WINDOW = 120


def _fetch_bars_uncached(ticker: str, as_of: str) -> Optional[List[dict]]:
    """真去取数的那一层（Twelve Data → 本地价格索引），**不碰缓存**。
    单独拆出来，是为了让「同一 (ticker, as_of) 只取一次」这件事能被测试直接数到——
    否则缓存命中与否只能靠计时猜，而本项目的教训是「看着成功其实早废了」。"""
    rows: Optional[List[dict]] = None
    src = "twelve_data"
    try:
        import twelve_data
        if twelve_data.is_configured():
            # v0.45.105：网络层的去重已经下沉到 twelve_data.fetch_bars
            #（按 (ticker, end_date) 记忆，三个消费方共享），本函数名里的
            # "uncached" 说的是**本模块这一层**不记忆，仍然成立。
            rows = twelve_data.fetch_bars(ticker, _BARS_WINDOW, end_date=as_of)
    except Exception as exc:  # noqa: BLE001
        _log.warning("[%s] Twelve Data 取 K 线失败: %s", ticker, exc)
        rows = None
    if not rows:
        try:
            from price_history import load_price_history
            hist = load_price_history(ticker, str(PATHS.cache_dir))
            if hist:
                rows = [{"date": d, "close": c} for d, c in hist]
                src = "local_index"
                _log.info("[%s] 用本地价格索引代替 Twelve Data 日线（%d 根）", ticker, len(rows))
        except Exception as exc:  # noqa: BLE001
            _log.warning("[%s] 本地价格索引读取失败: %s", ticker, exc)
    clean = []
    for b in rows or []:
        d = str(b.get("date") or "")[:10]
        c = _pos(b.get("close"))
        if d and c is not None and d <= as_of:
            # v0.45.423：带上来源。β 两种都能用；标的价只认 Twelve Data 那根（见 `_default_mark` ②）——
            # 本地索引是快照价 / price_at_predict，分不清那一刻是盘中还是收盘。
            clean.append({"date": d, "close": c, "src": src})
    clean.sort(key=lambda b: b["date"])
    return clean or None


def daily_bars(ticker: str, as_of: str) -> Optional[List[dict]]:
    """截至 as_of（含）的日线 `[{date, close}]` 升序，进程内按 (ticker, as_of) 记忆一次。
    **本模块对外的取 K 线入口**（二次复查新增）：收盘价与 β 共用同一次 Twelve Data 调用
    （30 只票 + SPY ≈ 31 次/天，预算 800，且 Twelve Data 是串行 7 次/分钟）。
    Twelve Data 未配置/拿不到 → 本地价格索引（快照价，口径略不同，日志里说）。

    v0.45.105：这里的 `_BARS_CACHE` 记的是**归一化之后**的结果——按 as_of 截断、
    去掉坏行、排好序，还可能来自本地价格索引而不是 Twelve Data。它和网络层
    去重是两件事，所以两层都留着，但**不再各管各的**：
      · 本层（`_BARS_CACHE`）省的是重复的归一化 + 兜底分支，并且给 `_bars_in_memory`
        提供「重算 β 要不要网络」的判据；
      · 网络层（`twelve_data.fetch_bars`，按 `(ticker, end_date)` 记忆）省的是
        真正贵的东西——串行 7 次/分钟的 API 调用，且**跨模块共享**。
    从前 vrp_signal 与 options_paper_leg 各自裸调 `twelve_data._fetch_rows`，
    同一只票的日线一次扫描最多被取 3 遍；三方现在都走 `fetch_bars` 且都请求
    `SHARED_BARS_WINDOW`(=120) 根，只剩 1 遍。缓存没放在本模块，是因为
    本模块已经 import 了 options_paper_leg，反向 import 会成环；`twelve_data`
    是三方共同的叶子依赖，放那里谁都不欠谁。"""
    key = (ticker, as_of)
    if key in _BARS_CACHE:
        return _BARS_CACHE[key]
    _BARS_CACHE[key] = _fetch_bars_uncached(ticker, as_of)
    return _BARS_CACHE[key]


def _default_bars(ticker: str, as_of: str) -> Optional[List[dict]]:
    """内部别名 = daily_bars（测试注入的挂钩点，历史名字，别删）。"""
    return daily_bars(ticker, as_of)


def _bars_in_memory(ticker: str, as_of: str) -> bool:
    """这只票与基准的日线是否**已经在本进程缓存里**（即：重算 β 不需要任何网络调用）。"""
    return bool(_BARS_CACHE.get((ticker, as_of))) and bool(_BARS_CACHE.get((CONFIG["hedge_instrument"], as_of)))


# ── 标的价：只认 as_of 那一场（v0.45.423，取代「5 个日历日内最后一根」的 `_default_close`）──────────
#
# mark = {"price", "source", "session", "at_close", "live"}：价 + 它属于哪一场 + 是不是那场的官方收盘 /
# 此刻的实时价。`closes_fn` 的默认实现是 `_default_mark`（返回 mark）；测试 / 旧调用方注入的裸数字由
# `_resolve_mark` 当作「调用方担保这就是 as_of 的收盘」。

_ET_TZ_NAME = "America/New_York"


def _et_now() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(_ET_TZ_NAME))


def _expected_session(as_of: str) -> str:
    """as_of 该用哪一场的价：交易日 = 当天；周末 / 假日 = 之前最近一个交易日（那天没有行情可等）。
    日历不可用 → 原样返回 as_of：宁可把价判成陈旧，也不猜场次。"""
    try:
        from is_trading_day import is_trading_day
        d = datetime.strptime(as_of, "%Y-%m-%d").date()
        for _ in range(10):                       # 长假最多跨约 5 天
            if is_trading_day(d)[0]:
                return d.isoformat()
            d -= timedelta(days=1)
    except Exception:  # noqa: BLE001
        _log.debug("交易日历不可用，按 as_of 原样判场次", exc_info=True)
    return as_of


def _session_closed(day: str, now_et: Optional[datetime] = None) -> bool:
    """`day` 那一场此刻是否已收盘（交易所收盘时刻，提前收盘日 13:00）。判不了 → False：不当它收完。"""
    try:
        from zoneinfo import ZoneInfo
        from is_trading_day import session_close_et
        d = datetime.strptime(day, "%Y-%m-%d").date()
        now = now_et or _et_now()
        if now.tzinfo is not None:
            now = now.astimezone(ZoneInfo(_ET_TZ_NAME)).replace(tzinfo=None)
        return now >= datetime.combine(d, session_close_et(d))
    except Exception:  # noqa: BLE001
        return False


def _default_mark(ticker: str, as_of: str) -> Dict:
    """标的在 as_of 那一场的价，**自报场次**。默认的 `closes_fn`。

    旧实现（`_default_close`）取 Twelve Data「≤ as_of、5 个日历日内最后一根」，而 twelve_data 按美东
    **日期**丢当日那根、不看收没收盘：17:00 ET 的生产扫描天天拿到前一交易日的收盘，5 天容差照单全收。
    「10-05 价是对的」恰恰是例外——那次重跑在 02:56 ET（次日），美东日期已翻页，那根才没被丢。

    按顺序取，每一步都说得出价属于哪一场：
      ① CBOE（`cboe_options.fetch_underlying_mark`）——与期权报价同源。收盘后要 last_trade 贴着收盘
         才算官方收盘，盘中给实时价；盘中生成的陈旧文件、判不了的文件都不算。
      ② Twelve Data 上日期**恰为**该场的那根，且交易所钟表上那一场已收盘（不只靠 twelve_data 那道闸
         丢没丢它来保证收完）。twelve_data 按美东日期丢当日那根（当晚是临时值，v0.45.438 实测），所以②只在
         **过了美东午夜**才跑的补跑 / 回填里走得到——那时 CBOE 场次已翻页，靠的就是它；收盘后同日跑走不到②，
         CBOE 拿不到 / 文件是盘中陈旧的那几只判陈旧（③）。本地价格索引不算（`src` 不是 twelve_data：
         快照价 / price_at_predict 分不清盘中还是收盘）。
      ③ 都不是 → 返回看到的最好那个，带它**真实的**场次，由 `_resolve_mark` 判陈旧、标红。

    日线**照旧先取**，哪怕①用不上它：`_default_beta` 靠 `_bars_in_memory` 判「重算 β 要不要网络」，
    日线不在内存就端出磁盘里至多 4 个交易日前的缓存 β。旧 `_default_close` 恒先取日线，所以生产上
    β 恒重算——这里改了取价，不能顺手把 β 的口径也改了。
    """
    want = _expected_session(as_of)
    bars = _default_bars(ticker, as_of) or []
    cboe: Optional[Dict] = None
    try:
        from cboe_options import fetch_underlying_mark
        cboe = fetch_underlying_mark(ticker)
    except Exception as exc:  # noqa: BLE001
        _log.warning("[%s] CBOE 标的价获取失败: %s", ticker, exc)
    if (cboe and _pos(cboe.get("price")) is not None and cboe.get("session") == want
            and (cboe.get("at_close") or cboe.get("live"))):
        return cboe
    bar = next((b for b in reversed(bars) if b.get("date") == want), None)
    if bar is not None and bar.get("src") == "twelve_data" and _session_closed(want):
        return {"price": bar["close"], "source": "twelve_data_bar", "session": want,
                "at_close": True, "live": False}
    if cboe and _pos(cboe.get("price")) is not None:
        return cboe                                    # 场次不对 / 不是收盘：原样交出去判陈旧
    if bars:
        last = bars[-1]
        return {"price": last["close"], "source": f"{last.get('src') or 'bars'}_bar", "session": last["date"],
                "at_close": bool(last.get("src") == "twelve_data" and _session_closed(last["date"])),
                "live": False}
    return {"price": None, "source": "unavailable", "session": None, "at_close": False, "live": False}


def _resolve_mark(raw, as_of: str, *, paired_with_quote: bool = False) -> Dict:
    """`closes_fn` 的返回（或报价里附带的 "underlying"）→ 规整后的 mark，并判它能不能当 as_of 的价用。

    能用 ⇔ 场次 == `_expected_session(as_of)` 且（官方收盘 或 盘中实时价）。
    `paired_with_quote=True`（期权行：S 与报价出自同一份 payload）时只要求场次对——盘中生成的陈旧文件
    里，标的价与同一份文件里的期权报价是**同一时刻**的，配对算 $Delta 要的正是它。
    裸数字（测试注入 / 旧调用方）= 调用方担保「这就是 as_of 的收盘」。生产默认 `_default_mark`
    **只返回 dict**（守卫 `TestDefaultMarkContract`），裸数字到不了生产路径。

    返回 {"price": 可用价 | None, "stale", "seen_price", "source", "session", "at_close", "live"}。
    stale=True ⇒ price=None：看到的价只留在 seen_price 给人看，**绝不进 $Delta / 净值 / 成交**。
    """
    if isinstance(raw, dict):
        seen = _pos(raw.get("price"))
        session, src = raw.get("session"), raw.get("source")
        at_close, live = bool(raw.get("at_close")), bool(raw.get("live"))
    else:
        seen = _pos(raw)
        session, src = (as_of, "injected") if seen is not None else (None, None)
        at_close, live = seen is not None, False
    usable = (seen is not None and session == _expected_session(as_of)
              and (at_close or live or paired_with_quote))
    return {"price": seen if usable else None, "stale": seen is not None and not usable,
            "seen_price": seen, "source": src, "session": session,
            "at_close": usable and at_close, "live": usable and live}


def _mark_from(closes_fn, ticker: str, as_of: str) -> Dict:
    try:
        raw = closes_fn(ticker, as_of)
    except Exception as exc:  # noqa: BLE001
        _log.warning("[%s] 标的价获取失败: %s", ticker, exc)
        raw = None
    return _resolve_mark(raw, as_of)


def _memo_marks(closes_fn):
    """一次运行内同一 (ticker, as_of) 只取一次：SPY 的价在建议 / 净值 / 成交 / 盯市 / 交易后聚合里
    必须是**同一个**数（同一时刻），且 CBOE 取不到时别每处各重试一遍。"""
    seen: Dict[Tuple[str, str], object] = {}

    def fn(ticker: str, as_of: str):
        key = (ticker, as_of)
        if key not in seen:
            seen[key] = closes_fn(ticker, as_of)
        return seen[key]
    fn._memoized = True
    return fn


def _apply_mark(row: Dict, m: Dict) -> Optional[float]:
    """把 mark 落到行上。陈旧行同时标 price_missing（as_of 没有可用价）——沿用缺价的整套「partial →
    unknown → 不对冲」，`price_stale` 只补「为什么」。"""
    row["price"] = m["price"]
    row["price_missing"] = m["price"] is None
    row["price_stale"] = bool(m["stale"])
    row["price_session"] = m["session"]
    row["price_source"] = m["source"]
    if m["stale"]:
        row["stale_price"] = m["seen_price"]
    return m["price"]


def _ols_beta(stock: List[dict], bench: List[dict], as_of: str, window: int) -> Optional[Tuple[float, int]]:
    """按日期对齐后取最后 window+1 根共同日线 → window 个对数收益 → 斜率 cov/var。
    共同日线不足 window+1 根 → None（不降级到更短窗口冒充 60 日 β）。"""
    b_by = {b["date"]: b["close"] for b in bench if b["date"] <= as_of}
    pairs = [(s["close"], b_by[s["date"]]) for s in stock if s["date"] <= as_of and s["date"] in b_by]
    pairs = pairs[-(window + 1):]
    if len(pairs) < window + 1:
        return None
    rs, rb = [], []
    for (s0, b0), (s1, b1) in zip(pairs[:-1], pairs[1:]):
        if min(s0, b0, s1, b1) <= 0:
            return None
        rs.append(math.log(s1 / s0))
        rb.append(math.log(b1 / b0))
    n = len(rb)
    mb, ms = sum(rb) / n, sum(rs) / n
    var = sum((x - mb) ** 2 for x in rb)
    if var <= 0:
        return None
    cov = sum((x - mb) * (y - ms) for x, y in zip(rb, rs))
    beta = cov / var
    return (beta, n) if math.isfinite(beta) else None


def _read_beta_cache() -> Dict:
    if BETA_CACHE_FILE.exists():
        try:
            d = json.loads(BETA_CACHE_FILE.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}
    return {}


def _fresh_cached_beta(ticker: str, as_of: str) -> Optional[float]:
    """磁盘缓存里对 as_of 仍然有效的 β：计算日在 (as_of − 5 个交易日, as_of] 内。
    计算日晚于 as_of（补跑历史）一律不用——那是未来数据。"""
    ent = _read_beta_cache().get(ticker)
    if not isinstance(ent, dict):
        return None
    wd = _weekdays_between(str(ent.get("as_of") or ""), as_of)
    b = _num(ent.get("beta"))
    if b is not None and wd is not None and 0 <= wd < CONFIG["beta_cache_trading_days"]:
        return b
    return None


def _default_beta(ticker: str, as_of: str) -> Tuple[Optional[float], Optional[str]]:
    """60 日 OLS β 对 SPY。返回 (beta, source)，source ∈ {"ols60", "cache", None}。
    算不出 → (None, None)。

    磁盘缓存只在**日线拿不到时**兜底，不再抢在重算前面（二次复查修正）：走到这里时
    _default_mark 早已把同一 (ticker, as_of) 的日线放进 _BARS_CACHE，OLS 不过是 60 次
    乘加——用缓存**一次网络调用都省不下**，却可能端上 4 个交易日前的 β。省错东西了。
    所以：日线已在内存 → 一律重算；日线取不到（限流/断网）→ 才退回缓存并标 source="cache"。"""
    if ticker == CONFIG["hedge_instrument"]:
        return 1.0, "benchmark"
    if not _bars_in_memory(ticker, as_of):
        b = _fresh_cached_beta(ticker, as_of)
        if b is not None:
            return b, "cache"
    stock = _default_bars(ticker, as_of)
    bench = _default_bars(CONFIG["hedge_instrument"], as_of)
    res = _ols_beta(stock, bench, as_of, CONFIG["beta_window"]) if (stock and bench) else None
    if res is None:
        b = _fresh_cached_beta(ticker, as_of)
        if b is not None:
            _log.warning("[%s] 日线不可得/不足，退回 %d 个交易日内的缓存 β", ticker, CONFIG["beta_cache_trading_days"])
            return b, "cache"
        if stock and bench:
            _log.warning("[%s] β 不可得：与 SPY 对齐的日线不足 %d 根", ticker, CONFIG["beta_window"] + 1)
        return None, None
    beta, n = res
    beta = round(beta, 4)
    cache = _read_beta_cache()          # 写回时才读盘：缓存的用途已只剩「日线断供时的兜底」
    cache[ticker] = {"as_of": as_of, "beta": beta, "n": n, "computed_at": as_of}
    try:
        ledger_io.write_json(BETA_CACHE_FILE, _scrub(cache), indent=1)
    except (OSError, ledger_io.LedgerError) as exc:   # 失败已在 ledger_io 源头登记（⇒ P2），这里只保本次结果
        _log.warning("β 缓存写入失败（不影响本次结果）: %s", exc)
    return beta, f"ols{CONFIG['beta_window']}"


def _default_quotes(ticker: str, symbols: List[str]) -> Dict[str, Optional[dict]]:
    """CBOE 重新报价；每张报价附上**同一份 payload** 的标的 mark（键 "underlying"）——
    option_exposures 拿它当 S，不再另取一个别的时刻、别的来源的价（v0.45.423）。"""
    from cboe_options import quote_contracts_with_underlying
    quotes, und = quote_contracts_with_underlying(ticker, symbols)
    return {s: ({**q, "underlying": und} if isinstance(q, dict) else q) for s, q in quotes.items()}


def _bid_zero_greeks_usable(q) -> bool:
    """`quote_ok` 为假、但 CBOE 的逐合约 Greeks 仍可用的那一类：**bid = 0**（深度虚值腿最常见）、
    ask 有限且 > 0、四个 Greeks 与 iv 都有限、iv > 0（iv 缺 / ≤ 0 时 CBOE 给的是全零占位，不是观测值）。
    mid 在这里依旧是 None——bid = 0 的「中间价」只是 ask 的一半，不是市价；但 Greeks 与 mid 无关，
    让这样一张腿把整个组合的 β·$Delta 变成 unknown（v0.45.609 之前的行为）等于为了 0.03 的 ask 停掉对冲。
    判据与 Alpha Bot 跨式页盘中参考（`straddle._greeks_usable`，v0.45.581）一致；这里不 import 它（依赖方向）。"""
    if not isinstance(q, dict):
        return False
    bid, ask = _num(q.get("bid")), _num(q.get("ask"))
    if bid is None or ask is None or bid != 0 or ask <= 0:
        return False
    iv = _num(q.get("iv"))
    return iv is not None and iv > 0 and all(_num(q.get(k)) is not None for k in ("delta", "gamma", "vega", "theta"))


# ══════════════════════════════════════════════════════════════════════════════
# 逐行暴露
# ══════════════════════════════════════════════════════════════════════════════

def _blank_row(ticker: str, kind: str) -> Dict:
    return {"ticker": ticker, "kind": kind, "qty": None, "price": None,
            "dollar_delta": None, "beta": None, "beta_source": None, "beta_dollar_delta": None,
            "gamma_dollar_per_1pct": None, "vega_dollar_per_pt": None, "theta_dollar_per_day": None,
            "price_missing": False, "quote_missing": False, "beta_missing": False,
            "price_stale": False, "price_session": None, "price_source": None}


def _apply_beta(row: Dict, beta_fn, as_of: str) -> None:
    try:
        beta, src = beta_fn(row["ticker"], as_of)
    except Exception as exc:  # noqa: BLE001
        _log.warning("[%s] β 计算异常: %s", row["ticker"], exc)
        beta, src = None, None
    beta = _num(beta)
    row["beta"], row["beta_source"] = beta, (src if beta is not None else None)
    row["beta_missing"] = beta is None
    dd = _num(row.get("dollar_delta"))
    row["beta_dollar_delta"] = round(dd * beta, 2) if (dd is not None and beta is not None) else None


def stock_exposures(as_of: str, positions: Optional[List[Dict]] = None,
                    closes_fn: Optional[Callable[[str, str], Optional[float]]] = None,
                    beta_fn: Optional[Callable[[str, str], Tuple[Optional[float], Optional[str]]]] = None,
                    exclude: Optional[Iterable[str]] = None
                    ) -> List[Dict]:
    """股票纸面组合每条持仓一行：qty = +shares（bullish）/ −shares（bearish）。
    股票没有 gamma/vega/theta，那三项是真实的 0.0 而不是「未知」。
    价只认 as_of 那一场（`_resolve_mark`）；陈旧价不进 $Delta。
    `exclude`：不出行的标的（`CONFIG["hedge_exclude_tickers"]`，由 compute_day 传入并另行记账）。"""
    closes_fn = closes_fn or _default_mark
    beta_fn = beta_fn or _default_beta
    if positions is None:
        import paper_portfolio as pp
        positions = _load_jsonl(pp.POSITIONS_FILE)
    rows: List[Dict] = []
    skip = {str(t).upper() for t in (exclude or ())}
    for p in positions or []:
        tk = str(p.get("ticker") or "")
        if tk.upper() in skip:
            continue
        row = _blank_row(tk, "stock")
        row.update({"direction": p.get("direction"), "entry_date": p.get("entry_date"),
                    "gamma_dollar_per_1pct": 0.0, "vega_dollar_per_pt": 0.0, "theta_dollar_per_day": 0.0})
        shares = _num(p.get("shares"))
        if not tk or shares is None:
            _log.warning("[PortfolioGreeks] 股票仓记录不完整（ticker=%r shares=%r），按缺价处理", tk, p.get("shares"))
            row["price_missing"] = True
            row["beta_missing"] = True
            rows.append(row)
            continue
        sign = -1.0 if str(p.get("direction")) == "bearish" else 1.0
        row["qty"] = round(sign * shares, 6)
        px = _apply_mark(row, _mark_from(closes_fn, tk, as_of))
        row["dollar_delta"] = round(row["qty"] * px, 2) if px is not None else None
        _apply_beta(row, beta_fn, as_of)
        rows.append(row)
    return rows


def option_exposures(as_of: str, positions: Optional[List[Dict]] = None,
                     quotes_fn: Optional[Callable[[str, List[str]], Dict[str, Optional[dict]]]] = None,
                     beta_fn: Optional[Callable[[str, str], Tuple[Optional[float], Optional[str]]]] = None,
                     closes_fn: Optional[Callable[[str, str], Optional[float]]] = None,
                     exclude: Optional[Iterable[str]] = None) -> List[Dict]:
    """跨式腿每张合约一行（一笔跨式两行）。qty = ±contracts×100（long +，short −）。
    Greeks 来自 CBOE 重新报价（单位见模块头）：
        $Delta      = qty × delta × S
        $Gamma(1%)  = ½ × qty × gamma × (0.01·S)²        —— 1% 移动的凸性 P&L
        $Vega/pt    = qty × vega                          —— CBOE vega 已是每 vol 点
        $Theta/日   = qty × theta                         —— CBOE theta 每日历日、多头为负
    报价缺失 → 该行 quote_missing、Greeks None（不沿用 last_mark 的任何东西）。
    例外（v0.45.609）：bid = 0 但 ask 真实、Greeks 与 iv 齐全的腿（`_bid_zero_greeks_usable`）**不算缺**——
    Greeks 照进汇总、行上标 `bid_zero=True`、`mid` 仍为 None（压力表因缺 mid 单独剔这一行并说明原因）。
    S（v0.45.423）取自与报价**同一份** CBOE payload（`_default_quotes` 附在报价里的 "underlying"），
    与期权报价同一时刻；报价里没有它（注入的 quotes_fn）才退回 closes_fn。payload 属于别的场次
    （补跑历史日时拉到的是今天的链）⇒ 报价本身也不是 as_of 的：quote_missing + quote_stale，Greeks 不进汇总。"""
    quotes_fn = quotes_fn or _default_quotes
    closes_fn = closes_fn or _default_mark
    beta_fn = beta_fn or _default_beta
    if positions is None:
        import options_paper_leg as opl
        positions = _load_jsonl(opl.POSITIONS_FILE)
    want = _expected_session(as_of)
    rows: List[Dict] = []
    skip = {str(t).upper() for t in (exclude or ())}
    for p in positions or []:
        tk = str(p.get("ticker") or "")
        if tk.upper() in skip:
            continue                      # 不报价、不取价、不算 β：排除就是整只不碰（敞口另由 excluded_exposure 记账）
        side = str(p.get("side") or "")
        n = _num(p.get("contracts"))
        legs = [("call", p.get("call_symbol")), ("put", p.get("put_symbol"))]
        try:
            quotes = quotes_fn(tk, [s for _, s in legs if s]) or {}
        except Exception as exc:  # noqa: BLE001
            _log.warning("[%s] 持仓重新报价失败: %s", tk, exc)
            quotes = {}
        und = next((q["underlying"] for q in quotes.values()
                    if isinstance(q, dict) and isinstance(q.get("underlying"), dict)), None)
        # closes_fn 照旧每只票调一次：默认实现顺带把 β 要的日线取进缓存（见 `_default_mark` 末段），
        # 注入的 quotes_fn 不带 "underlying" 时它就是 S。
        fallback = _mark_from(closes_fn, tk, as_of)
        if und is not None and _pos(und.get("price")) is not None:
            m = _resolve_mark(und, as_of, paired_with_quote=True)
            # 只凭**正面证据**判报价错场（场次已知且不是这一场）；判不了场次（无 last_trade_time）时
            # 报价照用（同 CBOE 那一栈的 fail-open），但 S 判陈旧 ⇒ $Delta 缺 ⇒ unknown，照样不对冲、照样报。
            quote_stale = und.get("session") is not None and und.get("session") != want
        else:
            m, quote_stale = fallback, False
        sign = 1.0 if side == "long" else -1.0
        for cp, sym in legs:
            row = _blank_row(tk, "option")
            row.update({"symbol": sym, "cp": cp, "side": side, "strike": _num(p.get("strike")),
                        "expiry": p.get("expiry"), "dte": _days_between(as_of, str(p.get("expiry") or "")),
                        "mid": None, "iv": None, "delta": None, "gamma": None, "vega": None, "theta": None})
            S = _apply_mark(row, m)
            if n is None or n <= 0 or not sym:
                row["quote_missing"] = True
                _apply_beta(row, beta_fn, as_of)
                rows.append(row)
                continue
            row["qty"] = sign * n * 100.0
            q = quotes.get(sym)
            if quote_stale and isinstance(q, dict):
                row["quote_stale"] = True
                row["quote_session"] = und.get("session")
                q = None                    # 别的场次的报价：不进 Greeks 汇总（同缺报价）
            ok = isinstance(q, dict) and bool(q.get("quote_ok"))
            bid_zero = (not ok) and _bid_zero_greeks_usable(q)
            g = {k: _num(q.get(k)) for k in ("mid", "iv", "delta", "gamma", "vega", "theta")} if (ok or bid_zero) else {}
            if not (ok or bid_zero) or any(g.get(k) is None
                                          for k in (("mid",) if ok else ()) + ("delta", "gamma", "vega", "theta")):
                row["quote_missing"] = True
                _apply_beta(row, beta_fn, as_of)
                rows.append(row)
                continue
            row.update(g)
            if bid_zero:
                row["bid_zero"] = True          # 买价为 0：Greeks 是 CBOE 模型值、mid 不给（None）；只在这类行上出现
            qty = row["qty"]
            row["vega_dollar_per_pt"] = round(qty * g["vega"], 2)
            row["theta_dollar_per_day"] = round(qty * g["theta"], 2)
            if S is not None:
                row["dollar_delta"] = round(qty * g["delta"] * S, 2)
                row["gamma_dollar_per_1pct"] = round(0.5 * qty * g["gamma"] * (0.01 * S) ** 2, 2)
            _apply_beta(row, beta_fn, as_of)
            rows.append(row)
    return rows


def _excluded_tickers() -> List[str]:
    return [str(t).upper() for t in (CONFIG.get("hedge_exclude_tickers") or []) if str(t).strip()]


def excluded_exposure(nav: Optional[float], stock_positions: Optional[List[Dict]] = None,
                      option_positions: Optional[List[Dict]] = None) -> Optional[Dict]:
    """被 `hedge_exclude_tickers` 挡在对冲之外的那几只，**到底还剩多少敞口**——不让「排除」变成「看不见」。
    股票仓按**建仓价**估名义额（不取任何价：取价正是它被排除的原因）：bullish 为正、bearish 为负；
    跨式腿只数张数（没有报价就没有 Delta，不编）。没有配置排除 ⇒ None；配了但今天没持仓 ⇒ 空 names（说明它没起作用）。"""
    excl = _excluded_tickers()
    if not excl:
        return None
    if stock_positions is None:
        import paper_portfolio as pp
        stock_positions = _load_jsonl(pp.POSITIONS_FILE)
    if option_positions is None:
        import options_paper_leg as opl
        option_positions = _load_jsonl(opl.POSITIONS_FILE)
    nav = _pos(nav)
    names: List[Dict] = []
    for tk in excl:
        usd, n_stock, unknown = 0.0, 0, False
        for p in stock_positions or []:
            if str(p.get("ticker") or "").upper() != tk:
                continue
            n_stock += 1
            sh, px = _num(p.get("shares")), _pos(p.get("entry_price"))
            if sh is None or px is None:
                unknown = True
                continue
            usd += (-1.0 if str(p.get("direction")) == "bearish" else 1.0) * sh * px
        n_legs = sum(1 for p in option_positions or [] if str(p.get("ticker") or "").upper() == tk)
        if not n_stock and not n_legs:
            continue
        names.append({"ticker": tk, "n_stock_positions": n_stock, "n_straddles": n_legs,
                      "stock_usd_est": None if unknown or not n_stock else round(usd, 2),
                      "stock_pct_nav_est": (round(usd / nav * 100.0, 3)
                                            if (nav and n_stock and not unknown) else None),
                      "basis": "entry_price"})
    return {"configured": excl, "names": names, "nav": nav,
            "note": "按建仓价估算的名义额（不取现价）；这些仓不进 $Delta / 聚合 / 压力表 / 逐票对冲，SPY 覆盖不管它们"}


def _hedge_positions() -> List[Dict]:
    return _load_jsonl(POSITIONS_FILE)


def hedge_exposures(as_of: str, closes_fn: Optional[Callable[[str, str], Optional[float]]] = None,
                    positions: Optional[List[Dict]] = None, spy_price: Optional[float] = None,
                    spy_mark: Optional[Dict] = None) -> List[Dict]:
    """SPY 覆盖账本的持仓行。β=1.0 是定义（对冲工具就是基准），source="benchmark"。
    价：`spy_mark`（compute_day 已判过场次的那一个）> `spy_price`（调用方担保是 as_of 的价）> closes_fn。"""
    closes_fn = closes_fn or _default_mark
    if positions is None:
        positions = _hedge_positions()
    if spy_mark is None and _pos(spy_price) is not None:
        spy_mark = _resolve_mark(spy_price, as_of)
    rows: List[Dict] = []
    for p in positions or []:
        tk = str(p.get("ticker") or CONFIG["hedge_instrument"])
        row = _blank_row(tk, "hedge")
        row.update({"gamma_dollar_per_1pct": 0.0, "vega_dollar_per_pt": 0.0, "theta_dollar_per_day": 0.0,
                    "beta": 1.0, "beta_source": "benchmark", "avg_price": _num(p.get("avg_price"))})
        shares = _num(p.get("shares"))
        if shares is None:
            # shares 是 null/NaN（_scrub 落盘时把 NaN 写成 null，坏行会原样读回来）：
            # 这行的 $ 暴露**未知**，不是 0。原来 `continue` 把它和「已平掉的 0 股行」
            # 一起丢掉，结果 hedge_exposures 返回空表、band_status 报 "empty"、
            # n_price_missing=0——自信且错误。照 stock_exposures 的老规矩标一行残行
            # （那边 :385 一直是这么处理的），让 β·$Delta 缺一项 → unknown → 不对冲。
            # 标 price_missing 而不是新造一个 shares_missing：口径与股票行一致，
            # 覆盖率计数与 hedge_recommendation 的 reason 都不用改就能说出「缺了东西」。
            _log.warning("[PortfolioGreeks] 对冲账本仓位 shares 非有限（%r），按缺数据处理不跳过", p.get("shares"))
            row["price_missing"] = True
            row["beta_missing"] = True
            row["beta"] = row["beta_source"] = None
            rows.append(row)
            continue
        if shares == 0:
            continue                      # 真实的 0 股（平仓留痕）：跳过是对的，它确实没有暴露
        row["qty"] = shares
        px = _apply_mark(row, spy_mark if spy_mark is not None else _mark_from(closes_fn, tk, as_of))
        if px is not None:
            row["dollar_delta"] = round(shares * px, 2)
            row["beta_dollar_delta"] = row["dollar_delta"]
        rows.append(row)
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# 逐票 Delta 对冲（v0.45.526）：每个有跨式腿的标的，用自己的股票对冲跨式 Delta
# ══════════════════════════════════════════════════════════════════════════════
# 决策规则在 `delta_hedge.py`（纯函数）；这里只负责：取价 / 取持仓 / 落账 / 记账本的净值与成交。
# 独立账本（不与 SPY 覆盖账混用：Alpha Bot 的 `account()` 取「第一条持仓」当 SPY）：
#   hedge_state/delta_positions.jsonl · delta_trades.jsonl · delta_equity_curve.jsonl · delta_meta.json
# 文件路径**调用时**由 STATE_DIR 解析（不是模块级常量）：测试把 STATE_DIR 指到 tmp 就整套跟着走。
# shadow（缺省）= 只算、写进审计文件的 `delta_hedge` 一节，不写任何账本文件；paper = 纸面成交。

DELTA_ROW_KIND = "delta_hedge"       # 聚合里与 SPY 覆盖行（kind="hedge"）区分：压力网格要按股票（带 β）处理它


def _delta_mode() -> str:
    mode = CONFIG.get("delta_hedge_mode")
    if mode not in dh.MODES:
        raise ValueError(f"delta_hedge_mode 只接受 {dh.MODES}，收到 {mode!r}")   # 拼错要当场红，不能静默当 off
    return mode


def _delta_paths() -> Dict[str, Path]:
    return {"positions": STATE_DIR / "delta_positions.jsonl", "trades": STATE_DIR / "delta_trades.jsonl",
            "equity": STATE_DIR / "delta_equity_curve.jsonl", "meta": STATE_DIR / "delta_meta.json"}


def _delta_opened() -> bool:
    return _delta_paths()["meta"].exists()


def _delta_load_meta() -> Dict:
    meta = ledger_io.load_json(_delta_paths()["meta"], default=None)
    if meta is not None:
        return meta
    return {"version": _VERSION, "starting_date": None, "cash": 0.0, "last_run_date": None, "config_snapshot": {}}


def _delta_save_meta(meta: Dict) -> None:
    ledger_io.write_json(_delta_paths()["meta"], _scrub(meta))


def _delta_positions() -> List[Dict]:
    return _load_jsonl(_delta_paths()["positions"])


def _delta_held_before_today(as_of: str) -> Tuple[Dict[str, Optional[float]], List[Dict]]:
    """逐票对冲持仓（**交易前**视角）+ 今天已成交的逐票成交。今天已成交的从持仓里减掉：同日重跑才能
    算出与第一次一模一样的决策（幂等，同 `_hedge_rows_before_today`）。
    持仓行的 shares 读不出来 ⇒ 该标的记 None（决策判 held_shares_unknown），不当 0。"""
    paths = _delta_paths()
    today = [t for t in _load_jsonl(paths["trades"]) if t.get("date") == as_of]
    held: Dict[str, Optional[float]] = {}
    for p in _delta_positions():
        tk = str(p.get("ticker") or "")
        if tk:
            held[tk] = _num(p.get("shares"))
    for t in today:
        tk, sh = str(t.get("ticker") or ""), _num(t.get("shares"))
        if not tk or sh is None:
            continue
        if held.get(tk, 0.0) is None:
            continue
        held[tk] = (held.get(tk) or 0.0) - sh
    return held, today


def delta_hedge_day(as_of: str, option_rows: List[Dict], nav: Optional[float], closes_fn) -> Optional[Dict]:
    """只读：今天每个标的的逐票对冲决策。mode=off ⇒ None。不写任何文件。
    shadow 模式没有持仓账（或账是空的）⇒ `trade_shares` 是「从空仓起」要成交的量，`target_shares` 才是无状态的目标。"""
    mode = _delta_mode()
    if mode == "off":
        return None
    held, today = _delta_held_before_today(as_of)
    straddle = dh.straddle_delta_by_name(option_rows)
    names = sorted(set(straddle) | {t for t, v in held.items() if v is None or v != 0.0})
    marks = {tk: _mark_from(closes_fn, tk, as_of) for tk in names}
    decisions = dh.decide(straddle, held, marks, nav=nav, band_pct=float(CONFIG["delta_hedge_band_pct"]),
                          rebalance_to=str(CONFIG["delta_hedge_rebalance_to"]))
    traded: Dict[str, float] = {}
    for t in today:
        traded[str(t.get("ticker"))] = traded.get(str(t.get("ticker")), 0.0) + (_num(t.get("shares")) or 0.0)
    for d in decisions:
        d["traded_today"] = traded.get(d["ticker"], 0.0)
    out = {"mode": mode, "band_pct": float(CONFIG["delta_hedge_band_pct"]),
           "rebalance_to": str(CONFIG["delta_hedge_rebalance_to"]), "nav": _pos(nav),
           "names": decisions, "executed": [], "blocked": {}}
    out.update(dh.summarize(decisions))
    return out


def _delta_effective_trade(d: Dict) -> int:
    """决策里**真能成交**的股数：要 hedge / exit，且价是 as_of 的官方收盘（盘中实时价只算不成交，同 SPY）。"""
    if d.get("status") in ("hedge", "exit") and d.get("price_at_close"):
        return int(d.get("trade_shares") or 0)
    return 0


def _delta_rows_after(as_of: str, section: Optional[Dict], beta_fn) -> List[Dict]:
    """paper 模式：把「今天对冲成交后」的逐票对冲持仓并进聚合（SPY 带看到的是对冲后的残余暴露）。
    只算真能成交的那部分（`_delta_effective_trade`）；价缺 / 陈旧的行带着缺价标记进来 ⇒ 聚合 partial ⇒ SPY 带 unknown。"""
    rows: List[Dict] = []
    if not section or section.get("mode") != "paper":
        return rows
    for d in section["names"]:
        held = d.get("held_shares") or 0.0
        after = held + _delta_effective_trade(d)
        if d.get("status") == "unknown" and d.get("reason") == "held_shares_unknown":
            after = None
        if after is not None and after == 0.0:
            continue
        row = _blank_row(d["ticker"], DELTA_ROW_KIND)
        row.update({"gamma_dollar_per_1pct": 0.0, "vega_dollar_per_pt": 0.0, "theta_dollar_per_day": 0.0,
                    "qty": after, "price": d.get("price"), "price_missing": d.get("price") is None,
                    "price_stale": bool(d.get("price_stale")), "price_session": d.get("price_session"),
                    "price_source": d.get("price_source")})
        if after is None or d.get("price") is None:
            row["price_missing"] = True
        else:
            row["dollar_delta"] = round(after * d["price"], 2)
        _apply_beta(row, beta_fn, as_of)
        rows.append(row)
    return rows


def delta_overlay_value(as_of: str, closes_fn, diag: Optional[Dict] = None) -> Optional[float]:
    """逐票对冲账净值 = cash + Σ shares × 当日价。没开过账不进 NAV（调用方判）；有仓但某只没价 ⇒ None。
    `diag`（可选）收集「没价」的标的（`diag["unmarked"]`），供 `compute_day` 判断 None 是不是只因某几只持仓标的缺价。"""
    meta = _delta_load_meta()
    cash = _num(meta.get("cash"))
    if cash is None:
        return None
    total = cash
    for p in _delta_positions():
        sh = _num(p.get("shares"))
        if sh is None:
            _log.error("[PortfolioGreeks] 逐票对冲账有 shares 非有限的仓位，账本净值不可得")
            return None
        if sh == 0:
            continue
        px = _mark_from(closes_fn, str(p.get("ticker") or ""), as_of)["price"]
        if px is None:
            if diag is not None:
                diag.setdefault("unmarked", []).append(str(p.get("ticker") or ""))
            continue                            # 点完名再统一判 None（多只缺价时一次说全）
        total += sh * px
    if diag is not None and diag.get("unmarked"):
        return None
    return total


def _execute_delta_trades(as_of: str, section: Dict) -> Tuple[List[Dict], Dict[str, str]]:
    """按当日收盘成交逐票对冲（无点差 / 冲击成本模型，明说不建模）。返回 (成交记录, {标的: 未成交原因})。
    只在拿到 as_of 官方收盘时成交；今天这只已成交过 ⇒ 跳过（同日重跑幂等）。成交记录自带定价场次证明，
    `load_history` 只认带证明且场次对的成交。调用方持 hedge_state 目录锁。"""
    paths = _delta_paths()
    blocked: Dict[str, str] = {}
    todo = []
    for d in section["names"]:
        if d.get("status") not in ("hedge", "exit") or not d.get("trade_shares"):
            continue
        if d.get("traded_today", 0.0) != 0.0:
            continue                                   # 今天已成交过：重跑不再交易
        if not d.get("price_at_close"):
            blocked[d["ticker"]] = (f"{d['ticker']} 的价不是 {_expected_session(as_of)} 的官方收盘（"
                                    f"{d.get('price_source')} @ {d.get('price_session')}）——成交按收盘记账，拒绝成交")
            continue
        todo.append(d)
    if not todo:
        return [], blocked
    meta = _delta_load_meta()
    cash = _num(meta.get("cash"))
    if cash is None:
        _log.error("[PortfolioGreeks] delta_meta.cash 非有限（%r）——拒绝逐票对冲成交", meta.get("cash"))
        return [], {**blocked, **{d["ticker"]: "delta_meta.cash 非有限，拒绝成交" for d in todo}}
    positions = {str(p.get("ticker")): dict(p) for p in _delta_positions()}
    trades: List[Dict] = []
    for d in todo:
        tk, shares, px = d["ticker"], int(d["trade_shares"]), float(d["price"])
        cur = positions.get(tk)
        old = (_num(cur.get("shares")) or 0.0) if cur else 0.0
        new = old + shares
        cash -= shares * px
        if not (math.isfinite(cash) and math.isfinite(new)):
            _log.error("[PortfolioGreeks] 逐票对冲成交算出非有限值（%s cash=%r shares=%r），拒绝入账", tk, cash, new)
            blocked[tk] = "成交算出非有限值，拒绝入账"
            return trades, blocked
        if new == 0:
            positions.pop(tk, None)
        else:
            old_avg = _num((cur or {}).get("avg_price")) or px
            same_side = old != 0 and (old > 0) == (new > 0)
            if same_side and abs(new) > abs(old):
                avg = (abs(old) * old_avg + abs(shares) * px) / abs(new)
            elif same_side:
                avg = old_avg
            else:
                avg = px
            positions[tk] = {"ticker": tk, "shares": new, "avg_price": round(avg, 4),
                             "entry_date": (cur or {}).get("entry_date") or as_of, "last_price": px,
                             "last_mark_date": as_of}
        trades.append({"date": as_of, "ticker": tk, "shares": shares, "price": px,
                       "action": "buy" if shares > 0 else "sell", "fill": "close",
                       "spread_model": "none (single-name spread/impact not modelled)",
                       "reason": d.get("status"), "option_dd": d.get("option_dd"),
                       "net_dd_before": d.get("net_dd"), "target_shares": d.get("target_shares"),
                       "shares_after": new, "price_session": d.get("price_session"),
                       "price_source": d.get("price_source"), "price_at_close": bool(d.get("price_at_close"))})
    _write_jsonl(paths["positions"], list(positions.values()))
    for t in trades:
        _append_jsonl(paths["trades"], t)
    if not meta.get("starting_date"):
        meta["starting_date"] = as_of
    meta["cash"] = cash
    _delta_save_meta(meta)
    for t in trades:
        _log.info("[PortfolioGreeks] 逐票对冲 → %s %s %+d @ %.2f", as_of, t["ticker"], t["shares"], t["price"])
    return trades, blocked


def _delta_snapshot(as_of: str, closes_fn) -> Dict:
    """逐票对冲账的净值快照（按日期去重）+ 盯市。每只持仓的价用 as_of 的 mark；有仓但某只没价 ⇒ 净值 None、
    `marks_complete=False`（不拿旧价估值）。`marks_at_close`：所有持仓的价都是官方收盘。"""
    meta = _delta_load_meta()
    cash = _num(meta.get("cash"))
    positions = _delta_positions()
    mv, complete, at_close, n = 0.0, True, True, 0
    for p in positions:
        sh = _num(p.get("shares"))
        if sh is None:
            complete = False
            continue
        if sh == 0:
            continue
        n += 1
        m = _mark_from(closes_fn, str(p.get("ticker") or ""), as_of)
        if m["price"] is None:
            complete = False
            continue
        mv += sh * m["price"]
        at_close = at_close and bool(m["at_close"])
        p["last_price"], p["last_mark_date"] = m["price"], as_of
    _write_jsonl(_delta_paths()["positions"], positions)      # 空仓也写（空文件）：开了账就必须有持仓文件
    nav = (cash + mv) if (cash is not None and complete) else None
    snap = {"date": as_of, "cash": _r(cash), "market_value": _r(mv) if complete else None, "nav": _r(nav),
            "n_positions": n, "marks_complete": complete, "marks_at_close": at_close and complete,
            "trades_today": sum(1 for t in _load_jsonl(_delta_paths()["trades"]) if t.get("date") == as_of)}
    equity = [e for e in _load_jsonl(_delta_paths()["equity"]) if e.get("date") != as_of]
    equity.append(snap)
    equity.sort(key=lambda e: e["date"])
    _write_jsonl(_delta_paths()["equity"], equity)
    if not meta.get("starting_date"):
        meta["starting_date"] = as_of
    meta["last_run_date"], meta["version"] = as_of, _VERSION
    meta["config_snapshot"] = dict(CONFIG)
    meta.setdefault("cash", 0.0)
    _delta_save_meta(meta)
    return snap


# ══════════════════════════════════════════════════════════════════════════════
# 聚合 / 合并 NAV / 对冲建议
# ══════════════════════════════════════════════════════════════════════════════

def aggregate(rows: List[Dict], nav: Optional[float]) -> Dict:
    """求和 + 折 % NAV + 覆盖率 + β·Delta 带状判定。
    band_status："unknown" 只要有任何一行的 β·$Delta 算不出来（缺价/缺报价/缺 β）——
    缺的那部分可能大到翻转结论，所以宁可说不知道。
    `n_price_stale`（v0.45.423）⊆ `n_price_missing`：价看到了，但不是 as_of 那一场的。"""
    nav = _pos(nav)
    sums = {k: 0.0 for k in _GREEK_KEYS}
    n_price = n_stale = n_quote = n_beta = n_incomplete_bdd = 0
    n_with_beta = 0
    for r in rows:
        for k in _GREEK_KEYS:
            v = _num(r.get(k))
            if v is not None:
                sums[k] += v
        n_price += bool(r.get("price_missing"))
        n_stale += bool(r.get("price_stale"))
        n_quote += bool(r.get("quote_missing"))
        n_beta += bool(r.get("beta_missing"))
        n_with_beta += _num(r.get("beta")) is not None
        n_incomplete_bdd += _num(r.get("beta_dollar_delta")) is None
    n_rows = len(rows)
    pct = {k: (round(sums[k] / nav * 100.0, 4) if nav else None) for k in _GREEK_KEYS}
    coverage = {"n_rows": n_rows, "n_price_missing": n_price, "n_price_stale": n_stale,
                "n_quote_missing": n_quote,
                "n_beta_missing": n_beta, "n_beta_dd_incomplete": n_incomplete_bdd,
                "beta_coverage": (round(n_with_beta / n_rows, 4) if n_rows else None)}
    partial = bool(n_price or n_quote or n_beta)
    target, band = CONFIG["beta_delta_target_pct"], CONFIG["beta_delta_band_pct"]
    if nav is None or n_incomplete_bdd > 0 or n_rows == 0:
        status = "unknown" if n_rows else "empty"
    else:
        p = pct["beta_dollar_delta"]
        status = "above" if p > target + band else ("below" if p < target - band else "inside")
    vega_alert = (nav is not None and pct["vega_dollar_per_pt"] is not None
                  and abs(pct["vega_dollar_per_pt"]) > CONFIG["vega_alert_pct"])
    gamma_alert = (nav is not None and pct["gamma_dollar_per_1pct"] is not None
                   and abs(pct["gamma_dollar_per_1pct"]) > CONFIG["gamma_alert_pct"])
    return {
        "nav": nav,
        "sums": {k: round(v, 2) for k, v in sums.items()},
        "pct_nav": pct,
        "coverage": coverage,
        "partial": partial,
        "band_status": status,
        "band": {"target_pct": target, "band_pct": band,
                 "lower_pct": target - band, "upper_pct": target + band},
        "vega_alert": bool(vega_alert),
        "gamma_alert": bool(gamma_alert),
        "by_kind": {kind: sum(1 for r in rows if r.get("kind") == kind)
                    for kind in ("stock", "option", "hedge", DELTA_ROW_KIND)},
    }


def _latest_nav(path: Path, as_of: str) -> Optional[float]:
    best = None
    for e in _load_jsonl(path):
        d = str(e.get("date") or "")
        if d and d <= as_of and (best is None or d > best[0]):
            best = (d, _num(e.get("nav")))
    return best[1] if best else None


def _book_never_started(equity_file, positions_file) -> bool:
    """这本账**从未启动**：净值文件与持仓文件都不存在，或存在但一条可解析记录都没有。
    两个都要看——单看净值文件的话，「文件被删」与「从未开张」输出完全一样。"""
    for f in (equity_file, positions_file):
        try:
            if Path(f).exists() and _load_jsonl(Path(f)):
                return False
        except OSError as exc:
            _log.warning("[PortfolioGreeks] %s 不可读，不敢当作「从未启动」: %s", f, exc)
            return False
    return True


def hedge_overlay_value(as_of: str, spy_price: Optional[float]) -> Optional[float]:
    """覆盖账本净值 = cash + shares × SPY 价。没开过仓 → 0.0；有仓但没价 → None。"""
    meta = _load_meta()
    cash = _num(meta.get("cash"))
    if cash is None:
        return None
    raw = [_num(p.get("shares")) for p in _hedge_positions()]
    if any(v is None for v in raw):
        # 有一行 shares 读不出来 → 覆盖账本市值未知。`or 0.0` 会把它算成 0 股，
        # 于是净值看着完好、合并 NAV 少一块、Greeks 分子却还在——分母被做小、比率被放大。
        _log.error("[PortfolioGreeks] 对冲账本有 shares 非有限的仓位，覆盖账本净值不可得")
        return None
    shares = sum(raw)
    if shares == 0:
        return cash
    px = _pos(spy_price)
    return None if px is None else cash + shares * px


def combined_nav_detail(as_of: str, closes_fn: Optional[Callable[[str, str], Optional[float]]] = None,
                        spy_price: Optional[float] = None) -> Dict:
    """三分量合并 NAV；任一缺失 → nav=None 且 `missing` 列出是哪一个。
    跨式腿的 10 万起始资本是名义的（测量账本），合并进来只为让百分比有一个分母。"""
    import paper_portfolio as pp
    import options_paper_leg as opl
    closes_fn = closes_fn or _default_mark
    comps: Dict[str, Optional[float]] = {
        "stock_book": _latest_nav(pp.EQUITY_FILE, as_of),
        "straddle_leg": _latest_nav(opl.EQUITY_FILE, as_of),
    }
    # 跨式腿**从未启动** ≠ 「今天缺一行」。前者是合法的零状态：没有期权仓，对合并 NAV
    # 与 Greeks 的贡献确实是 0，按 0 计并标注 not_started，否则对冲在腿建账之前永远 unknown。
    # 但「从未启动」要由**净值文件与持仓文件双双空**来证明，只看净值文件不够（二次复查修正）：
    #   ① 净值文件被删/状态目录指错 → 输出与「从未启动」逐字节相同，$98,000 的账凭空消失；
    #   ② 更糟的是持仓还在、净值文件没了：这些仓的 Greeks 进了分子、NAV 却按 0 进分母，
    #      partial=False、band_status="above"，照样下单——分母被做小的比率是要成交的。
    # 有持仓却没净值行，那是数据缺失，只能 None 并点名。
    not_started: List[str] = []
    if comps["straddle_leg"] is None and _book_never_started(opl.EQUITY_FILE, opl.POSITIONS_FILE):
        comps["straddle_leg"] = 0.0
        not_started.append("straddle_leg")
    if spy_price is None:
        # 陈旧的 SPY 价 → None → 有仓时覆盖账本净值不可得（不拿昨收给今天的账估值）
        spy_price = _mark_from(closes_fn, CONFIG["hedge_instrument"], as_of)["price"]
    comps["hedge_overlay"] = hedge_overlay_value(as_of, spy_price)
    if _delta_opened() or _delta_mode() == "paper":
        # 逐票对冲账（v0.45.526）：开过账（delta_meta.json 在）或 paper 模式才进 NAV——paper 模式第一次跑时账还没开，
        # 若这时不加这一项，第一次与同日重跑的 NAV 分量（以及审计文件）就不一样；没开账的价值是 0.0（无仓、现金 0）。
        # shadow / off 且没开过账 ⇒ 不加这一项，与旧行为逐字节相同
        delta_diag: Dict = {}
        comps["delta_overlay"] = delta_overlay_value(as_of, closes_fn, delta_diag)
    else:
        delta_diag = None
    missing = [k for k, v in comps.items() if v is None]
    nav = None if missing else sum(comps.values())  # type: ignore[arg-type]
    out = {"nav": (round(nav, 2) if nav is not None else None), "components": comps,
           "missing": missing, "not_started": not_started, "spy_price": spy_price}
    if delta_diag is not None:
        out["delta_overlay_unmarked"] = list(delta_diag.get("unmarked") or [])
    return out


def combined_nav(as_of: str, closes_fn: Optional[Callable[[str, str], Optional[float]]] = None) -> Optional[float]:
    d = combined_nav_detail(as_of, closes_fn)
    if d["nav"] is None:
        _log.warning("[PortfolioGreeks] %s 合并 NAV 不可得，缺：%s", as_of, ", ".join(d["missing"]))
    return d["nav"]


def hedge_recommendation(agg: Dict, spy_price: Optional[float], nav: Optional[float]) -> Dict:
    """带外 → 用 SPY 股票拉回（edge：最近带边；center：带中心）。
    spy_shares 负 = 卖出 SPY（组合太多头），正 = 买入。edge 模式股数向外取整（ceil），
    否则四舍五入会留半股在带外、第二天再来一笔 1 股的小交易。
    inside / unknown / empty 一律 hold，reason 说清楚缺什么——**不在部分数据上对冲**。"""
    status = agg.get("band_status")
    nav = _pos(nav)
    base = {"action": "hold", "spy_shares": 0, "excess_usd": None, "target_usd": None,
            "band_status": status, "rebalance_to": CONFIG["rebalance_to"],
            "beta_dd_usd": (agg.get("sums") or {}).get("beta_dollar_delta"),
            "beta_dd_pct": (agg.get("pct_nav") or {}).get("beta_dollar_delta"),
            "spy_price": _pos(spy_price)}
    if status == "unknown":
        cov = agg.get("coverage") or {}
        why = []
        if nav is None:
            why.append("nav unavailable")
        if cov.get("n_price_missing"):
            stale = cov.get("n_price_stale") or 0
            why.append(f"{cov['n_price_missing']} price missing"
                       + (f" ({stale} stale: not the as_of session's price)" if stale else ""))
        if cov.get("n_quote_missing"):
            why.append(f"{cov['n_quote_missing']} quote missing")
        if cov.get("n_beta_missing"):
            why.append(f"{cov['n_beta_missing']} beta missing")
        base["reason"] = "partial data — no hedge on partial data (" + ", ".join(why or ["unknown"]) + ")"
        return base
    if status == "empty":
        base["reason"] = "no positions"
        return base
    if status == "inside":
        base["reason"] = (f"β·Δ {base['beta_dd_pct']:+.2f}% NAV inside band "
                          f"[{agg['band']['lower_pct']:+.0f}%, {agg['band']['upper_pct']:+.0f}%]")
        return base
    if nav is None or base["spy_price"] is None:
        base["reason"] = f"band {status} but " + ("nav unavailable" if nav is None else "SPY price unavailable")
        return base
    band = agg["band"]
    if CONFIG["rebalance_to"] == "center":
        target_pct = band["target_pct"]
    else:
        target_pct = band["upper_pct"] if status == "above" else band["lower_pct"]
    target_usd = target_pct / 100.0 * nav
    excess = base["beta_dd_usd"] - target_usd
    mag = abs(excess) / base["spy_price"]
    shares = int(math.ceil(mag - 1e-9)) if CONFIG["rebalance_to"] != "center" else int(round(mag))
    spy_shares = -shares if excess > 0 else shares
    action = "sell_spy" if spy_shares < 0 else ("buy_spy" if spy_shares > 0 else "hold")
    base.update({"action": action, "spy_shares": spy_shares, "excess_usd": round(excess, 2),
                 "target_usd": round(target_usd, 2), "target_pct": target_pct,
                 "reason": (f"β·Δ {base['beta_dd_pct']:+.2f}% NAV {status} band → rebalance to "
                            f"{CONFIG['rebalance_to']} ({target_pct:+.0f}% = ${target_usd:,.0f}); "
                            f"excess ${excess:,.0f} / SPY ${base['spy_price']:.2f} = {spy_shares:+d} sh")})
    return base


# ══════════════════════════════════════════════════════════════════════════════
# 压力测试：现货 × IV 联合网格
# ══════════════════════════════════════════════════════════════════════════════

def _no_price_label(row: Dict) -> str:
    """剔除标签里说清「没价」还是「价不是 as_of 那一场的」——两者的排查方向完全不同。"""
    return "stale price" if row.get("price_stale") else "no price"


def stress_table(rows: List[Dict], spy_price: Optional[float] = None) -> Dict:
    """网格 stress_spot_pct × stress_iv_pts → 组合 P&L。
        股票：dollar_delta × β × shock            （β 缺 → 剔除，该格 partial）
        期权：qty × [BS(S·(1+β·shock), K, T, r, iv + pts/100) − mid]，T=dte/365
        SPY：dollar_delta × shock
    (0,0) 格股票恒为 0；期权在 (0,0) 是 BS 价 − mid 的模型基差，逐合约列在 bs_vs_mid_gap。
    剔除的行进 `excluded`，其毛 |$Delta| 进 `excluded_dollar_delta`（也挂到 worst_cell 上）；
    IV 轴被 0 地板托住的格标 `iv_clamped` + 实际施加的 `iv_pts_effective`。
    已到期（dte<=0）的合约剔除，不重定价——理由写在下面剔除处。"""
    from greeks_engine import bs_price
    IV_FLOOR = 0.0001
    spots = list(CONFIG["stress_spot_pct"])
    ivs = list(CONFIG["stress_iv_pts"])
    r = CONFIG["risk_free"]
    cells: List[Dict] = []
    gaps: List[Dict] = []
    excluded: List[str] = []
    usable: List[Tuple[str, Dict]] = []
    excl_dd = 0.0            # 被剔除行的 |$Delta| 合计（**毛额**：正负互抵会把规模抹平）
    excl_dd_unknown = 0      # 连 $Delta 都算不出来的剔除行数（缺价/缺报价）

    def _exclude(row: Dict, label: str) -> None:
        """剔一行的同时把它的规模记下来。只记 label 的话，worst_cell 里那个温和的数字
        就没人对得上账了——$100 万无 β 的股票被剔掉后，最差格能小 100 倍。"""
        nonlocal excl_dd, excl_dd_unknown
        excluded.append(label)
        dd = _num(row.get("dollar_delta"))
        if dd is None:
            excl_dd_unknown += 1
        else:
            excl_dd += abs(dd)

    for row in rows:
        kind = row.get("kind")
        if kind == "hedge":
            dd = _num(row.get("dollar_delta"))
            if dd is None:
                dd = (_num(row.get("qty")) or 0.0) * (_pos(spy_price) or 0.0) if _pos(spy_price) else None
            if dd is None:
                _exclude(row, f"{row.get('ticker')}(hedge:{_no_price_label(row)})")
                continue
            usable.append(("hedge", {"dd": dd}))
        elif kind in ("stock", DELTA_ROW_KIND):
            dd, beta = _num(row.get("dollar_delta")), _num(row.get("beta"))
            if dd is None or beta is None:
                _exclude(row, f"{row.get('ticker')}({kind}:{_no_price_label(row) if dd is None else 'no beta'})")
                continue
            usable.append(("stock", {"dd": dd, "beta": beta}))
        elif kind == "option":
            need = {k: _num(row.get(k)) for k in ("qty", "price", "strike", "iv", "mid", "beta")}
            dte = _num(row.get("dte"))     # 过 _num：NaN 的 dte 躲得过 `is None`，却会让 int() 直接崩
            if any(v is None for v in need.values()) or dte is None or row.get("cp") not in ("call", "put"):
                miss = [("mid(bid=0)" if (k == "mid" and row.get("bid_zero")) else k)
                        for k, v in need.items() if v is None] + (["dte"] if dte is None else [])
                _exclude(row, f"{row.get('symbol') or row.get('ticker')}(option:{','.join(miss) or 'cp'})")
                continue
            if dte <= 0:
                # 已到期的合约剔除，**不**重新定价。原来 T=max(int(dte),1)/365 会把一张
                # 过期 5 天的合约当成「还剩 1 天」的活合约报价，凭空发明时间价值，
                # 且不剔除、不标记——网格看着完整。
                # 为什么是剔除而不是按内在价值 max(S−K,0)·qty 重估：过期腿的真实结果
                # 取决于结算与平仓约定（有没有被行权、跨式腿账本何时把它移出持仓），
                # 那是 options_paper_leg 的信息，本模块既不管也拿不到；在这里编一个
                # 内在价值只是用一个猜测换另一个猜测，而且更像真的。剔除会让整张网格
                # partial、行名进 excluded、规模进 excluded_dollar_delta——大声说不知道。
                _exclude(row, f"{row.get('symbol') or row.get('ticker')}(option:expired {int(dte)}d)")
                continue
            T = int(dte) / 365.0
            base_bs = bs_price(need["price"], need["strike"], T, r, need["iv"], row["cp"])
            gaps.append({"symbol": row.get("symbol"), "bs": round(base_bs, 4), "mid": need["mid"],
                         "gap": round(base_bs - need["mid"], 4), "T_days": int(dte)})
            usable.append(("option", {**need, "T": T, "cp": row["cp"]}))
    partial = bool(excluded)
    worst = None
    for sp in spots:
        for ivp in ivs:
            pnl = 0.0
            clamped: List[float] = []
            for kind, u in usable:
                shock = sp / 100.0
                if kind == "hedge":
                    pnl += u["dd"] * shock
                elif kind == "stock":
                    pnl += u["dd"] * u["beta"] * shock
                else:
                    S1 = u["price"] * (1.0 + u["beta"] * shock)
                    iv_shocked = u["iv"] + ivp / 100.0      # ivp 是 vol 点：−10pt = IV −0.10
                    iv1 = max(iv_shocked, IV_FLOOR)
                    if iv1 > iv_shocked:
                        # 地板托住了：这张合约实际吃到的冲击比列标签小（IV 8% 的票吃不下
                        # −10pt）。原来这里静默截断，格子照样标 −10pt——标签在说谎。
                        clamped.append(round((iv1 - u["iv"]) * 100.0, 4))
                    pnl += u["qty"] * (bs_price(S1, u["strike"], u["T"], r, iv1, u["cp"]) - u["mid"])
            cell = {"spot_pct": sp, "iv_pts": ivp, "pnl": round(pnl, 2), "partial": partial,
                    "iv_clamped": bool(clamped)}
            if clamped:
                cell["iv_pts_effective"] = min(clamped, key=abs)   # 截得最狠的那张（|冲击| 最小）
                cell["n_iv_clamped"] = len(clamped)
            cells.append(cell)
            if worst is None or pnl < worst["pnl"]:
                worst = dict(cell)
    zero = next((c["pnl"] for c in cells if c["spot_pct"] == 0 and c["iv_pts"] == 0), None)
    out = {"spot_pct": spots, "iv_pts": ivs, "cells": cells, "worst_cell": worst,
           "pnl_at_zero": zero, "bs_vs_mid_gap": gaps, "n_used": len(usable),
           "excluded": excluded, "partial": partial,
           "excluded_dollar_delta": round(excl_dd, 2), "excluded_dd_unknown": excl_dd_unknown,
           "n_iv_clamped_cells": sum(1 for c in cells if c.get("iv_clamped"))}
    if worst is not None and partial:
        # 最差格那个金额会被单独引用（日报里就是一行字），所以把「它少算了多少敞口」
        # 贴在它自己身上，而不是指望读者去翻 excluded 列表。
        worst["excluded_dollar_delta"] = round(excl_dd, 2)
        worst["excluded_rows"] = len(excluded)
        worst["excluded_dd_unknown"] = excl_dd_unknown
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 计算一天（纯读）→ 执行（写账本）
# ══════════════════════════════════════════════════════════════════════════════

def _hedge_rows_before_today(as_of: str, closes_fn, spy_price: Optional[float],
                             spy_mark: Optional[Dict] = None) -> Tuple[List[Dict], Optional[Dict]]:
    """今天已经成交过的话，把那笔从持仓里减掉，重建「交易前」视角——同日重跑才能算出
    与第一次一模一样的建议与审计文件（幂等）。返回 (rows_before, today_trade | None)。"""
    trades = [t for t in _load_jsonl(TRADES_FILE) if t.get("date") == as_of]
    today = trades[-1] if trades else None
    positions = _hedge_positions()
    if today is not None:
        traded = _num(today.get("shares")) or 0.0
        tk = today.get("ticker") or CONFIG["hedge_instrument"]
        found = False
        adj = []
        for p in positions:
            if p.get("ticker") == tk:
                found = True
                adj.append({**p, "shares": (_num(p.get("shares")) or 0.0) - traded})
            else:
                adj.append(p)
        if not found:
            adj.append({"ticker": tk, "shares": -traded, "avg_price": _num(today.get("price"))})
        positions = adj
    return hedge_exposures(as_of, closes_fn, positions=positions, spy_price=spy_price,
                           spy_mark=spy_mark), today


_LAST_PRICE_CHECK: Optional[Dict] = None
_PRICE_CHECK_LIST_MAX = 20      # 进 status.json 的明细条数上限（计数不截）


def price_check_stats() -> Optional[Dict]:
    """本进程最近一次 compute_day / run_for_date 的标的价场次核对（v0.45.423）。
    `scan_timing.counters()["portfolio_greeks"]` 读它 → status.json → alert_manager（陈旧价 / 拒绝成交 /
    数据不全致对冲决定做不出来 → P2；后者 v0.45.435）。
    本进程没跑过 → None：None 是「没测到」，{} 会被读成「测过为零」。"""
    return dict(_LAST_PRICE_CHECK) if _LAST_PRICE_CHECK is not None else None


def _hedge_undecided(rec: Optional[Dict]) -> Optional[str]:
    """今天的对冲决定**做不出来**（不是「决定不对冲」）⇒ 原因；做得出来 ⇒ None。

    两种：带状判 unknown（缺价 / 缺报价 / 缺 β / 缺 NAV，任一行算不出 β·$Delta）；判出带外却缺 NAV / SPY 价
    下不了单。inside（不用对冲）/ empty（没仓）是决定，不算。v0.45.435：此前只有「价陈旧」会红——
    2026-09-15~25 生产连续 7 天 unknown（BRK-B 的 β 取不到 5 天、09-24/25 SPY 价与全部期权报价取不到），
    零告警，覆盖层整段停摆没人知道。"""
    rec = rec or {}
    status = rec.get("band_status")
    # 带外却 hold：只有「缺 NAV / SPY 价算不出目标」（target_usd 为 None）才算做不出来；center 模式股数四舍五入成 0
    # 是算出来了、只是不用动（v0.45.442 二次检查：原条件把它也报成「数据不全」）
    if status == "unknown" or (status in ("above", "below") and rec.get("action") == "hold"
                               and rec.get("target_usd") is None):
        return str(rec.get("reason") or status)
    return None


def _price_check(as_of: str, rows: List[Dict], spy_mark: Dict, rec: Optional[Dict] = None,
                 nav_missing: Optional[List[str]] = None) -> Dict:
    """这一天的每个价是不是 as_of 那一场的，以及对冲决定做没做得出来。
    审计文件、日报、status.json、alert_manager 读的都是这一份。

    `unpriced`（v0.45.435）= 两源都没给出价（不是陈旧：根本没看到价）、或持仓记录不完整的行；
    `quote_missing` 不含报价错场（那是 `quote_stale`）。`hedge_undecided` 见 `_hedge_undecided`。"""
    def _ids(pred) -> List[str]:
        return list(dict.fromkeys(str(r.get("symbol") or r.get("ticker")) for r in rows if pred(r)))
    stale = [{"ticker": r.get("ticker"), "kind": r.get("kind"), "symbol": r.get("symbol"),
              "seen_price": r.get("stale_price"), "session": r.get("price_session"),
              "source": r.get("price_source")}
             for r in rows if r.get("price_stale")]
    unpriced = [{"ticker": r.get("ticker"), "kind": r.get("kind"), "symbol": r.get("symbol"),
                 "source": r.get("price_source")}
                for r in rows if r.get("price_missing") and not r.get("price_stale")]
    quote_stale = sorted({str(r.get("symbol") or r.get("ticker")) for r in rows if r.get("quote_stale")})
    quote_missing = _ids(lambda r: r.get("quote_missing") and not r.get("quote_stale"))
    bid_zero = _ids(lambda r: r.get("bid_zero"))
    beta_missing = list(dict.fromkeys(str(r.get("ticker")) for r in rows if r.get("beta_missing")))
    chk = {"as_of": as_of, "expected_session": _expected_session(as_of), "version": _VERSION,
            "n_rows": len(rows), "n_priced": sum(1 for r in rows if _pos(r.get("price")) is not None),
            "n_stale": len(stale), "stale": stale[:_PRICE_CHECK_LIST_MAX],
            "n_quote_stale": len(quote_stale), "quote_stale": quote_stale[:_PRICE_CHECK_LIST_MAX],
            "n_unpriced": len(unpriced), "unpriced": unpriced[:_PRICE_CHECK_LIST_MAX],
            "n_quote_missing": len(quote_missing), "quote_missing": quote_missing[:_PRICE_CHECK_LIST_MAX],
            "n_bid_zero": len(bid_zero), "bid_zero": bid_zero[:_PRICE_CHECK_LIST_MAX],
            "n_beta_missing": sum(1 for r in rows if r.get("beta_missing")),
            "beta_missing": beta_missing[:_PRICE_CHECK_LIST_MAX],
            "nav_missing": list(nav_missing or []),
            "hedge_undecided": _hedge_undecided(rec),
            "spy": {k: spy_mark.get(k) for k in ("price", "seen_price", "stale", "source", "session",
                                                   "at_close", "live")},
            "execution_blocked": None}
    chk["gaps"] = _data_gaps(chk)      # 人话版只在这里生成一次：status.json 带着走，告警 / 摘要行不各写一份
    return chk


def _delta_price_check(section: Optional[Dict]) -> Dict:
    """`price_check["delta_hedge"]`：审计 / status.json / alert_manager 读的逐票对冲摘要（mode=off ⇒ 全 0）。
    shadow 下未决只记日志；paper 下 alert_manager 报 P2——逐票对冲停摆不能无人知道。"""
    sec = section or {}
    return {"mode": sec.get("mode") or "off", "n_names": int(sec.get("n_names") or 0),
            "n_hedge": int(sec.get("n_hedge") or 0), "n_inside": int(sec.get("n_inside") or 0),
            "n_undecided": int(sec.get("n_undecided") or 0),
            "undecided": list(sec.get("undecided") or [])[:_PRICE_CHECK_LIST_MAX],
            "n_blocked": len(sec.get("blocked") or {}), "blocked": dict(sec.get("blocked") or {})}


def _data_gaps(chk: Dict) -> List[str]:
    """`price_check` 里「不是陈旧、是缺」的那几类，逐条写成人话（ERROR 日志 / 告警 / 摘要行读 `gaps`）。"""
    out = []
    if chk.get("n_unpriced"):
        out.append(f"取不到价 {chk['n_unpriced']} 行（"
                   + ",".join(dict.fromkeys(str(u.get("symbol") or u.get("ticker")) for u in chk.get("unpriced") or []))
                   + "）")
    if chk.get("n_quote_missing"):
        out.append(f"缺报价 {chk['n_quote_missing']} 张")
    if chk.get("n_beta_missing"):
        out.append(f"缺 β {chk['n_beta_missing']} 行（" + ",".join(chk.get("beta_missing") or []) + "）")
    if chk.get("nav_missing"):
        out.append("NAV 缺 " + ",".join(chk["nav_missing"]))
    return out


def compute_day(as_of: str, closes_fn=None, quotes_fn=None, beta_fn=None) -> Dict:
    """只读：暴露行 + 聚合 + 建议 + 压力表。不写任何账本文件（β 缓存除外——那是缓存）。
    v0.45.423：每个标的价都要是 as_of 那一场的（`_resolve_mark`）；不是的行判陈旧、不进 $Delta，
    结果带 `price_check`，有陈旧就打 ERROR——不再让 5 天容差把「昨天的价」改写成「正常」。"""
    global _LAST_PRICE_CHECK
    closes_fn = closes_fn or _default_mark
    if not getattr(closes_fn, "_memoized", False):
        closes_fn = _memo_marks(closes_fn)
    quotes_fn = quotes_fn or _default_quotes
    beta_fn = beta_fn or _default_beta
    spy_mark = _mark_from(closes_fn, CONFIG["hedge_instrument"], as_of)
    spy_price = spy_mark["price"]
    excl = _excluded_tickers()
    stock_rows = stock_exposures(as_of, closes_fn=closes_fn, beta_fn=beta_fn, exclude=excl)
    option_rows = option_exposures(as_of, quotes_fn=quotes_fn, beta_fn=beta_fn, closes_fn=closes_fn, exclude=excl)
    hedge_rows, today_trade = _hedge_rows_before_today(as_of, closes_fn, spy_price, spy_mark=spy_mark)
    nav_d = combined_nav_detail(as_of, closes_fn, spy_price=spy_price)
    excluded = excluded_exposure(nav_d["nav"])
    for e in (excluded or {}).get("names") or []:
        # WARNING 不是 ERROR：这是配置里写明的取舍；但每天都留一行，估算值跟着走，敞口变大时看得见
        _log.warning("[PortfolioGreeks] %s：%s 在 hedge_exclude_tickers 里，不进对冲计算（股票仓 %d 笔，按建仓价估 %s"
                     "，占 NAV %s；跨式 %d 笔）", as_of, e["ticker"], e["n_stock_positions"],
                     (f"${e['stock_usd_est']:+,.0f}" if e["stock_usd_est"] is not None else "不可估"),
                     (f"{e['stock_pct_nav_est']:+.2f}%" if e["stock_pct_nav_est"] is not None else "—"),
                     e["n_straddles"])
    # 逐票 Delta 对冲先于 SPY 带：paper 模式下 SPY 带看到的是逐票对冲**之后**的残余暴露；shadow / off 不进聚合
    # 逐票带宽的分母（v0.45.529）：NAV 缺的**只有** delta_overlay 且原因是某几只持仓标的缺价 ⇒ 带宽用「不含这一块」的 NAV。
    # 否则一只持仓标的的陈旧价（盘中陈旧文件约每 30 只里有 1 只）会把 NAV 整体做成 None，连价格正常的标的也判未知——
    # 逐标的隔离就成了空话。overlay 自己只是对冲仓的盈亏（成交瞬间 NAV 中性），相对 15 万的 NAV 是个零头；
    # SPY 带的 NAV 仍按「任一分量缺失 ⇒ None」判（不改）。
    band_nav, nav_basis = nav_d["nav"], "full"
    if (band_nav is None and nav_d.get("missing") == ["delta_overlay"] and nav_d.get("delta_overlay_unmarked")):
        band_nav = sum(v for v in nav_d["components"].values() if v is not None)
        nav_basis = "excluding_unmarked_delta_overlay"
    delta_section = delta_hedge_day(as_of, option_rows, band_nav, closes_fn)
    if delta_section is not None:
        delta_section["nav_basis"] = nav_basis
        if nav_basis != "full":
            delta_section["nav_unmarked"] = list(nav_d.get("delta_overlay_unmarked") or [])
    delta_rows = _delta_rows_after(as_of, delta_section, beta_fn)
    rows = stock_rows + option_rows + hedge_rows + delta_rows
    # NAV 不分「交易前/后」：成交价=收盘价，−shares×px 的现金腿与 +shares×px 的市值腿
    # 恰好抵消，覆盖净值在交易瞬间不变——所以同日重跑用当前状态算 NAV 也与第一次一致。
    agg = aggregate(rows, nav_d["nav"])
    rec = hedge_recommendation(agg, spy_price, nav_d["nav"])
    stress = stress_table(rows, spy_price)
    chk = _price_check(as_of, rows, spy_mark, rec, nav_d.get("missing"))
    gaps = chk["gaps"]
    if chk["n_stale"] or chk["n_quote_stale"] or spy_mark["stale"]:
        bits = []
        if spy_mark["stale"]:
            bits.append(f"SPY 看到 {spy_mark['seen_price']} @ {spy_mark['session']}［{spy_mark['source']}］")
        for s in dict.fromkeys((s["ticker"], s["kind"], s["seen_price"], s["session"], s["source"])
                               for s in chk["stale"]):
            bits.append(f"{s[0]}({s[1]}) 看到 {s[2]} @ {s[3]}［{s[4]}］")
        if chk["n_quote_stale"]:
            bits.append(f"{chk['n_quote_stale']} 张合约的报价属于别的场次")
        _log.error("[PortfolioGreeks] %s：标的价不属于 %s 这一场——%s。这些行不进 $Delta / 净值 / 成交，"
                   "今日不据此对冲%s", as_of, chk["expected_session"], "；".join(bits),
                   ("；另缺：" + "；".join(gaps)) if gaps else "")
    elif chk["hedge_undecided"]:
        _log.error("[PortfolioGreeks] %s：数据不全，今日对冲决定做不出来（%s）——%s", as_of,
                   chk["hedge_undecided"], "；".join(gaps) or "见 recommendation.reason")
    chk["delta_hedge"] = _delta_price_check(delta_section)
    if chk["delta_hedge"]["n_undecided"]:
        _log.log(logging.ERROR if chk["delta_hedge"]["mode"] == "paper" else logging.WARNING,
                 "[PortfolioGreeks] %s：逐票 Delta 对冲有 %d 只决定做不出来（%s）", as_of,
                 chk["delta_hedge"]["n_undecided"],
                 "；".join(f"{u['ticker']}:{u['reason']}" for u in chk["delta_hedge"]["undecided"]))
    _LAST_PRICE_CHECK = chk
    return {"as_of": as_of, "version": _VERSION, "spy_price": spy_price, "spy_mark": spy_mark,
            "nav": nav_d, "rows": rows, "aggregate": agg, "recommendation": rec, "stress": stress,
            "today_trade": today_trade, "price_check": chk, "delta_hedge": delta_section,
            "excluded_from_hedge": excluded}


def _execute_trade(as_of: str, rec: Dict, agg: Dict, nav: Optional[float],
                   spy_mark: Optional[Dict] = None) -> Optional[Dict]:
    """按当日收盘成交 SPY（无点差模型：SPY 点差 ~1 bp，明说不建模）。返回成交记录。
    v0.45.440：成交记录自带成交价的场次证明（`price_session` / `price_source` / `price_at_close`，取自
    `run_for_date` 放行成交的那个 SPY mark）——`load_history()` 只认带证明且场次对的成交。"""
    shares = int(rec.get("spy_shares") or 0)
    px = _pos(rec.get("spy_price"))
    if shares == 0 or px is None:
        return None
    meta = _load_meta()
    cash = _num(meta.get("cash"))
    if cash is None:
        _log.error("[PortfolioGreeks] meta.cash 非有限（%r）——拒绝成交", meta.get("cash"))
        return None
    positions = _hedge_positions()
    tk = CONFIG["hedge_instrument"]
    cur = next((p for p in positions if p.get("ticker") == tk), None)
    old = _num(cur.get("shares")) if cur else 0.0
    old = old or 0.0
    new = old + shares
    cash -= shares * px
    if not math.isfinite(cash) or not math.isfinite(new):
        _log.error("[PortfolioGreeks] 成交算出非有限值（cash=%r shares=%r），拒绝入账", cash, new)
        return None
    rest = [p for p in positions if p.get("ticker") != tk]
    if new != 0:
        old_avg = _num((cur or {}).get("avg_price")) or px
        same_side = old != 0 and (old > 0) == (new > 0)
        if same_side and abs(new) > abs(old):        # 加仓：数量加权均价
            avg = (abs(old) * old_avg + abs(shares) * px) / abs(new)
        elif same_side:                               # 减仓：均价不变
            avg = old_avg
        else:                                         # 翻向：新仓从成交价起算
            avg = px
        rest.append({"ticker": tk, "shares": new, "avg_price": round(avg, 4),
                     "entry_date": (cur or {}).get("entry_date") or as_of, "last_price": px,
                     "last_mark_date": as_of})
    _write_jsonl(POSITIONS_FILE, rest)
    trade = {"date": as_of, "ticker": tk, "shares": shares, "price": px, "action": rec.get("action"),
             "fill": "close", "spread_model": "none (SPY ~1bp, not modelled)",
             "excess_usd": rec.get("excess_usd"), "target_usd": rec.get("target_usd"),
             "beta_dd_usd_before": rec.get("beta_dd_usd"), "beta_dd_pct_before": rec.get("beta_dd_pct"),
             "nav": nav, "shares_after": new, "reason": rec.get("reason"),
             "price_session": (spy_mark or {}).get("session"), "price_source": (spy_mark or {}).get("source"),
             "price_at_close": bool((spy_mark or {}).get("at_close"))}
    _append_jsonl(TRADES_FILE, trade)
    if not meta.get("starting_date"):
        meta["starting_date"] = as_of
    meta["cash"] = cash
    _save_meta(meta)
    _log.info("[PortfolioGreeks] → %s %+d SPY @ %.2f（%s）", as_of, shares, px, rec.get("reason"))
    return trade


def run_for_date(as_of: str, closes_fn=None, quotes_fn=None, beta_fn=None, execute: bool = True) -> Dict:
    """见 `_run_for_date`。v0.45.442：任何异常先记进 `price_check_stats()`（`error` 键）再原样抛出——
    日报把它当「非致命」吞成一行 WARNING，`_LAST_PRICE_CHECK` 又只在 compute_day 末尾才赋值 ⇒ 此前
    status.json 里这一项是 None，alert_manager 只记 checks_skipped，**覆盖层整轮没跑也不红**。"""
    global _LAST_PRICE_CHECK
    try:
        if not execute:                                   # 只算、不写账本：不必占锁
            return _run_for_date(as_of, closes_fn=closes_fn, quotes_fn=quotes_fn, beta_fn=beta_fn, execute=False)
        # v0.45.448：整轮读-改-写持 hedge_state 目录锁——手动补跑与定时扫描撞上时，后到者等前者写完再读持仓 / 现金，
        # 不会拿旧快照做决定、再把对方的成交与净值覆盖掉（锁内的追加 / 重写经 ledger_io 可重入）
        with ledger_io.locked(STATE_DIR):
            return _run_for_date(as_of, closes_fn=closes_fn, quotes_fn=quotes_fn, beta_fn=beta_fn, execute=True)
    except Exception as exc:  # noqa: BLE001 - 记下来、原样抛：调用方的处理不变
        prev = _LAST_PRICE_CHECK if (_LAST_PRICE_CHECK or {}).get("as_of") == as_of else None
        _LAST_PRICE_CHECK = dict(prev or {"as_of": as_of, "version": _VERSION},
                                 error=f"{type(exc).__name__}: {exc}"[:300])
        raise


def _run_for_date(as_of: str, closes_fn=None, quotes_fn=None, beta_fn=None, execute: bool = True) -> Dict:
    """算暴露 → 聚合 → 建议 →（execute 且带外且今天还没交易过）成交 → 覆盖账本盯市 →
    净值快照（按日期去重）→ 审计文件 hedge_state/greeks_{as_of}.json。同日重跑幂等。
    execute=False：只算，不写任何账本文件（β 缓存除外）。
    v0.45.423：SPY 只在拿到 as_of 的**官方收盘**时成交（`fill: close` 名副其实）；盘中实时价只算不成交，
    原因进 `execution_blocked`（审计文件 / price_check → status.json → alert_manager）。"""
    closes_fn = _memo_marks(closes_fn or _default_mark)   # 本次运行内 SPY 只有一个价：建议 / 成交 / 盯市 / 交易后
    res = compute_day(as_of, closes_fn=closes_fn, quotes_fn=quotes_fn, beta_fn=beta_fn)
    rec = res["recommendation"]
    nav = res["nav"]["nav"]
    spy_mark = res["spy_mark"]
    executed = res.get("today_trade")
    res["execution_blocked"] = None
    if not execute:
        res["executed_trade"] = executed
        res["executed"] = False
        res["aggregate_after"] = None
        return res
    delta = res.get("delta_hedge")
    if delta and delta.get("mode") == "paper":
        # 逐票 Delta 对冲先于 SPY：SPY 带的决策（compute_day）已按「逐票对冲成交后」的暴露算
        trades, blocked = _execute_delta_trades(as_of, delta)
        # executed = 今天账本里的全部逐票成交（不只是这一轮新成交的）：第一次跑与同日重跑写出同一份审计（幂等）
        delta["executed"] = [t for t in _load_jsonl(_delta_paths()["trades"]) if t.get("date") == as_of]
        delta["blocked"] = blocked
        for t in trades:                    # 与重跑（从账本重建 traded_today）一致 ⇒ 审计文件幂等
            for d in delta["names"]:
                if d["ticker"] == t["ticker"]:
                    d["traded_today"] = (d.get("traded_today") or 0.0) + t["shares"]
        res["price_check"]["delta_hedge"] = _delta_price_check(delta)
        for tk, why in blocked.items():
            _log.warning("[PortfolioGreeks] %s 逐票对冲 %s 未执行：%s", as_of, tk, why)
        res["delta_equity_snapshot"] = _delta_snapshot(as_of, closes_fn)
    if executed is None and rec.get("action") in ("sell_spy", "buy_spy"):
        if spy_mark.get("price") is not None and spy_mark.get("at_close"):
            executed = _execute_trade(as_of, rec, res["aggregate"], nav, spy_mark=spy_mark)
        else:
            blocked = (f"SPY 价不是 {_expected_session(as_of)} 的官方收盘（{spy_mark.get('source')} @ "
                       f"{spy_mark.get('session')}{'，盘中实时价' if spy_mark.get('live') else ''}）"
                       "——成交按收盘记账，拒绝成交")
            res["execution_blocked"] = res["price_check"]["execution_blocked"] = blocked
            _log.warning("[PortfolioGreeks] %s %s %+d SPY 未执行：%s", as_of, rec.get("action"),
                         int(rec.get("spy_shares") or 0), blocked)
    elif executed is not None and rec.get("action") in ("sell_spy", "buy_spy"):
        _log.info("[PortfolioGreeks] %s 今天已成交过（%+d SPY），重跑不再交易", as_of, int(executed.get("shares") or 0))
    res["executed_trade"] = executed
    res["executed"] = executed is not None
    res["today_trade"] = executed      # 第一次跑与重跑写出同一份审计文件（幂等）

    # ── 覆盖账本盯市 + 净值快照 ──
    meta = _load_meta()
    spy_price = res["spy_price"]
    positions = _hedge_positions()
    shares = 0.0
    for p in positions:
        s = _num(p.get("shares")) or 0.0
        shares += s
        if spy_price is not None:
            p["last_price"] = spy_price
            p["last_mark_date"] = as_of
    if positions:
        _write_jsonl(POSITIONS_FILE, positions)
    cash = _num(meta.get("cash"))
    mv = (shares * spy_price) if (spy_price is not None) else (None if shares else 0.0)
    overlay_nav = (cash + mv) if (cash is not None and mv is not None) else None
    snapshot = {"date": as_of, "spy_shares": shares, "spy_price": spy_price,
                "spy_price_source": spy_mark.get("source"), "spy_price_session": spy_mark.get("session"),
                "spy_price_at_close": bool(spy_mark.get("at_close")),        # v0.45.440：盘中实时价不是收盘盯市
                "cash": _r(cash),
                "market_value": _r(mv), "nav": _r(overlay_nav),
                "trades_today": sum(1 for t in _load_jsonl(TRADES_FILE) if t.get("date") == as_of),
                "band_status": res["aggregate"]["band_status"]}
    equity = [e for e in _load_jsonl(EQUITY_FILE) if e.get("date") != as_of]
    equity.append(snapshot)
    equity.sort(key=lambda e: e["date"])
    _write_jsonl(EQUITY_FILE, equity)
    if not meta.get("starting_date"):
        meta["starting_date"] = as_of
    meta["last_run_date"] = as_of
    meta["version"] = _VERSION
    meta["config_snapshot"] = dict(CONFIG)
    meta.setdefault("cash", 0.0)
    _save_meta(meta)

    # ── 交易后的聚合（含 SPY 覆盖现状）──
    hedge_rows_after = hedge_exposures(as_of, closes_fn, positions=_hedge_positions(),
                                       spy_price=spy_price, spy_mark=spy_mark)
    rows_after = [r for r in res["rows"] if r.get("kind") != "hedge"] + hedge_rows_after
    res["aggregate_after"] = aggregate(rows_after, nav)
    res["equity_snapshot"] = snapshot

    audit = {k: v for k, v in res.items() if k != "rows"}
    audit["rows"] = res["rows"]
    ledger_io.write_json(STATE_DIR / f"greeks_{as_of}.json", _scrub(audit), indent=1, sort_keys=True)
    return res


# ══════════════════════════════════════════════════════════════════════════════
# 历史：唯一的读取口（v0.45.440）
# ══════════════════════════════════════════════════════════════════════════════
# 账本历史里混着两种记录：v0.45.423 之前的（标的价 / SPY 成交价晚一个交易日，且**自己不说**）与之后的
#（每条自带「价属于哪一场、是不是收盘」）。按日期切要靠读者记得一个约定；这里按记录**自带的证明**放行，
# 证不出的不放行、计数并说清原因。其他模块不许绕过它直读历史（守卫 tests/test_hedge_history_gate.py）。

#: v0.45.423~439 的净值行没有 `spy_price_at_close` 字段：这两个来源只在收盘后产生（cboe_close = last_trade
#: 贴收盘的官方收盘；twelve_data_bar = 日期恰为该场、交易所钟上已收盘的那根），据此补判。
_CLOSE_SOURCES = ("cboe_close", "twelve_data_bar")
#: 盘中跑（交易所还没收盘）时 CBOE 给的实时价来源。这样的记录不是「该场的收盘」，不论持不持 SPY（v0.45.442）。
_LIVE_SOURCES = ("cboe_intraday",)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _read_history_lines(path: Path) -> List[Tuple[Optional[Dict], str]]:
    """逐行读成 (记录 | None, 预判)：坏 JSON ⇒ unreadable、不是对象 ⇒ not_an_object、没有合法日期 ⇒ no_date。
    v0.45.442：此前借 `_load_jsonl`——它把坏行**静默跳过**（闸要的是「排除并计数」），且照收非对象行，
    `load_history` 对一行 `"str"` / `[1, 2]` 直接 AttributeError。"""
    out: List[Tuple[Optional[Dict], str]] = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                out.append((None, "unreadable"))
                continue
            if not isinstance(rec, dict):
                out.append((None, "not_an_object"))
            elif not _DATE_RE.match(str(rec.get("date") or "")):
                out.append((rec, "no_date"))
            else:
                out.append((rec, ""))
    return out


def _verdict_trade(t: Dict) -> str:
    if "price_session" not in t:
        return "no_provenance"
    if t.get("price_session") != _expected_session(str(t.get("date") or "")) or not t.get("price_at_close"):
        return "not_its_session_close"
    return "ok"


def _verdict_equity(e: Dict) -> str:
    if "spy_price_session" not in e:
        return "no_provenance"
    if _num(e.get("nav")) is None:
        return "unpriced"
    if e.get("spy_price_source") in _LIVE_SOURCES:
        return "intraday_run"
    if not (_num(e.get("spy_shares")) or 0.0):
        return "ok"                                   # 没持 SPY：净值 = 现金，与 SPY 价无关
    at_close = (e["spy_price_at_close"] if "spy_price_at_close" in e
                else e.get("spy_price_source") in _CLOSE_SOURCES)
    if e.get("spy_price_session") != _expected_session(str(e.get("date") or "")) or not at_close:
        return "not_its_session_close"
    return "ok"


def _verdict_audit(a: Dict, day: str) -> str:
    chk = a.get("price_check") if isinstance(a, dict) else None
    if not isinstance(chk, dict):
        return "no_provenance"
    if not (chk.get("as_of") == day == a.get("as_of")):
        return "date_mismatch"
    # 盘中跑的审计：SPY 是实时价，或任何一行的价来自盘中实时价 ⇒ 不是「该场收盘」的快照（v0.45.442）
    if (chk.get("spy") or {}).get("live") or any(
            isinstance(r, dict) and r.get("price_source") in _LIVE_SOURCES for r in a.get("rows") or []):
        return "intraday_run"
    return "ok"


def _verdict_delta_equity(e: Dict) -> str:
    """逐票对冲账净值行的放行判据（v0.45.526）：自带 `marks_complete`（每只持仓都有 as_of 的价）与
    `marks_at_close`（价都是官方收盘）；没有持仓时净值 = 现金，与价无关。"""
    if "marks_complete" not in e:
        return "no_provenance"
    if _num(e.get("nav")) is None:
        return "unpriced"
    if (_num(e.get("n_positions")) or 0) and not e.get("marks_at_close"):
        return "not_its_session_close"
    return "ok"


def _load_delta_history(d: Optional[Path]) -> Dict:
    """逐票对冲账（delta_trades / delta_equity_curve）的历史，同样只放行能自证定价场次的记录。
    成交的判据与 SPY 成交同一个 `_verdict_trade`（`price_session` 对且 `price_at_close`）。"""
    paths = _delta_paths()
    trades_f = d / "delta_trades.jsonl" if d is not None else paths["trades"]
    equity_f = d / "delta_equity_curve.jsonl" if d is not None else paths["equity"]
    kept: Dict[str, List] = {"trades": [], "equity": []}
    excluded: Dict[str, Dict[str, int]] = {"trades": {}, "equity": {}}
    excluded_dates: Dict[str, List[str]] = {"trades": [], "equity": []}
    for kind, path, judge in (("trades", trades_f, _verdict_trade), ("equity", equity_f, _verdict_delta_equity)):
        for rec, pre in _read_history_lines(path):
            day = str(rec.get("date") or "") if isinstance(rec, dict) else ""
            v = pre or judge(rec)
            if v == "ok":
                kept[kind].append(rec)
            else:
                excluded[kind][v] = excluded[kind].get(v, 0) + 1
                excluded_dates[kind].append(day or "?")
    for k in kept:
        kept[k].sort(key=lambda r: str(r.get("date") or ""))
    return {"trades": kept["trades"], "equity": kept["equity"], "excluded": excluded,
            "excluded_dates": excluded_dates}


def load_history(state_dir: Optional[Path] = None) -> Dict:
    """对冲账本历史的**唯一**读取口：只放行能自证「价属于自己那一场的收盘」的记录。

    返回 {"trades", "equity": 放行的记录（按日期升序）, "audits": {日期: 审计文件},
          "excluded": {种类: {原因: 条数}}, "excluded_dates": {种类: [日期]},
          "first_verified": {种类: 最早放行日期 | None}, "measurement_start": MEASUREMENT_START,
          "regressions": [(种类, 日期)], "before_measurement_start": [(种类, 日期)]}。
    原因：no_provenance（v0.45.423 之前的格式，证不出）/ not_its_session_close（证明说价不是该场收盘）/
    unpriced（净值行没有净值）/ date_mismatch / unreadable（坏 JSON）/ not_an_object / no_date（v0.45.442 起计数，此前坏行
    被静默跳过、非对象行直接抛错）/ intraday_run（盘中跑的记录：价是实时价，不是收盘；v0.45.442）。
    `regressions` = 某种记录已出现过放行的之后又出现无证明的——写入方退化了，打 ERROR。
    `before_measurement_start` = 早于 `MEASUREMENT_START` 却带了证明——有人往旧记录上补了字段，打 WARNING。"""
    d = Path(state_dir) if state_dir is not None else None
    trades_f = d / "trades.jsonl" if d is not None else TRADES_FILE
    equity_f = d / "equity_curve.jsonl" if d is not None else EQUITY_FILE
    audit_dir = d if d is not None else STATE_DIR
    kept: Dict[str, List] = {"trades": [], "equity": []}
    audits: Dict[str, Dict] = {}
    excluded: Dict[str, Dict[str, int]] = {"trades": {}, "equity": {}, "audits": {}}
    excluded_dates: Dict[str, List[str]] = {"trades": [], "equity": [], "audits": []}
    verdicts: Dict[str, List[Tuple[str, str]]] = {"trades": [], "equity": [], "audits": []}

    for kind, path, judge in (("trades", trades_f, _verdict_trade), ("equity", equity_f, _verdict_equity)):
        for rec, pre in _read_history_lines(path):
            day = str(rec.get("date") or "") if isinstance(rec, dict) else ""
            v = pre or judge(rec)
            if day and _DATE_RE.match(day):
                verdicts[kind].append((day, v))          # 没日期的行不参与起点 / 退化的日期推断
            if v == "ok":
                kept[kind].append(rec)
            else:
                excluded[kind][v] = excluded[kind].get(v, 0) + 1
                excluded_dates[kind].append(day or "?")
    for f in sorted(audit_dir.glob("greeks_*.json")) if audit_dir.is_dir() else []:
        day = f.stem[len("greeks_"):]
        if not _DATE_RE.match(day):
            continue                                    # 不是 greeks_<日期>.json（别的同前缀文件）
        try:
            a = json.loads(f.read_text(encoding="utf-8"))
            v = _verdict_audit(a, day) if isinstance(a, dict) else "not_an_object"
        except (OSError, ValueError):
            a, v = None, "unreadable"
        verdicts["audits"].append((day, v))
        if v == "ok":
            audits[day] = a
        else:
            excluded["audits"][v] = excluded["audits"].get(v, 0) + 1
            excluded_dates["audits"].append(day)

    first = {k: min((day for day, v in vs if v == "ok"), default=None) for k, vs in verdicts.items()}
    regressions = sorted((k, day) for k, vs in verdicts.items() for day, v in vs
                         if v == "no_provenance" and first[k] is not None and day >= first[k])
    early = sorted((k, day) for k, vs in verdicts.items() for day, v in vs
                   if v == "ok" and day < MEASUREMENT_START)
    if regressions:
        _log.error("[PortfolioGreeks] 对冲账本出现**无定价证明的新记录**（写入方退化）：%s", regressions)
    if early:
        _log.warning("[PortfolioGreeks] 早于 MEASUREMENT_START %s 的记录带了定价证明（旧记录被补过字段？）：%s",
                     MEASUREMENT_START, early)
    for k in ("trades", "equity"):
        kept[k].sort(key=lambda r: str(r.get("date") or ""))
    return {"trades": kept["trades"], "equity": kept["equity"], "audits": audits,
            "excluded": excluded, "excluded_dates": excluded_dates, "first_verified": first,
            "measurement_start": MEASUREMENT_START, "regressions": regressions,
            "before_measurement_start": early, "delta": _load_delta_history(d)}


# ══════════════════════════════════════════════════════════════════════════════
# 报告
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_usd(v) -> str:
    f = _num(v)
    return f"${f:+,.0f}" if f is not None else "—"


def _fmt_pct(v, nd=2) -> str:
    f = _num(v)
    return f"{f:+.{nd}f}%" if f is not None else "—"


#: 对外展示的规则常量（Alpha Bot 对冲页 / 帮助页取这一份，不在前端另抄）
PUBLIC_RULE_KEYS = ("beta_delta_band_pct", "beta_delta_target_pct", "rebalance_to", "hedge_instrument",
                    "vega_alert_pct", "gamma_alert_pct", "beta_window",
                    "delta_hedge_mode", "delta_hedge_band_pct", "delta_hedge_rebalance_to")


def ledger_view(root=None) -> Dict:
    """对冲账本的只读视图（v0.45.459，Alpha Bot「对冲」页）。账本之外的读者一律经这里，不碰文件名。

    `root` = 数据根；None ⇒ **调用时**的 `PATHS.home`（不用 import 期冻住的 `STATE_DIR`：Alpha Bot 是常驻进程，
    数据根可经 `--home` 指定）。
      · 历史（成交 / 净值 / 每日审计）只经 `load_history()`——放行能自证定价场次的记录，被拦下的给计数与日期；
      · 当前持仓与现金是「现在」不是历史：`ledger_io` 严格读，读不了写进 `errors`、**不抛**（页面要能如实显示
        「读不了」，而不是整页出错，更不能把读不了当成「空仓」）。
    不写任何文件。"""
    d = Path(root if root is not None else PATHS.home) / "hedge_state"
    out: Dict = {"state_dir": str(d), "exists": d.is_dir(), "opened": False, "positions": None, "meta": None, "errors": {},
                 "history": None, "rules": {k: CONFIG.get(k) for k in PUBLIC_RULE_KEYS},
                 "measurement_start": MEASUREMENT_START,
                 "delta_opened": False, "delta_positions": None, "delta_meta": None}   # v0.45.526：状态目录不存在时键也在
    if not out["exists"]:
        return out
    # 「开过账」= meta.json 在（第一次执行就写它）。开过账却没有持仓文件 ⇒ 被删了 / 数据根不对，是错不是空仓——
    # 空仓时持仓文件照样在（写成空文件）。没开过账 ⇒ 持仓为 None、不报错，页面说「还没有记录」（v0.45.459 二次检查）。
    out["opened"] = (d / "meta.json").exists()
    pos_f = d / "positions.jsonl"
    if not pos_f.exists():
        if out["opened"]:
            out["errors"]["positions"] = f"{pos_f} 不存在（meta.json 在：持仓文件被删了，或数据根不对）"
    else:
        try:
            out["positions"] = ledger_io.load_jsonl(pos_f)
        except (ledger_io.LedgerError, OSError) as exc:
            out["errors"]["positions"] = f"{type(exc).__name__}: {exc}"
    try:
        out["meta"] = ledger_io.load_json(d / "meta.json", default=None)
    except (ledger_io.LedgerError, OSError) as exc:
        out["errors"]["meta"] = f"{type(exc).__name__}: {exc}"
    # 逐票对冲账（v0.45.526）：开过账（delta_meta.json 在）才有；没开过 ⇒ None、不报错（shadow 缺省就是这样）
    out["delta_opened"] = (d / "delta_meta.json").exists()
    if out["delta_opened"]:
        for key, fname, loader in (("delta_positions", "delta_positions.jsonl", ledger_io.load_jsonl),
                                   ("delta_meta", "delta_meta.json",
                                    lambda f: ledger_io.load_json(f, default=None))):
            try:
                out[key] = loader(d / fname) if (d / fname).exists() else None
                if out[key] is None and key == "delta_positions":
                    out["errors"][key] = f"{d / fname} 不存在（delta_meta.json 在：持仓文件被删了）"
            except (ledger_io.LedgerError, OSError) as exc:
                out["errors"][key] = f"{type(exc).__name__}: {exc}"
    try:
        out["history"] = load_history(d)
    except Exception as exc:  # noqa: BLE001 - 历史读不出来如实说，不当成「没有历史」
        out["errors"]["history"] = f"{type(exc).__name__}: {exc}"
    return out


def _load_audit(as_of: str) -> Optional[Dict]:
    p = STATE_DIR / f"greeks_{as_of}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _spy_price_note(res: Dict) -> str:
    px = _num(res.get("spy_price"))
    m = res.get("spy_mark") or {}
    if px is not None:
        return f"；SPY ${px:.2f}（{m.get('source') or '?'} @ {m.get('session') or '?'}）" if m else f"；SPY ${px:.2f}"
    if m.get("stale"):
        return f"；SPY 价不可得（看到的 {m.get('seen_price')} 属于 {m.get('session')}，不是这一场）"
    return "；SPY 价不可得"


def _stale_price_line(res: Dict) -> str:
    """v0.45.423：陈旧价点名——读者带走的是「哪只票、看到的是哪天的价」，不是一个计数。"""
    chk = res.get("price_check") or {}
    bits = list(dict.fromkeys(f"{s.get('ticker')} {s.get('seen_price')} @ {s.get('session')}"
                              for s in chk.get("stale") or []))
    if chk.get("n_quote_stale"):
        bits.append(f"{chk['n_quote_stale']} 张合约的报价属于别的场次")
    blocked = chk.get("execution_blocked") or res.get("execution_blocked")
    if not bits and not blocked:
        return ""
    out = ""
    if bits:
        out = (f"⚠️ **价不属于 {chk.get('expected_session') or res.get('as_of')} 这一场**："
               + "；".join(bits[:8]) + "——这些行不进 $Delta / 净值 / 成交")
    if blocked:
        out += ("　" if out else "⚠️ ") + f"**未成交**：{blocked}"
    return out


def _excluded_line(ex: Optional[Dict]) -> str:
    """日报里的一行：谁被挡在对冲之外、估计还剩多少敞口。没配置 / 今天没持仓 ⇒ 空串。"""
    names = (ex or {}).get("names") or []
    if not names:
        return ""
    bits = []
    for e in names:
        usd = e.get("stock_usd_est")
        bits.append(f"{e['ticker']}（股票 {e.get('n_stock_positions', 0)} 笔"
                    + (f"，按建仓价估 {_fmt_usd(usd)} / {_fmt_pct(e.get('stock_pct_nav_est'))} NAV" if usd is not None else "")
                    + (f"；跨式 {e['n_straddles']} 笔" if e.get("n_straddles") else "") + "）")
    return "**不进对冲计算**（`hedge_exclude_tickers`，敞口仍在、SPY 覆盖不管）：" + "；".join(bits)


def _delta_markdown(sec: Optional[Dict]) -> List[str]:
    """日报里的「逐票 Delta 对冲」小节（v0.45.526）。mode=off / 没有这一节（旧审计文件）⇒ 不写。"""
    if not sec or not sec.get("names"):
        return []
    mode = sec.get("mode")
    head = {"shadow": "只算不成交（shadow）", "paper": "纸面成交"}.get(mode, str(mode))
    L = ["", f"**逐票 Delta 对冲**（{head}；带 NAV 的 ±{sec.get('band_pct'):.1f}%，"
         f"{'拉回 0' if sec.get('rebalance_to') == 'center' else '拉回带边'}）：",
         "", "| 标的 | 跨式腿 $Delta | % NAV | 目标对冲股数 | 今日成交 | 状态 |", "|---|---|---|---|---|---|"]
    nav = _num(sec.get("nav"))
    for d in sec["names"]:
        dd = _num(d.get("option_dd"))
        pct = f"{dd / nav * 100:+.1f}%" if (dd is not None and nav) else "—"
        ts = d.get("trade_shares") or 0
        state = d.get("status")
        if state == "unknown":
            state = f"未决：{d.get('reason')}"
        L.append(f"| {d['ticker']} | {_fmt_usd(dd)} | {pct} | "
                 f"{('%+d' % d['target_shares']) if d.get('target_shares') is not None else '—'} | "
                 f"{('%+d' % ts) if ts else '—'} | {state} |")
    if sec.get("n_undecided"):
        L.append("")
        L.append(f"⚠️ {sec['n_undecided']} 只决定做不出来（只影响这几只，别的照常）："
                 + "；".join(f"{u['ticker']}（{u['detail']}）" for u in sec.get("undecided") or []))
    if sec.get("blocked"):
        L.append("未执行：" + "；".join(sec["blocked"].values()))
    L.append("> 没有点差 / 冲击成本模型（单只股票比 SPY 贵）；只对冲 Delta，Gamma / Vega 不对冲；"
             "带与拉回方式是 v1 主观常数，不是校准结果，也没有证据说明逐票对冲后更好。")
    return L


def render_markdown(as_of: str, result: Optional[Dict] = None) -> str:
    """日报小节。优先读当日审计文件（run_for_date 已跑过）；否则只读计算、不落盘。
    三本账都没有任何持仓 → 空串。"""
    res = result or _load_audit(as_of)
    if res is None:
        try:
            res = run_for_date(as_of, execute=False)
        except Exception as exc:  # noqa: BLE001
            _log.warning("[PortfolioGreeks] 只读计算失败: %s", exc)
            return ""
    rows = res.get("rows") or []
    if not rows:
        return ""
    agg = res.get("aggregate") or {}
    after = res.get("aggregate_after") or agg
    rec = res.get("recommendation") or {}
    nav_d = res.get("nav") or {}
    nav = _num(nav_d.get("nav"))
    kinds = agg.get("by_kind") or {}
    trade = res.get("executed_trade")
    stress = res.get("stress") or {}

    L = ["", "## 组合 Greeks 与对冲（观察项 + 纸面 SPY 覆盖）", ""]
    hedge_sh = sum(_num(r.get("qty")) or 0.0 for r in rows if r.get("kind") == "hedge")
    L.append(f"截至 {as_of}：股票仓 {kinds.get('stock', 0)} 行 / 跨式腿合约 {kinds.get('option', 0)} 行 / "
             f"SPY 覆盖 {hedge_sh:+,.0f} 股（交易前）；合并 NAV "
             + (f"${nav:,.0f}" if nav is not None else f"不可得（缺：{', '.join(nav_d.get('missing') or ['?'])}）")
             + _spy_price_note(res)
             + "。")
    L += ["", "| 指标 | 数值 | % NAV | 备注 |", "|------|------|-------|------|"]
    s, p = agg.get("sums") or {}, agg.get("pct_nav") or {}
    band = agg.get("band") or {}
    L.append(f"| $Delta | {_fmt_usd(s.get('dollar_delta'))} | {_fmt_pct(p.get('dollar_delta'))} | 名义方向暴露 |")
    L.append(f"| β·$Delta | {_fmt_usd(s.get('beta_dollar_delta'))} | {_fmt_pct(p.get('beta_dollar_delta'))} | "
             f"带 [{band.get('lower_pct', 0):+.0f}%, {band.get('upper_pct', 0):+.0f}%] → **{agg.get('band_status')}** |")
    L.append(f"| $Gamma (1% 移动) | {_fmt_usd(s.get('gamma_dollar_per_1pct'))} | {_fmt_pct(p.get('gamma_dollar_per_1pct'), 3)} | "
             f"{'⚠️ 超 ' + str(CONFIG['gamma_alert_pct']) + '%' if agg.get('gamma_alert') else '凸性项'} |")
    L.append(f"| $Vega / vol 点 | {_fmt_usd(s.get('vega_dollar_per_pt'))} | {_fmt_pct(p.get('vega_dollar_per_pt'), 3)} | "
             f"{'⚠️ 超 ' + str(CONFIG['vega_alert_pct']) + '%' if agg.get('vega_alert') else 'IV 每变 1 点'} |")
    L.append(f"| $Theta / 日 | {_fmt_usd(s.get('theta_dollar_per_day'))} | {_fmt_pct(p.get('theta_dollar_per_day'), 3)} | 日历日 |")
    cov = agg.get("coverage") or {}
    cov_line = (f"覆盖：{cov.get('n_rows', 0)} 行，缺价 {cov.get('n_price_missing', 0)}"
                + (f"（其中陈旧 {cov['n_price_stale']}）" if cov.get("n_price_stale") else "")
                + f" / 缺报价 {cov.get('n_quote_missing', 0)} / "
                f"缺 β {cov.get('n_beta_missing', 0)}，β 覆盖率 "
                + (f"{(cov.get('beta_coverage') or 0) * 100:.0f}%" if cov.get("beta_coverage") is not None else "—"))
    if agg.get("partial"):
        cov_line += " —— **部分数据，结论不完整，不据此对冲**"
    L += ["", cov_line]
    stale_line = _stale_price_line(res)
    if stale_line:
        L.append(stale_line)
    excl_line = _excluded_line(res.get("excluded_from_hedge"))
    if excl_line:
        L.append(excl_line)
    L.append(f"**今日建议**：`{rec.get('action')}` {int(rec.get('spy_shares') or 0):+d} SPY —— {rec.get('reason')}")
    if trade:
        L.append(f"**已执行**（纸面）：{int(trade.get('shares') or 0):+d} SPY @ ${_num(trade.get('price')) or 0:.2f}，"
                 f"成交=收盘、无点差模型；交易后 β·$Delta {_fmt_pct((after.get('pct_nav') or {}).get('beta_dollar_delta'))} NAV"
                 f"（{after.get('band_status')}）")
    elif res.get("executed") is False and rec.get("action") in ("sell_spy", "buy_spy"):
        L.append("**未执行**（dry-run / 只读渲染）")
    L += _delta_markdown(res.get("delta_hedge"))
    if stress.get("cells"):
        spots, ivs = stress["spot_pct"], stress["iv_pts"]
        L += ["", "**压力网格**（行：β 调整后现货冲击；列：IV 平移 vol 点；单位 $）", "",
              "| spot \\ IV | " + " | ".join(f"{v:+d}pt" for v in ivs) + " |",
              "|---|" + "---|" * len(ivs)]
        by = {(c["spot_pct"], c["iv_pts"]): c for c in stress["cells"]}
        for sp in spots:
            vals = []
            for iv in ivs:
                c = by.get((sp, iv)) or {}
                # `*` = 这一格的 IV 冲击被 0 地板截断了，实际没打满标签上的点数
                vals.append(f"{_num(c.get('pnl')) or 0:+,.0f}" + ("*" if c.get("iv_clamped") else ""))
            L.append(f"| {sp:+d}% | " + " | ".join(vals) + " |")
        w = stress.get("worst_cell") or {}
        L.append("")
        # 「网格不完整，已排除 $X 敞口」必须**紧贴**最差格那个金额：读者带走的是那个数字，
        # 不是下一行的免责声明（二次复查：$100 万无 β 的行被剔掉后，最差格温和了 100 倍）。
        worst_line = (f"最差格：现货 {w.get('spot_pct', 0):+d}% / IV {w.get('iv_pts', 0):+d}pt → {_fmt_usd(w.get('pnl'))}"
                      + (f"（{_num(w.get('pnl')) / nav * 100:+.2f}% NAV）" if (nav and _num(w.get('pnl')) is not None) else ""))
        if stress.get("partial"):
            exd = _num(w.get("excluded_dollar_delta"))
            unk = int(w.get("excluded_dd_unknown") or 0)
            worst_line += ("　⚠️ **网格不完整，已排除 " + (f"${exd:,.0f}" if exd is not None else "未知规模")
                           + (f" + {unk} 行规模未知" if unk else "") + " 敞口，真实最差比这个数字更差**"
                           + f"；剔除 {len(stress.get('excluded') or [])} 行（{', '.join(stress['excluded'][:4])}）")
        L.append(worst_line)
        if stress.get("n_iv_clamped_cells"):
            cl = [c for c in stress["cells"] if c.get("iv_clamped")]
            L.append(f"⚠️ IV 轴截断（表中标 `*`）：{stress['n_iv_clamped_cells']} 格的标称冲击打不满"
                     f"（合约 IV 不够减，IV 不能为负）——"
                     + "；".join(f"{c['spot_pct']:+d}%/{c['iv_pts']:+d}pt 实际 {_num(c.get('iv_pts_effective')) or 0:+.1f}pt"
                                 for c in cl[:4])
                     + "。这些格施加的冲击比标签小，别按标签读。")
        if stress.get("bs_vs_mid_gap"):
            g = stress["bs_vs_mid_gap"]
            L.append(f"模型基差（BS − mid，每股）：" + "；".join(f"{x.get('symbol')} {x.get('gap'):+.2f}" for x in g[:6])
                     + f"；(0,0) 格合计 {_fmt_usd(stress.get('pnl_at_zero'))}（不是 P&L，是模型与市场的差）")
    L += ["",
          "> CBOE Greeks 单位（NVDA 实盘对 BS 校核）：vega 每 vol 点、theta 每日历日、delta/gamma 每股。",
          f"> 对冲规则：只在 β·$Delta 出 ±{CONFIG['beta_delta_band_pct']:.0f}% NAV 带时用 SPY 股票拉回{'带边' if CONFIG['rebalance_to'] == 'edge' else '带中心'}；"
          "vega/gamma 只告警不对冲；缺 β/缺价一律不对冲。",
          "> **观察项：不进评分、不进股票纸面组合、不构成任何建议。** SPY 覆盖是独立纸面账本，起始现金 0。", ""]
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main(argv: Optional[List[str]] = None) -> int:
    from hive_logger import pdt_today
    ap = argparse.ArgumentParser(description="组合 Greeks 聚合 + β·Delta 带状对冲（纸面 SPY 覆盖）")
    ap.add_argument("--date", default=None, help="业务日 YYYY-MM-DD（默认 PDT 今天）")
    ap.add_argument("--dry-run", action="store_true", help="只算不写账本（β 缓存除外）")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON（不含逐行）")
    args = ap.parse_args(argv)
    as_of = args.date or pdt_today()
    res = run_for_date(as_of, execute=not args.dry_run)
    if args.json:
        out = {k: v for k, v in res.items() if k != "rows"}
        out["n_rows"] = len(res.get("rows") or [])
        print(json.dumps(_scrub(out), ensure_ascii=False, indent=2))
    else:
        print(render_markdown(as_of, result=res) or f"{as_of}: 三本账均无持仓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
