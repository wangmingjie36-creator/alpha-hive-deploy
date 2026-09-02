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

盘中冻结快照同样默认拒绝（v0.45.88）
------------------------------------
`vintage_date` 只能证明「这份数据属于哪一天」，证不了「它是不是那天的**收盘**」。
CBOE CDN 对个别符号刷新滞后时，记录会冻在盘中某刻却仍带正确日期——
2026-08-28 的 TMO 冻在 09:45:27（当日其余 29 只均 15:59~16:00），
存下的 `price_at_fetch=626.325` 标着 `price_source=cboe_close`，
而真实收盘是 622.18（差 0.67%）。生产端 v0.45.88 起给这类文件打
`intraday_freeze_suspect`，消费端据此**默认拒绝**。

判据与隔壁 `load_market` 剔除 `degraded_sections` 同源：**留着比删掉危险**——
盘中价与收盘价同形，下游读到 626.325 不会觉得有什么不对。
需要时用 `allow_freeze_suspect=True` 显式取用（`full_chain_oi` 相对可信：
OI 是日频结算量，盘中与收盘同值）。

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
import os
import subprocess
from contextlib import contextmanager
from typing import Dict, List, Optional

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
                repo: Optional[str] = None, allow_unverified: bool = False,
                allow_freeze_suspect: bool = False) -> Optional[dict]:
    """载入某日某标的的快照；不存在、vintage 不符或疑似盘中冻结返回 None。

    `allow_unverified=True` 才接受缺 `vintage_date` 的旧快照（v0.45.36 之前产出）。
    `allow_freeze_suspect=True` 才接受带 `intraday_freeze_suspect` 的快照
    （v0.45.88，见模块 docstring）——那种文件日期对但记录冻在盘中，
    `price_source=cboe_close` 名不副实，只有 `full_chain_oi` 相对可信。
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
    if d.get("intraday_freeze_suspect") and not allow_freeze_suspect:
        return None                      # 日期对但记录冻在盘中，收盘价名不副实

    d["full_chain_oi"] = _restore_numeric_keys(d.get("full_chain_oi"))
    return d


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
