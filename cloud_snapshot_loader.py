#!/usr/bin/env python3
"""
🐝 Alpha Hive — 云端快照消费端 (v0.45.38)
==========================================
把 `cloud-snapshots` 分支上的当日快照喂回本机扫描，用于**补跑主机关机那天**。
生产端是 `cloud_snapshot_fetch.py`；本模块是它唯一的读者。

为什么需要适配层，而不是 `json.load` 直接喂
--------------------------------------------
JSON 只有字符串键。`full_chain_oi` 的 `call_oi` / `put_oi` / `call_exp_oi` /
`put_exp_oi` **以行权价为键**，往返后 `130.0` → `'130.0'`。这不会崩 ——
`options_analyzer` 里的

    all_strikes = sorted(set(call_oi.keys()) | set(put_oi.keys()))

照样排得出来，只是排成**字典序**（`'100.0' < '130.0' < '90.0'`），
Max Pain 穷举与 GEX 于是算在错序的行权价上：**数字照出、全是错的**。
这正是本项目最常见的故障形态（见 MEMORY 静默降级三件套），
所以恢复数字键是本模块的第一职责，且有守卫盯着。

为什么快照模式下**不回落**实时抓取
----------------------------------
补跑的是**过去某天**。实时抓取会拿到**今天**的期权链、贴上那天的日期 ——
与 v0.45.36 拦下的污染同源，只是方向相反。所以快照缺某个标的时返回
`None`（诚实缺失），绝不静默回落。

vintage 双重把关
----------------
生产端自 v0.45.36 起保证落盘数据的 `vintage_date` == 目录日期。消费端**再验一次**：
拿 `2026-08-20/NVDA.json` 当 8/20 用之前，确认它自己也说是 8/20 的。
缺 `vintage_date` 的（v0.45.36 之前产出）默认拒绝 —— 现存这类快照只有一份，
而它恰好就是被污染的那份。

用法
----
    import cloud_snapshot_loader as csl
    print(csl.available_dates())
    with csl.snapshot_mode("2026-08-20"):
        ...                      # 此间 cboe_options 的四个取数入口全走快照
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

# ⚠️ 下面这些常量必须**晚绑定**使用：函数签名一律 `ref: Optional[str] = None`，
# 再在体内 `ref = ref or SNAPSHOT_REF`。写成 `ref: str = SNAPSHOT_REF` 会在
# import 时把默认值绑死，monkeypatch 模块属性打不中 —— v0.45.37 刚因这个
# pattern 在 replay_scoring 里造出过假守卫，本模块初版又犯了一次。
SNAPSHOT_REF = "origin/cloud-snapshots"
SNAPSHOT_SUBDIR = "cloud_snapshots"
REPO_DIR = os.path.dirname(os.path.abspath(__file__))

# 以行权价为键、必须还原成 float 的段（见模块 docstring）
_FLOAT_KEY_SECTIONS = ("call_oi", "put_oi", "call_exp_oi", "put_exp_oi")

_log = logging.getLogger("alpha_hive.cloud_snapshot_loader")


class SnapshotUnavailable(RuntimeError):
    """快照不存在 / 不可信 —— 调用方应当诚实降级，不要回落实时抓取。"""


def _git_show(path: str, ref: Optional[str] = None, repo: Optional[str] = None) -> Optional[str]:
    """`git show ref:path`；对象不存在返回 None（不抛）。

    刻意走 git 而不是 checkout：分支上只有数据、与工作区代码无关，
    没必要为读一天的快照切分支或多开 worktree。
    """
    ref = ref or SNAPSHOT_REF
    try:
        r = subprocess.run(["git", "show", f"{ref}:{path}"],
                           cwd=repo or REPO_DIR, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def available_dates(ref: Optional[str] = None, repo: Optional[str] = None) -> List[str]:
    """分支上已有快照的日期列表（升序）。分支不存在返回空表。"""
    ref = ref or SNAPSHOT_REF
    try:
        r = subprocess.run(["git", "ls-tree", "--name-only", f"{ref}:{SNAPSHOT_SUBDIR}"],
                           cwd=repo or REPO_DIR, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return sorted(x.strip("/") for x in r.stdout.split() if x.strip("/"))


def _restore_numeric_keys(oi: Optional[dict]) -> Optional[dict]:
    """把 full_chain_oi 里以行权价为键的段从 str 还原成 float。

    只动这四段的**顶层键**；`call_exp_oi[strike]` 的内层键是到期日字符串
    （'2026-09-19'），本来就该是 str，不能一起转。
    """
    if not isinstance(oi, dict):
        return oi
    out = dict(oi)
    for sec in _FLOAT_KEY_SECTIONS:
        d = out.get(sec)
        if not isinstance(d, dict):
            continue
        conv = {}
        for k, v in d.items():
            try:
                nk = float(k)
            except (TypeError, ValueError):
                conv[k] = v          # 转不动就原样留，别静默丢数据
                continue
            if nk in conv:
                # 理论上不会发生（快照的键统一来自 Python float 经 json.dump，
                # 不会同时出现 "130" 与 "130.0"）。但真发生就是**静默丢一条 OI**，
                # 所以宁可吵一声也不闷着 —— 本项目最贵的故障都是这么来的。
                _log.warning("full_chain_oi.%s 行权价键碰撞：%r 与已有键都归到 %s，"
                             "后者覆盖前者", sec, k, nk)
            conv[nk] = v
        out[sec] = conv
    return out


def load_ticker(date: str, ticker: str, *, ref: Optional[str] = None,
                repo: Optional[str] = None, allow_unverified: bool = False) -> Optional[dict]:
    """载入某日某标的的快照；不存在或 vintage 不符返回 None。

    `allow_unverified=True` 才接受缺 `vintage_date` 的旧快照（v0.45.36 之前产出）。
    """
    txt = _git_show(f"{SNAPSHOT_SUBDIR}/{date}/{ticker.upper()}.json", ref, repo)
    if not txt:
        return None
    try:
        d = json.loads(txt)
    except ValueError:
        return None

    v = d.get("vintage_date")
    if v is None and not allow_unverified:
        return None                      # 旧格式：证不出新鲜度，默认不用
    if v is not None and v != date:
        return None                      # 目录日期与数据自述不符，一律不用

    d["full_chain_oi"] = _restore_numeric_keys(d.get("full_chain_oi"))
    return d


# load_official_close 的判决标签（进 StockData.price_source / 补跑结果的 _reason）
SNAP_CLOSE = "cloud_snapshot_close"
SNAP_NEXT_PREV_CLOSE = "cloud_snapshot_next_prev_day_close"


def _fetched_at(snap: dict):
    """快照 `fetched_at_utc` → aware datetime（close_verdict 自己转 ET）；缺失/解析不了/无时区返回 None。"""
    from datetime import datetime
    raw = snap.get("fetched_at_utc")
    if not isinstance(raw, str):
        return None
    try:
        dt_ = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return dt_ if dt_.tzinfo is not None else None


def load_official_close(date: str, ticker: str, *, ref: Optional[str] = None,
                        repo: Optional[str] = None) -> Tuple[Optional[float], str]:
    """某日某标的的**官方收盘价**，取自云端快照 → `(价 | None, 判决)`（v0.45.243）。

    `price_at_fetch` 不能直接当收盘用：它是 `official_price` 在抓取那一刻的取值，
    CDN 发盘中生成的文件时它是中午的成交价，而 v0.45.234 之前的快照照样标
    `price_source="cboe_close"`。云端 290 份（08-28~09-11）里 71 份如此，
    08-28 的 DE（624.85 vs 官方 630.33，last_trade 14:55:46）、TMO（626.325 vs 622.18，
    09:45:27）、CVX、VZ 就是经补跑兜底这么进的库。**所以按 `last_trade_time_et` 判，
    不看标签**（判据是 `cboe_options.close_verdict`，与数据管道同一份）。

    取价顺序：
      1. 当日快照 close_verdict == official 且场次 == date → `price_at_fetch`（SNAP_CLOSE）
      2. 当日快照不是官方收盘 ⇒ **其后第一份快照的 `prev_day_close`**，且它自述的归属
         （`prev_close_session`）必须恰为 date（SNAP_NEXT_PREV_CLOSE）。这不是凑数：
         290 份实测 289 份 ≤0.01%、含全部 71 份陈旧文件；08-31 快照的 prev_day_close
         恰还原 08-28 的 DE 630.33 / CVX 201.86 / VZ 50.10。归属自证，下一份若隔了
         交易日（如 09-05 没跑）就对不上而返回 None，不需要另算日历。
         当日快照根本不存在时同样问这一步。
      3. 都不行 → `(None, "snapshot_<判决>")`，判决说明当日快照卡在哪一步
         （no_snapshot / stale_intraday / session_open / unverifiable / …）——调用方据此标不可用。

    ⚠️ 为什么不「照用 + 降级标签」：这个价的终点是 `price_at_predict`（T+7 收益的
    入场价），而 predictions 表**没有**价格来源列 —— 标签到不了库里，降级在账本上
    与官方收盘无从分辨。实时管道（v0.45.234）退用陈旧价是因为那里还有 price=0
    整只跳过的代价要权衡；补跑缺一行只是少一个样本，错一个入场价是污染一个样本。
    """
    import cboe_options as co

    verdict = "no_snapshot"
    snap = load_ticker(date, ticker, ref=ref, repo=repo)
    if snap:
        # now = 抓取时刻：判的是「抓的时候这一场收了没有」，不是补跑此刻
        # （fetched_at 缺失时退回本机此刻——补跑必在那场之后，只剩 last_trade 一条判据，仍成立）
        verdict, session = co.close_verdict(
            {"last_trade_time": snap.get("last_trade_time_et")}, _fetched_at(snap))
        px = snap.get("price_at_fetch")
        if verdict == co.CLOSE_OFFICIAL and (session is None or session.isoformat() != date):
            verdict = "session_mismatch"
        elif verdict == co.CLOSE_OFFICIAL:
            if (isinstance(px, (int, float)) and not isinstance(px, bool)
                    and math.isfinite(px) and px > 0):
                return float(px), SNAP_CLOSE
            verdict = "price_invalid"

    # 当日快照没有 / 不是官方收盘 ⇒ 问其后第一份快照的 prev_day_close（归属自证）
    later = [d for d in available_dates(ref, repo) if d > date]
    if later:
        nxt = load_ticker(later[0], ticker, ref=ref, repo=repo)
        if nxt:
            pc = co.prev_close_session({"last_trade_time": nxt.get("last_trade_time_et"),
                                        "prev_day_close": nxt.get("prev_day_close")})
            if pc and pc[0].isoformat() == date:
                return pc[1], SNAP_NEXT_PREV_CLOSE
    return None, f"snapshot_{verdict}"


def load_market(date: str, *, ref: Optional[str] = None,
                repo: Optional[str] = None) -> Optional[dict]:
    """载入某日大盘段。`degraded_sections` 里列出的段会被**剔除**而非留着。

    留着比删掉危险：兜底常量与观测值同形，下游读到 15.0/15.75/16.5
    不会觉得有什么不对（v0.43.24 同款事故）。
    """
    txt = _git_show(f"{SNAPSHOT_SUBDIR}/{date}/market.json", ref, repo)
    if not txt:
        return None
    try:
        m = json.loads(txt)
    except ValueError:
        return None
    deg = m.get("degraded_sections") or {}
    cboe = m.get("cboe")
    if isinstance(cboe, dict) and deg:
        m["cboe"] = {k: v for k, v in cboe.items() if k not in deg}
        m["dropped_degraded"] = sorted(deg)
    return m


def load_manifest(date: str, *, ref: Optional[str] = None,
                  repo: Optional[str] = None) -> Optional[dict]:
    txt = _git_show(f"{SNAPSHOT_SUBDIR}/{date}/manifest.json", ref, repo)
    if not txt:
        return None
    try:
        return json.loads(txt)
    except ValueError:
        return None


# ══════════════════════════════════════════════════════════════════
# 快照模式：装载到 cboe_options，四个取数入口一并接管
# ══════════════════════════════════════════════════════════════════

@contextmanager
def snapshot_mode(date: str, *, ref: Optional[str] = None, repo: Optional[str] = None,
                  allow_unverified: bool = False):
    """在 with 块内让 `cboe_options` 的取数改走该日快照。

    进入前先确认该日有 manifest —— 没有就直接 `SnapshotUnavailable`，
    而不是进去之后每个标的各失败一次（那样会把「没这天」读成「30 只全挂」）。
    """
    import cboe_options as co

    _man = load_manifest(date, ref=ref, repo=repo)
    if _man is None:
        raise SnapshotUnavailable(
            f"{ref} 上没有 {date} 的快照（已有：{', '.join(available_dates(ref, repo)) or '无'}）")

    # v0.45.58：manifest 在不等于标的能载入。
    #
    # 旧实现只验 manifest 就 yield，调用方随即打印「📦 云端快照模式」——
    # 而如果每个 load_ticker 都返回 None，扫描会**静默退回实时抓取**，
    # 却顶着一个宣称走了快照的标签。这正是 check_label_honesty 要抓的形态：
    # 标签宣称成功，它所管辖的值却是空的。
    #
    # 实测代价：`vintage_date` 于 v0.45.36 加进主线的生产端，但云端 routine
    # 跑的是 cloud-snapshots 分支上的旧脚本（该字段出现 0 次），于是**每一份
    # 快照都缺它**，而消费端对缺它的一律 return None。8-26 与 8-27 两天
    # 各 30/30 完好的快照，一份都没被用上 —— 兜底自那时起就是死的，
    # 且因为这条假标签，看起来一直是活的。
    _ok = [t for t in (_man.get("ok") or []) if isinstance(t, str)]
    _probe = _ok[:5]
    _loadable = sum(1 for t in _probe
                    if load_ticker(date, t, ref=ref, repo=repo,
                                   allow_unverified=allow_unverified) is not None)
    if _probe and _loadable == 0:
        _why = ""
        _raw = _git_show(f"{SNAPSHOT_SUBDIR}/{date}/{_probe[0]}.json", ref, repo)
        if _raw:
            try:
                if json.loads(_raw).get("vintage_date") is None:
                    _why = ("：快照缺 `vintage_date`（生产端早于 v0.45.36）。"
                            "确认新鲜度后可用 allow_unverified=True，"
                            "根治要更新 cloud-snapshots 分支上的 cloud_snapshot_fetch.py")
            except ValueError:
                pass
        raise SnapshotUnavailable(
            f"{date} 的 manifest 称 {_man.get('tickers_ok')} 只可用，"
            f"但抽验 {len(_probe)} 只**一只都载不进来**{_why}")
    if _probe and _loadable < len(_probe):
        _log.warning("%s 快照抽验 %d/%d 可载入——部分标的会退回实时抓取",
                     date, _loadable, len(_probe))

    cache: Dict[str, Optional[dict]] = {}

    def provider(ticker: str) -> Optional[dict]:
        k = ticker.upper()
        if k not in cache:
            cache[k] = load_ticker(date, k, ref=ref, repo=repo,
                                   allow_unverified=allow_unverified)
        return cache[k]

    # v0.45.59：宏观与期权链**同进同出**。
    # 只装一半的后果是报告里期权是目标日的、宏观是运行当天的，而且无从分辨
    # —— `market.json` 此前在生产代码里 0 个调用者，8/27 的真实 VIX 期限结构
    # 天天被抓下来、提交、然后没人读（「死字段：算了没人读」）。
    _macro_installed = False
    try:
        import fred_macro as _fm
        _fm.set_macro_snapshot(date, load_market(date, ref=ref, repo=repo))
        _macro_installed = True
    except Exception as _e_fm:  # noqa: BLE001 - 宏观装不上不阻断期权链
        _log.warning("宏观快照未装上（宏观将是运行当天的口径）: %s", _e_fm)

    co.set_snapshot_provider(provider)
    try:
        yield provider
    finally:
        co.set_snapshot_provider(None)
        if _macro_installed:
            try:
                import fred_macro as _fm2
                _fm2.set_macro_snapshot(None)
            except Exception:  # noqa: BLE001 - 卸载失败只影响同进程后续调用
                _log.warning("宏观快照卸载失败")
