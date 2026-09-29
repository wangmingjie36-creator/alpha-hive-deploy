#!/usr/bin/env python3
"""
🐝 Alpha Hive — 扫描字段覆盖率闸 (v0.45.42)
===========================================
把「这次扫描的数据是不是大面积没抓到」从**事后翻 JSON 才发现**，
变成扫完当场可判。

为什么需要
----------
2026-08-26 14:10 那次扫描，yfinance 全线返回空：

    rv_30d        30/30 → 1/30
    iv_rank       29/30 → 1/30
    iv_rv_spread  30/30 → 1/30
    ChronosBee    详情整个是 {}（催化剂全丢）

而**扫描退出码是 0，日报照常生成、照常推 Slack、照常上站**。
每个字段的失败都被 `except → return _empty` 老实接住了，没有一个是 bug；
问题是**没有任何一层在看"老实降级"发生了多少次**。
这正是 MEMORY「静默降级三件套」记的第三条：编排器只看退出码。

它不做什么
----------
- 不发通知（与 scan_continuity 一致，通知与否由编排器决定）
- 不阻止报告生成 —— 数据缺失是事实，报告该出还得出，只是必须**可见**
- 不重试取数 —— 重试是取数层的事（http_gate），这里只负责报告事实

退出码
------
    0  覆盖率健康
    1  检出降级（某字段覆盖率低于阈值）
    3  无法判定（结果文件不存在/不可解析）—— 3 而非 2，编排器把 2 留给"脚本不存在"

用法
----
    /usr/local/bin/python3 scan_coverage_gate.py                    # 判当日
    /usr/local/bin/python3 scan_coverage_gate.py --date 2026-08-26
    /usr/local/bin/python3 scan_coverage_gate.py --quiet --out cov.json

输出契约（v0.45.355，`step_contract`）
--------------------------------------
`--out` 写 `step_contract.envelope(...)`：原有键原样留在顶层（编排器 Step 12 现行内联解析照读）。
⚠️ **撞名**：本工具是四个里唯一一个结果里本来就有 `date` 的（`check()` 的返回值）。外壳把
`date` 列为保留键、撞名即抛，所以写盘时把它从 payload 里**拿出来、同值交给外壳**——JSON 顶层
`date` 的键名与取值都与改造前一致（`--date` 或缺省业务日），`check()` 的返回值本身不变
（`_render` 与进程内调用方照读 `res["date"]`）。缺省业务日 = `step_contract.business_today()`（洛杉矶当日，
与扫描给 `.swarm_results_<date>.json` 取名的口径相同）；⚠️ 它**不等于**编排器本机时区的 `DATE_STR`
（2026-11-01 起冬令时每天有一小时差一天，见 `business_today` 的 docstring）。
0 ⇒ ok、1 ⇒ attention、3 ⇒ undetermined；未捕获异常 ⇒ 退出码 3 + `status: "error"` 外壳
（此前 Python 默认 1 = 「检出降级」）。`attention` 由 `contract_attention()` 显式列出。
`step_contract` 本身导入失败 ⇒ 入口也是退出码 3（不写 `--out`）；**本模块其余的导入期失败仍是 1**
（`run_tool` 兜不到 import，见其 docstring）。
`--out` 的父目录不存在 ⇒ 与改造前一样是未捕获异常（`write_out` 不建目录），现在按崩溃记退出码 3，
崩溃路径同样写不出外壳、只打 stderr。
⚠️ 非规范的 `--date`（如 `2026-8-26`）过不了外壳的日期校验 ⇒ 按崩溃记（退出码 3，
改造前同一输入也是 3：结果文件名对不上 ⇒ 无法判定）。编排器不传 `--date`。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# `step_contract` 本身导入失败 ⇒ 置哨兵，`__main__` 入口见哨兵退出码 3——不让它落成 Python 默认的 1
# （= 本工具的「检出降级」）。⚠️ 只兜这一个 import：本模块其余的导入期失败仍是 1（见 step_contract.run_tool）。
try:
    import step_contract
    _STEP_CONTRACT_IMPORT_ERROR: Optional[str] = None
except Exception as _e:  # noqa: BLE001 —— 连语法错也要兜：哨兵只记下原因，入口据此退出码 3
    step_contract = None
    _STEP_CONTRACT_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

# v0.45.260（数据根迁移阶段 2）：`ROOT` 此前是 `Path(__file__).parent`（模块级
# 常量），完全不读 `ALPHA_HIVE_HOME`——本模块是编排器 Step 12 每日活跃调用的
# 覆盖率闸（`alpha-hive-orchestrator.sh:1240-1241`，只传 `--quiet --out`，
# **不传** `--file`，走的正是这条默认分支）。改为覆盖钩子 + 调用时求值。
# 生产今天不设 `ALPHA_HIVE_HOME` 时兜底到同一个仓库根，行为不变。
ROOT = None

_TOOL = "scan_coverage_gate"
#: 退出码 → 外壳 status（v0.45.355）。退出码约定本身不变；崩溃的 3 由 run_tool 写 "error"。
_STATUS_BY_RC = {0: "ok", 1: "attention", 3: "undetermined"}


def _root() -> Path:
    """`.swarm_results_<date>.json` 所在目录，**调用时求值**。"""
    if ROOT is not None:
        return Path(ROOT)
    from hive_logger import PATHS
    return PATHS.home

# ── 受监视字段 ──────────────────────────────────────────────────────
# min_coverage：低于该比例即判降级。
# 阈值不是「可接受的坏」，是「低于此必然是系统性故障而非个别标的没数据」。
# 定在 0.70：单只标的偶发取数失败很常见（新上市/停牌/低流动性），
# 但 30 只里超过 9 只同时失败，只可能是上游整体挂了。
FIELDS: List[Dict[str, Any]] = [
    {"key": "rv_30d", "path": "OracleBeeEcho.rv_30d",
     "min_coverage": 0.70, "source": "yfinance 日K",
     "note": "IV Rank(hv_proxy) 与 IV-RV 价差都由它派生，丢它等于丢三个指标"},
    {"key": "iv_rank", "path": "OracleBeeEcho.iv_rank",
     "min_coverage": 0.70, "source": "yfinance 日K（hv_proxy 期）",
     "note": "ML 模型头号特征（实测 importance 0.267）"},
    {"key": "iv_current", "path": "OracleBeeEcho.iv_current",
     "min_coverage": 0.70, "source": "CBOE 期权链",
     "note": "走 CBOE，与上面两项**不同源**——同时挂说明是网络层而非单一数据源"},
    {"key": "iv_skew_ratio", "path": "OracleBeeEcho.iv_skew_ratio",
     "min_coverage": 0.70, "source": "CBOE 期权链"},
    {"key": "put_call_ratio", "path": "OracleBeeEcho.put_call_ratio",
     "min_coverage": 0.70, "source": "CBOE 期权链"},
    {"key": "iv_rv_spread", "path": "OracleBeeEcho.iv_rv_spread",
     "min_coverage": 0.70, "source": "yfinance 日K（派生自 rv_30d）"},
    {"key": "catalysts", "path": "ChronosBeeHorizon.catalysts",
     "min_coverage": 0.40, "source": "yfinance 财报日历",
     "note": "阈值低于其余项：并非每只标的在任意时点都有已知催化剂，"
             "0.40 是经验下限（8/25 实测 21/30=0.70）"},
]


def _dig(tr: Dict, dotted: str) -> Any:
    """agent_details.<Agent>.details.<field> 的简写路径"""
    agent, field = dotted.split(".", 1)
    det = ((tr.get("agent_details") or {}).get(agent) or {}).get("details") or {}
    return det.get(field)


def _present(v: Any) -> bool:
    """有值 = 非 None、非空容器。0 与 False 算有值（它们是合法读数）"""
    if v is None:
        return False
    if isinstance(v, (list, dict, str)):
        return len(v) > 0
    return True


def check(date: str, results_path: Optional[Path] = None) -> Dict[str, Any]:
    path = results_path or (_root() / f".swarm_results_{date}.json")
    if not path.exists():
        return {"date": date, "determinable": False,
                "reason": f"结果文件不存在：{path.name}"}
    try:
        results = json.loads(path.read_text())
    except Exception as e:  # noqa: BLE001
        return {"date": date, "determinable": False,
                "reason": f"结果文件不可解析：{type(e).__name__}: {e}"}
    if not isinstance(results, dict) or not results:
        return {"date": date, "determinable": False,
                "reason": "结果文件为空或格式异常"}

    n = len(results)
    rows = []
    for spec in FIELDS:
        have = sum(1 for tr in results.values()
                   if isinstance(tr, dict) and _present(_dig(tr, spec["path"])))
        cov = have / n if n else 0.0
        rows.append({
            "field": spec["key"], "source": spec["source"],
            "have": have, "total": n, "coverage": round(cov, 4),
            "min_coverage": spec["min_coverage"],
            "degraded": cov < spec["min_coverage"],
            "note": spec.get("note", ""),
        })

    degraded = [r for r in rows if r["degraded"]]
    # 多个**不同数据源**同时降级 ⇒ 大概率是网络/闸门层，不是某个源挂了
    srcs = {r["source"].split()[0] for r in degraded}
    return {
        "date": date, "determinable": True, "tickers": n,
        "ticker_names": sorted(results),    # v0.45.360：账本核对要知道「该有哪些」
        "healthy": not degraded, "fields": rows,
        "degraded_fields": [r["field"] for r in degraded],
        "likely_network_layer": len(srcs) > 1,
    }


# ── 账本入场价可用性（v0.45.360，离线，默认跑）──────────────────────
# 2026-09-24 / 09-25 两天 predictions 各 30/30 行 price_at_predict=0：
# 09-24 CBOE 源站整源停更 + yfinance 限流冷却，09-25 本机 DNS 解析失败。
# 0 是本仓「没有这个价」的标记——全仓统计一律 `price_at_predict > 0`，
# 所以这 60 行对**全部**收益 / IC 计算不可见，且 close_correction 也只选
# `> 0` 的行，永远补不回来。
#
# 当时谁红了？Step 2 RC=1、Step 12 的**字段**覆盖率 0/30 —— 都是「这一天
# 数据差」，没有一处说「账本少了 30 个样本、而且不会自己回来」。
# `Backtester.last_save_stats` 数了这件事，但全仓零读者；那条 WARNING 只进日志。
# 更糟的是字段全健康、只丢入场价的日子（08-12 / 08-14 的 BRK-B）：**什么都不红**。
#
# 所以直接查**终点**（账本），不查中间量（Scout 价）：09-17 Scout 价 0/30，
# 可 yfinance 兜底补上了、账本 30/30 可用——看中间量会误报。
# 阈值是「任何一行」而不是覆盖率比例：字段缺一只是报告少个数，入场价缺一只是
# 永久少一个样本，且每一行都可用 entry_price_backfill.py 补回。
# 历史全量对照（本地 41 份 .swarm_results）：自 08-12 起 missing 恒为 0，
# 零价行只出现在 08-12 / 08-14 / 09-24 / 09-25 —— 该闸只会在这四天变红。
_ENTRY_FIELD = "entry_price"


def _usable_price(p: Any) -> bool:
    import math
    return (isinstance(p, (int, float)) and not isinstance(p, bool)
            and math.isfinite(p) and p > 0)


def check_entry_prices(date: str, tickers: Optional[List[str]] = None,
                       db_path: Optional[str] = None) -> Dict[str, Any]:
    """当日扫描标的在 predictions 里是否都落了**可用**入场价。纯离线，只读。

    - `zero`：落库了但价不可用（NULL / 0 / 负 / 非有限）——对全部统计不可见
    - `missing`：扫描结果有、账本没有（`tickers` 给了才判）
    - `backlog`：**其他日期**仍未补的不可用行 {date: [ticker…]}，只报不进退出码
      （用户可能决定某些行不补，恒红会把人养成不看的习惯）
    """
    import os
    import sqlite3
    if db_path is None:
        from hive_logger import PATHS
        db_path = str(PATHS.db)
    if not os.path.exists(db_path):          # 先判存在：sqlite 打开不存在的路径会建空库
        return {"determinable": False, "reason": f"{db_path} 不存在"}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = {t: p for t, p in conn.execute(
                "SELECT ticker, price_at_predict FROM predictions WHERE date=?", (date,))}
            bl_rows = conn.execute(
                "SELECT date, ticker, price_at_predict FROM predictions "
                "WHERE date <> ? ORDER BY date, ticker", (date,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return {"determinable": False, "reason": f"账本读取失败：{type(e).__name__}: {e}"}

    backlog: Dict[str, List[str]] = {}
    for d, t, p in bl_rows:
        if not _usable_price(p):
            backlog.setdefault(d, []).append(t)

    expected = sorted(set(tickers)) if tickers else sorted(rows)
    if not expected:
        return {"determinable": False, "reason": f"{date} 无预测记录，也没给扫描标的",
                "backlog": backlog}
    zero = sorted(t for t, p in rows.items() if not _usable_price(p))
    missing = sorted(set(expected) - set(rows)) if tickers else []
    have = sum(1 for t in expected if t in rows and _usable_price(rows[t]))
    return {
        "determinable": True, "date": date, "expected": len(expected), "have": have,
        "zero": zero, "missing": missing,
        "backlog": backlog, "backlog_rows": sum(len(v) for v in backlog.values()),
        "healthy": not zero and not missing,
    }


def merge_entry_prices(res: Dict[str, Any], ep: Dict[str, Any]) -> None:
    """把账本入场价作为一个 `fields` 行并进覆盖率结果。

    刻意并进 `fields` / `degraded_fields` / `healthy`，而不是另起一段：编排器
    Step 12 的摘要行只列 `fields` 里 degraded 的项、status.json 只记 healthy/degraded
    ——并进去，**不改编排器**就能让「entry_price 0/30」出现在 WARN 行和 status.json 里。
    无法判定（库不存在等）不改结论：那不是这一步能证明的失败，照实写进 `entry_price` 段。
    """
    res["entry_price"] = ep
    if not ep.get("determinable") or not res.get("determinable"):
        return
    bad = not ep["healthy"]
    res["fields"].append({
        "field": _ENTRY_FIELD, "source": "predictions 账本",
        "have": ep["have"], "total": ep["expected"],
        "coverage": round(ep["have"] / ep["expected"], 4) if ep["expected"] else 0.0,
        "min_coverage": 1.0, "degraded": bad,
        "note": ("入场价不可用的行对全部收益/IC 统计不可见、且不会自愈；"
                 "补：/usr/local/bin/python3 entry_price_backfill.py --date " + ep["date"]
                 + "（默认 dry-run）"),
    })
    if bad:
        res["healthy"] = False
        res["degraded_fields"] = list(res.get("degraded_fields") or []) + [_ENTRY_FIELD]


def _render_entry_backlog(ep: Dict[str, Any]) -> str:
    if not ep.get("backlog"):
        return ""
    days = ", ".join(f"{d}×{len(v)}" for d, v in sorted(ep["backlog"].items()))
    return (f"⚠️  账本积压：其他日期仍有 {sum(len(v) for v in ep['backlog'].values())} 行"
            f"入场价不可用（{days}）——entry_price_backfill.py 可补")


# ── 价格可信度交叉核验（v0.45.45，需网络，默认不跑）─────────────────
# 2026-08-26 实测暴露两种独立的 price_at_predict 污染：
#   ① **补跑窗口漂移**：为业务日 D 重跑扫描，但运行时刻已经越过 D 的交易时段，
#      现拉的价格早已不代表 D。实测 23:57 PDT（= 8/27 ET 凌晨 2:57）重跑，
#      NVDA 写进 219.53 而 8/26 真实收盘是 209.66（+4.71%），30 只里 8 只 >1%。
#      期权快照因为是**冻结**的反而干净（中位 0.14% vs DB 0.36%）——
#      「冻结」在这件事上是防护而不是风险。
#   ② **数据源单点乱码**：CRM 8/26 写进 232.93，而其近一个月最高仅 209.17，
#      且同一份快照的期权支撑位在 160/190（对应 ~$205 的股票）。
#      该值在任何日期都不存在，是 CBOE 当次读数本身坏了。8/24、8/25 都正确。
# 两者都只能靠**与外部收盘价对照**发现——内部自洽性检查抓不到。
_PRICE_DEV_WARN_PCT = 1.0    # 单只偏差超过即列出
_PRICE_DEV_BAD_PCT = 5.0     # 超过即判定为坏读数（几乎不可能是正常时点差）


def check_prices(date: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """把 predictions.price_at_predict 与该日真实收盘对照。

    需要网络。取不到收盘价时返回 determinable=False —— 不猜。

    v0.45.260（数据根迁移阶段 2 顺手发现）：`db_path` 原默认值是裸字符串
    `"pheromone.db"`——不是 `__file__` 派生，是**相对 CWD** 的路径字面量，
    同一物种的另一个变体（假设「在哪跑就在仓库根」）。本函数只在显式
    `--check-prices`（需网络，编排器默认不传）时才被调用，唯一生产调用点
    `main()` 未传 `db_path`，一直隐式依赖「从仓库根启动」。改为 `None` +
    调用时经 `PATHS.db` 解析；生产今天不设 `ALPHA_HIVE_HOME` 时两者兜底到
    同一个仓库根，行为不变。
    """
    import os
    import sqlite3
    if db_path is None:
        from hive_logger import PATHS
        db_path = PATHS.db
    if not os.path.exists(db_path):
        return {"determinable": False, "reason": f"{db_path} 不存在"}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db = {t: p for t, p in conn.execute(
        "SELECT ticker, price_at_predict FROM predictions WHERE date=?", (date,))}
    if not db:
        return {"determinable": False, "reason": f"{date} 无预测记录"}
    try:
        import warnings as _w
        _w.filterwarnings("ignore")
        import yfinance as yf
        from datetime import date as _d, timedelta as _td
        _s = _d.fromisoformat(date) - _td(days=4)
        _e = _d.fromisoformat(date) + _td(days=2)
        hist = yf.download(sorted(db), start=_s.isoformat(), end=_e.isoformat(),
                           interval="1d", progress=False, auto_adjust=False,
                           group_by="column")
        cl = hist["Close"]
        idx = [d for d in cl.index if str(d)[:10] == date]
        if not idx:
            return {"determinable": False, "reason": f"取不到 {date} 的收盘价"}
        row = cl.loc[idx[0]]
    except Exception as e:  # noqa: BLE001
        return {"determinable": False, "reason": f"收盘价取数失败：{type(e).__name__}: {e}"}

    devs, warn, bad = [], [], []
    for tk, px in db.items():
        try:
            real = float(row[tk])
        except Exception:  # noqa: BLE001
            continue
        if real != real or real <= 0:
            continue
        if not _usable_price(px):
            # v0.45.360：原先 `not px` 与「取不到收盘」一起 continue——入场价为 0 的行
            # 被静默剔出核验，30/30 为 0 的日子会报「无可比对样本」而不是「全坏」
            bad.append({"ticker": tk, "recorded": 0.0, "actual_close": round(real, 2),
                        "deviation_pct": -100.0})
            devs.append(100.0)
            continue
        d = (px - real) / real * 100
        devs.append(abs(d))
        rec = {"ticker": tk, "recorded": round(px, 2),
               "actual_close": round(real, 2), "deviation_pct": round(d, 2)}
        if abs(d) >= _PRICE_DEV_BAD_PCT:
            bad.append(rec)
        elif abs(d) >= _PRICE_DEV_WARN_PCT:
            warn.append(rec)
    if not devs:
        return {"determinable": False, "reason": "无可比对样本"}
    devs.sort()
    return {
        "determinable": True, "checked": len(devs),
        "median_dev_pct": round(devs[len(devs) // 2], 3),
        "max_dev_pct": round(devs[-1], 2),
        "warn": sorted(warn, key=lambda r: -abs(r["deviation_pct"])),
        "bad": sorted(bad, key=lambda r: -abs(r["deviation_pct"])),
        "healthy": not bad,
    }


# ── 来源标签诚实度（v0.45.53）──────────────────────────────────────
# 静态检查判不了「一个来源标签是不是在撒谎」：`"data_quality": "fallback"`
# 写在 except 块里本来就诚实，写在成功路径上才可疑 —— 同一行字面量，
# 诚实与否**取决于它在哪条分支上**，那是语义不是语法。
# （实测：全仓 98 处字面量来源标签、175 处结果词字面量，静态筛完全是噪音。）
#
# 但运行时判得了，而且判据很硬：
#
#     标签宣称取数成功 ⇒ 它管辖的值必须非空。
#
# 违反即矛盾 —— 要么标签在撒谎，要么值被谁清掉了，两种都该查。
# 这正是 2026-08-27 审计里 `"momentum": "real"` 那一类的运行时对应物：
# 当时动量兜底成 0.0，而质量标签硬编码自称 real。
_SUCCESS_LABELS = {"real", "cboe", "yfinance", "live", "verified",
                   "cboe_close", "cboe_intraday", "yfinance+fred"}

# (标签字段, 它管辖的值字段, 说明)
_LABEL_GOVERNS = [
    ("OracleBeeEcho.iv_rank_source", "OracleBeeEcho.iv_rank",
     "iv_rank_source 宣称有来源，iv_rank 却为空"),
    ("OracleBeeEcho.data_quality", "OracleBeeEcho.iv_current",
     "期权链标 real，iv_current 却为空"),
    ("OracleBeeEcho.data_quality", "OracleBeeEcho.put_call_ratio",
     "期权链标 real，put_call_ratio 却为空"),
]


def check_label_honesty(date: str, results_path: Optional[Path] = None) -> Dict[str, Any]:
    """核对来源标签与它管辖的值是否自洽。纯离线，读扫描结果即可。"""
    path = results_path or (_root() / f".swarm_results_{date}.json")
    if not path.exists():
        return {"determinable": False, "reason": f"结果文件不存在：{path.name}"}
    try:
        results = json.loads(path.read_text())
    except Exception as e:  # noqa: BLE001
        return {"determinable": False, "reason": f"不可解析：{type(e).__name__}: {e}"}
    if not isinstance(results, dict) or not results:
        return {"determinable": False, "reason": "结果为空"}

    contradictions = []
    checked = 0
    for tk, tr in results.items():
        if not isinstance(tr, dict):
            continue
        for label_path, value_path, why in _LABEL_GOVERNS:
            label = _dig(tr, label_path)
            if not isinstance(label, str) or label.lower() not in _SUCCESS_LABELS:
                continue          # 标签本身就说降级 ⇒ 诚实，跳过
            checked += 1
            if not _present(_dig(tr, value_path)):
                contradictions.append({
                    "ticker": tk, "label_field": label_path, "label": label,
                    "value_field": value_path, "why": why,
                })
    return {
        "determinable": True, "checked": checked,
        "contradictions": contradictions,
        "healthy": not contradictions,
    }


def _render_label_honesty(r: Dict[str, Any]) -> str:
    if not r.get("determinable"):
        return f"⚠️  来源标签核对无法判定：{r.get('reason')}"
    if r["healthy"]:
        return f"来源标签核对 · {r['checked']} 项宣称成功 · ✅ 全部与值自洽"
    out = [f"来源标签核对 · {r['checked']} 项宣称成功 · "
           f"❌ {len(r['contradictions'])} 处矛盾"]
    for c in r["contradictions"][:10]:
        out.append(f"  ❌ {c['ticker']:6} {c['label_field']}={c['label']!r} "
                   f"但 {c['value_field'].split('.')[-1]} 为空 —— {c['why']}")
    return "\n".join(out)


def _render_prices(pr: Dict[str, Any]) -> str:
    if not pr.get("determinable"):
        return f"⚠️  价格核验无法判定：{pr.get('reason')}"
    out = [f"价格核验 · {pr['checked']} 只 · 中位偏差 {pr['median_dev_pct']}% · "
           f"最大 {pr['max_dev_pct']}%"]
    for r in pr["bad"]:
        out.append(f"  ❌ {r['ticker']:6} 记录 {r['recorded']:>9.2f}  实际收盘 "
                   f"{r['actual_close']:>9.2f}  {r['deviation_pct']:+.2f}%  ← 坏读数")
    for r in pr["warn"]:
        out.append(f"  ⚠️  {r['ticker']:6} 记录 {r['recorded']:>9.2f}  实际收盘 "
                   f"{r['actual_close']:>9.2f}  {r['deviation_pct']:+.2f}%")
    out.append("✅ 价格可信" if pr["healthy"] else "❌ 检出坏价格 —— 这些标的的入场价不可用于收益计算")
    return "\n".join(out)


def _render(res: Dict[str, Any]) -> str:
    if not res.get("determinable"):
        return f"⚠️  无法判定 {res['date']}：{res['reason']}"
    out = [f"扫描字段覆盖率 · {res['date']} · {res['tickers']} 只标的"]
    for r in res["fields"]:
        mark = "❌" if r["degraded"] else "✅"
        out.append(f"  {mark} {r['field']:16} {r['have']:2}/{r['total']:2} "
                   f"({r['coverage']*100:5.1f}%  闸 {r['min_coverage']*100:.0f}%)  ← {r['source']}")
        if r["degraded"] and r["note"]:
            out.append(f"       ↳ {r['note']}")
    if res["healthy"]:
        out.append("✅ 覆盖率健康")
    else:
        out.append(f"❌ 降级字段：{', '.join(res['degraded_fields'])}")
        if res["likely_network_layer"]:
            out.append("   多个**不同数据源**同时降级 ⇒ 疑为网络/闸门层故障，"
                       "而非单一数据源不可用")
    return "\n".join(out)


def check_rate_limit(date: str, log_dir: str = "") -> Dict[str, Any]:
    """数当日编排器日志里的 yfinance 429，回答「为什么降级」而不只是「降了什么」。

    v0.45.56 起。8/27 的覆盖率报告准确列出了 rv_30d/iv_rank/iv_rv_spread/
    catalysts 各 0/30 —— 但**没说是被限流打空的**，于是那份报告读起来像
    「yfinance 今天没数据」，而真相是「我们把 yfinance 打到拒绝服务」。
    两者的修法完全相反：前者等它恢复，后者必须自己降速。

    证据本来就躺在日志里，不需要新管道：数一下就是了。

    阈值 100 是经验刻度而非实测最优（⚠️ 待验证）：已知 8/25=364 次时数据仍
    全须全尾、8/27=687 次时全空，真正的临界点在两者之间，尚未定位。
    """
    _dir = Path(log_dir) if log_dir else Path.home() / ".claude" / "logs"
    log = _dir / f"orchestrator-{date}.log"
    if not log.exists():
        return {"determinable": False, "reason": f"无 {log.name}", "healthy": True}
    try:
        txt = log.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return {"determinable": False, "reason": f"日志读取失败: {e}", "healthy": True}

    n = txt.count("Too Many Requests")
    # 首末时刻：限流是"一阵子"还是"全程"，决定它是否吃掉了整轮扫描
    stamps = re.findall(r"^(\d{2}:\d{2}:\d{2}).*Too Many Requests", txt, re.M)
    return {
        "determinable": True,
        "count": n,
        "first": stamps[0] if stamps else None,
        "last": stamps[-1] if stamps else None,
        "threshold": 100,
        "healthy": n < 100,
    }


def _rate_limit_verdict(rl: Dict[str, Any], fields_healthy: bool) -> str:
    """限流量高**且**字段降了 = 病因；限流量高但字段还全 = 预警。

    两者必须分开说。8/25 实测 364 次限流、数据却全须全尾；8/27 是 687 次、
    全空。把前者也写成「降级的直接原因」是假话 —— 那天没有降级。
    但它同样该报：赤字是那时开始攒的，三天后才还。
    """
    if rl["healthy"]:
        return ""
    if fields_healthy:
        return ("⚠️ 本轮数据仍是全的，但限流量已越闸 —— 这是**早期预警**："
                "8/25=364 次时数据还全，8/27=687 次时全空。")
    return ("yfinance 限流是本轮降级的直接原因；重试会加深它，"
            "应下调 resilience.yfinance_limiter 速率。")


def _render_rate_limit(r: Dict[str, Any], fields_healthy: bool = False) -> str:
    if not r.get("determinable"):
        return f"⚠️  限流检查无法判定：{r['reason']}"
    if r["healthy"]:
        return f"✅ yfinance 限流 {r['count']} 次（闸 {r['threshold']}）"
    return (f"❌ yfinance 限流 {r['count']} 次（闸 {r['threshold']}），"
            f"{r['first']}–{r['last']}\n       ↳ "
            + _rate_limit_verdict(r, fields_healthy))


def _exit_code(res: Dict[str, Any]) -> int:
    """退出码语义（与 scan_continuity 一致）：0 健康 / 1 检出降级 / 3 无法判定。

    v0.45.355 从 `main()` 末尾原样抽出——外壳 `status` 与退出码必须出自同一处判定。"""
    if not res.get("determinable"):
        return 3
    lh = res.get("label_honesty") or {}
    _degraded = (
        (not res["healthy"])
        or (lh.get("determinable") and not lh["healthy"])
        or (res.get("price_check", {}).get("determinable")
            and not res.get("price_check", {}).get("healthy", True))
    )
    return 1 if _degraded else 0


def contract_attention(res: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把全部检查结果**显式**翻成 `step_contract` 的 attention 条目（v0.45.355）。

    与 `_exit_code` 的三条降级判据一一对应（字段降级 / 标签矛盾 / 坏价格），各出一条
    warn 或 alarm；「疑为网络层」另出一条 warn（它是对降级的定性，决定修法方向）。
    级别取舍：字段缺失 = 数据**缺**（报告里显示「—」）⇒ warn；标签宣称成功而值为空、
    入场价是坏读数 = 数据或口径**可能已经错了** ⇒ alarm。
    限流是诊断项、**不进退出码**：字段降了它是病因 ⇒ warn；字段还全 ⇒ 早期预警，只作 info
    （status 仍 ok——`status=ok` 不许带 warn/alarm）。
    """
    A = step_contract.attention_item
    items: List[Dict[str, Any]] = []
    if not res.get("determinable"):
        items.append(A(f"{_TOOL}.undetermined", "warn",
                       f"无法判定字段覆盖率：{res.get('reason')}"))
        return items

    bad = [f for f in res.get("fields") or [] if f.get("degraded")]
    if bad:
        items.append(A(f"{_TOOL}.fields_degraded", "warn",
                       "降级字段：" + "；".join(
                           f"{f['field']} {f['have']}/{f['total']}"
                           f"（闸 {f['min_coverage']:.0%}，{f['source']}）" for f in bad)
                       + " —— 本次报告里这些指标会显示「—」"))
        if res.get("likely_network_layer"):
            srcs = sorted({f["source"].split()[0] for f in bad})
            items.append(A(f"{_TOOL}.likely_network_layer", "warn",
                           f"多个不同数据源同时降级（{'、'.join(srcs)}）⇒ 疑为网络/闸门层，"
                           f"而非单一数据源不可用"))
    elif not res.get("healthy", True):
        items.append(A(f"{_TOOL}.fields_degraded", "warn",
                       f"字段覆盖率降级：{', '.join(res.get('degraded_fields') or []) or '（未列出字段）'}"))

    lh = res.get("label_honesty") or {}
    if lh.get("determinable") and not lh.get("healthy", True):
        cs = lh.get("contradictions") or []
        shown = "；".join(f"{c['ticker']} {c['label_field']}={c['label']!r} 但 "
                         f"{c['value_field'].split('.')[-1]} 为空" for c in cs[:10])
        more = f" 等共 {len(cs)} 处" if len(cs) > 10 else ""
        items.append(A(f"{_TOOL}.label_contradictions", "alarm",
                       f"来源标签矛盾 {len(cs)} 处（标签宣称取数成功，值却为空）：{shown}{more}"))
    elif not lh.get("determinable"):
        items.append(A(f"{_TOOL}.label_honesty_undetermined", "info",
                       f"来源标签核对无法判定：{lh.get('reason')}"))

    pr = res.get("price_check")
    if isinstance(pr, dict):
        if pr.get("determinable") and not pr.get("healthy", True):
            items.append(A(f"{_TOOL}.bad_prices", "alarm",
                           "坏价格（入场价不可用于收益计算）：" + "；".join(
                               f"{r['ticker']} 记录 {r['recorded']} vs 收盘 {r['actual_close']}"
                               f"（{r['deviation_pct']:+.2f}%）" for r in pr.get("bad") or [])))
        elif pr.get("determinable") and pr.get("warn"):
            items.append(A(f"{_TOOL}.price_drift", "info",
                           "价格偏差 1%~5%（多为补跑窗口漂移）：" + "、".join(
                               r["ticker"] for r in pr["warn"])))
        elif not pr.get("determinable"):
            items.append(A(f"{_TOOL}.price_check_undetermined", "info",
                           f"价格核验无法判定：{pr.get('reason')}"))

    rl = res.get("rate_limit") or {}
    if rl.get("determinable") and not rl.get("healthy", True):
        fields_ok = bool(res.get("healthy"))
        items.append(A(f"{_TOOL}.rate_limit", "info" if fields_ok else "warn",
                       f"yfinance 限流 {rl.get('count')} 次（闸 {rl.get('threshold')}，"
                       f"{rl.get('first')}–{rl.get('last')}）："
                       + (rl.get("verdict") or _rate_limit_verdict(rl, fields_ok))))
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description="扫描字段覆盖率闸")
    ap.add_argument("--date", default=None, help="业务日期 YYYY-MM-DD（默认洛杉矶当日）")
    ap.add_argument("--file", default=None, help="直接指定 .swarm_results_*.json")
    ap.add_argument("--quiet", action="store_true", help="只输出结论行")
    ap.add_argument("--out", default=None, help="把完整结果写成 JSON")
    ap.add_argument("--log-dir", default="",
                    help="编排器日志目录（默认 ~/.claude/logs）；限流检查从这里读")
    ap.add_argument("--db", default=None,
                    help="predictions 库（默认 PATHS.db，调用时求值）；账本入场价核对读它")
    ap.add_argument("--check-prices", action="store_true",
                    help="额外核验 price_at_predict 与真实收盘（需网络，较慢）")
    args = ap.parse_args()

    # v0.45.355：缺省业务日改走 `step_contract.business_today()`（洛杉矶当日 = 扫描给结果文件取名的口径，
    # 也就是外壳 `date` 的定义）。原写法 `from timezone_utils import pdt_today` 引用的模块在本仓
    # **从未存在过**（git 历史里零提交），每次都静默落进 `except` 用本机日历日（America/Vancouver）——
    # 那条「PDT」是写在注释里、从没被执行过的断言。两地目前同日；2026-11-01 起温哥华常年 UTC-7，冬令时每天
    # 本机 00:00–01:00 两者差一天，那一小时里缺省值从此跟扫描的文件名走，而**不等于**编排器的 DATE_STR
    # （本机日）——要比新鲜度就显式传 --date（见 step_contract.business_today）。
    date = args.date or step_contract.business_today()

    res = check(date, Path(args.file) if args.file else None)
    # v0.45.360：账本入场价。必须在渲染/写盘之前并进 res，否则 --quiet 摘要与 --out 都看不见
    ep = check_entry_prices(date, res.get("ticker_names"), args.db)
    merge_entry_prices(res, ep)

    # v0.45.54 二次检查：`--out` 原先在这里就写盘，而 label_honesty / price_check
    # 是之后才算出来的 —— 默认路径（编排器用的 `--quiet --out`）写出的 JSON
    # **缺 label_honesty 段**，下游永远看不到这项检查的结果。
    # 现统一挪到全部检查之后写一次。
    if not args.quiet:
        print(_render(res))
    elif not res.get("determinable"):
        print(f"⚠️  {res['reason']}")
    elif not res["healthy"]:
        print(f"❌ {res['date']} 降级字段：{', '.join(res['degraded_fields'])}")

    _bl = _render_entry_backlog(ep)
    if _bl:
        print(_bl)

    lh = check_label_honesty(date, Path(args.file) if args.file else None)
    res["label_honesty"] = lh
    if not args.quiet:
        print()
        print(_render_label_honesty(lh))
    elif lh.get("determinable") and not lh["healthy"]:
        print(f"❌ {date} 来源标签矛盾 {len(lh['contradictions'])} 处")

    # 限流检查是**诊断项，不进退出码**：
    #   · 字段真降了 → 退出码已经是 1，限流只是补上「为什么」
    #   · 字段还全但 429 越闸 → 那是早期预警（8/25 实测 364 次、数据全须全尾），
    #     把它判成失败会让编排器对一次成功的扫描报错
    # 它的价值在于**回答病因**，不在于多亮一盏红灯。
    rl = check_rate_limit(date, args.log_dir)
    _fields_ok = bool(res.get("healthy"))
    if rl.get("determinable"):
        rl["verdict"] = _rate_limit_verdict(rl, _fields_ok)
    res["rate_limit"] = rl
    if not args.quiet:
        print()
        print(_render_rate_limit(rl, _fields_ok))
    elif rl.get("determinable") and not rl["healthy"]:
        _tail = "降级的直接原因" if not _fields_ok else "早期预警（本轮数据仍全）"
        print(f"❌ {date} yfinance 限流 {rl['count']} 次 —— {_tail}")

    if args.check_prices:
        pr = check_prices(date, args.db)
        res["price_check"] = pr
        if not args.quiet:
            print()
            print(_render_prices(pr))
        elif pr.get("determinable") and not pr["healthy"]:
            print(f"❌ {date} 坏价格：{', '.join(r['ticker'] for r in pr['bad'])}")

    # ── v0.45.54 二次检查：写盘与退出码都收敛到这里 ──
    # 原先坏价格那条是**提前 return 1**，会绕过写盘 —— 于是「检出问题」的那次
    # 恰好是 --out 拿不到 JSON 的那次，下游想查都查不了。
    # 退出码语义（与 scan_continuity 一致）：0 健康 / 1 检出降级 / 3 无法判定。
    rc = _exit_code(res)
    if args.out:
        # `date` 是外壳保留键：同值交给外壳，JSON 顶层 `date` 键名与取值不变（见模块 docstring）
        payload = {k: v for k, v in res.items() if k != "date"}
        step_contract.write_out(args.out, step_contract.envelope(
            _TOOL, res.get("date") or date, _STATUS_BY_RC[rc],
            attention=contract_attention(res), payload=payload))
    return rc


if __name__ == "__main__":
    if step_contract is None:
        # 没有 step_contract 就写不出外壳：只打 stderr，退出码 3（「无法判定」），不写 --out
        print(f"{_TOOL}: 无法导入 step_contract（{_STEP_CONTRACT_IMPORT_ERROR}）—— 退出码 3，按「无法判定」处理；"
              "本次不写 --out", file=sys.stderr)
        sys.exit(3)
    # v0.45.355：未捕获异常 ⇒ 退出码 3 + error 外壳（不再是 Python 默认的 1 = 「检出降级」）
    sys.exit(step_contract.run_tool(_TOOL, main))
