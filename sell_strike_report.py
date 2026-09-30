"""卖权行权价选择器 · 适配层（v0.45.333）：本地 markdown 报告 + MCP 现算。

⚠️ **不上公开网站**：本模块的输出**不得**拼进日报 `report["markdown_report"]`——
`alpha-hive-daily-*.md` 在 gh-pages 部署白名单与自动提交白名单里。报告只写
`<PATHS.sell_strike_state>/reports/sell-strike-<日期>.md`（私有状态目录，文件名不匹配任何
部署 / 自动提交规则），MCP 工具按需现算或读账本。

依赖只向下：report → ledger → candidates → levels。不 import 任何评分组件。
"""
from __future__ import annotations

import math
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

import sell_strike_candidates as C
import sell_strike_ledger as LG
import sell_strike_levels as L
from hive_logger import get_logger

_log = get_logger("sell_strike_report")

DISCLAIMER = "以下为公开信息研究与情景推演，**不构成投资建议**。仅记录与结算，不开仓、不影响任何评分。"

# 局限说明（报告与 MCP 共用同一份文字，别各写一份）
CAVEATS = (
    "OI 是 t−1 的（CBOE 每日开盘前更新一次），当日新开的仓位看不到。",
    "GEX 用朴素 OI 符号（call 记 +、put 记 −，即假设做市商多 call 空 put）；个股上 put 侧符号可能反了"
    "（Garleanu-Pedersen-Poteshman 2009），GEX 只作环境参考。",
    "结算只判**到期收盘**是否 ITM，不判存续期内是否被触及（日线只有收盘价）。",
    "账本行的报价是日报钩子收盘后取到的 CBOE 文件，MCP 现算是调用时刻的文件（盘中标 cboe_intraday）；"
    "报价真正是哪一刻的看 payload_last_trade_time 与 underlying_price_source。CBOE 偶尔在收盘后仍给盘中生成的文件"
    "（cboe_stale_intraday），或盘中就读了（cboe_intraday / session_live）：那样的行权利金是盘中报价、"
    "不是收盘口径，照记但不进预注册检验。"
    "无论哪种，实际最早 t+1 才能成交 ⇒ 权利金与收益率偏乐观。",
    "P(ITM)=N(d2) 是**风险中性**概率，不是真实概率；卖方有波动率风险溢价时实际 ITM 频率预期更低。",
    "金额为**每股**口径（×100 为每张）；卖按 bid、买按 ask。",
)

# 收益口径（报告与 MCP 共用）：单笔风险回报是主数字，年化只作次要。
# 年化 = 单笔 × 365 / DTE 是单利外推：7–14 DTE 的价差单笔 30% 会被写成「年化 800%」，
# 读者看到的是一个不可能兑现的数字（每期都得原样复制、不能有一次亏损）。
YIELD_NOTE = ("主数字是单笔风险回报 yield_raw = credit / 占用资金（collateral；价差 / 铁鹰即 credit / max_loss；"
              "卖 put 按现金担保 K、卖 call 按备兑 S、宽跨只计 put 侧担保）。年化 yield_annualized = "
              "yield_raw × 365 / DTE，是**单利年化**，只作次要参考——DTE 越短、价差类越会显得很大，不代表可实现收益。")

_TENOR_LABEL = {"monthly": "月度（21–45 DTE）", "weekly": "周度（7–<21 DTE）"}
_STRUCT_LABEL = {"short_put": "卖 put", "short_call": "卖 call", "bull_put_spread": "牛市 put 价差",
                 "bear_call_spread": "熊市 call 价差", "strangle": "宽跨", "iron_condor": "铁鹰"}
_SIGN_LABEL = {"positive": "正", "negative": "负", "zero": "零"}


def _num(x) -> Optional[float]:
    if isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _f(v, nd=2, prefix="", suffix="") -> str:
    v = _num(v)
    return "—" if v is None else f"{prefix}{v:,.{nd}f}{suffix}"


def _pct(v, nd=1) -> str:
    v = _num(v)
    return "—" if v is None else f"{v * 100:.{nd}f}%"


# ─────────────────────────────── 结构报价（报告与 MCP 共用）

def route_rungs(row: dict) -> Dict[str, Optional[float]]:
    rt = row.get("route") or {}
    out = {}
    for side in LG.SIDES:
        try:
            out[side] = C.rung_for(rt.get(side))
        except ValueError:
            out[side] = None
    return out


def _quote_summary(q: dict) -> dict:
    return {k: q.get(k) for k in ("structure", "rung", "credit", "max_loss", "collateral",
                                  "yield_raw", "yield_annualized", "breakevens", "quotable",
                                  "reason", "notes")}


def structures_for_row(row: dict) -> dict:
    """六个结构在 route 档与基线档（BASE_RUNG）的报价。route 判 unavailable 的一侧给 `rung_unavailable`。"""
    ladder = row.get("ladder")
    if not isinstance(ladder, dict):
        return {"route_rungs": None, "route": {}, "base": {}}
    rr = route_rungs(row)
    base = {s: C.BASE_RUNG for s in LG.SIDES}
    out = {"route_rungs": rr, "route": {}, "base": {}}
    for label, rungs in (("route", rr), ("base", base)):
        for st in C.STRUCTURES:
            if st in ("short_put", "bull_put_spread"):
                rung = rungs["put"]
            elif st in ("short_call", "bear_call_spread"):
                rung = rungs["call"]
            else:
                rung = dict(rungs) if all(v is not None for v in rungs.values()) else None
            out[label][st] = _quote_summary(C.structure_quote(ladder, st, rung))
    return out


# ─────────────────────────────── markdown

def _env_line(row: dict) -> str:
    env = row.get("env") or {}
    sign = _SIGN_LABEL.get(env.get("sign_at_spot"), "不可得")
    return (f"S={_f(row.get('underlying_price'), 2, '$')}（{row.get('underlying_price_source') or '—'}）"
            f" · 现价处净 gamma：{sign}（{env.get('curve_state') or '—'}，视图 {env.get('view') or '—'}）"
            f" · ZG 最近 {_f(env.get('zg_nearest'), 2, '$')}"
            f"（下方 {_f(env.get('zg_below_pct'), 1, suffix='%')} / 上方 {_f(env.get('zg_above_pct'), 1, suffix='%')}）"
            f" · 净 Major+ {_f(env.get('net_major_pos_strike'), 2, '$')}"
            f" / 净 Major− {_f(env.get('net_major_neg_strike'), 2, '$')}"
            f" · 单边极值 call {_f(env.get('call_side_extreme_strike'), 2, '$')}"
            f" / put {_f(env.get('put_side_extreme_strike'), 2, '$')}")


def _route_line(row: dict) -> str:
    rt = row.get("route") or {}
    rr = route_rungs(row)
    parts = [f"{side}={rt.get(side) or '—'}"
             + (f"（{rr[side]:.2f}Δ）" if rr.get(side) is not None else "") for side in LG.SIDES]
    reasons = "；".join(rt.get("reasons") or []) or "—"
    return f"路由 v{rt.get('rule_version', '—')}：" + " · ".join(parts) + f" —— {reasons}"


def _ladder_table(row: dict) -> List[str]:
    ladder = row.get("ladder") or {}
    head = ("| 档位 | put K | Δ | P(ITM) | σ距离 | bid | 点差 | 单笔回报（年化） "
            "| call K | Δ | P(ITM) | σ距离 | bid | 点差 | 单笔回报（年化） |")
    lines = [head, "|" + "---|" * 15]
    for d in C.LADDER_DELTAS:
        key = f"{d:.2f}"
        cells = [key]
        for side in LG.SIDES:
            slot = (ladder.get(side) or {}).get(key) or {}
            leg = slot.get("short")
            if not isinstance(leg, dict):
                why = ",".join(slot.get("reasons") or []) or "—"
                cells += [f"—（{why}）", "", "", "", "", "", ""]
                continue
            q = C.structure_quote(ladder, f"short_{side}", d)
            cells += [_f(leg.get("strike"), 2), _f(leg.get("delta"), 3), _pct(leg.get("itm_prob")),
                      _f(leg.get("sigma_distance"), 2), _f(leg.get("bid"), 2),
                      _pct(leg.get("spread_pct"), 0),
                      _yield_cell(q) if q.get("quotable") else f"—（{q.get('reason')}）"]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _yield_cell(q: dict) -> str:
    """单笔回报为主、年化在括号里（见 YIELD_NOTE）。"""
    return f"{_pct(q.get('yield_raw'), 2)}（年化 {_pct(q.get('yield_annualized'))}）"


def _structure_table(row: dict) -> List[str]:
    s = structures_for_row(row)
    lines = ["| 结构 | route 档 credit | max_loss | 单笔回报 | 年化（单利） "
             "| 基线档 credit | max_loss | 单笔回报 | 年化（单利） |",
             "|---|---|---|---|---|---|---|---|---|"]
    for st in C.STRUCTURES:
        cells = [_STRUCT_LABEL.get(st, st)]
        for label in ("route", "base"):
            q = s[label].get(st) or {}
            if not q.get("quotable"):
                cells += [f"—（{q.get('reason')}）", "", "", ""]
                continue
            ml = "无上限" if q.get("max_loss") is None else _f(q.get("max_loss"), 2)
            cells += [_f(q.get("credit"), 2), ml, _pct(q.get("yield_raw"), 2), _pct(q.get("yield_annualized"))]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _coverage_line(rows: List[dict]) -> str:
    rec = sum(1 for r in rows if r.get("status") == "recorded")
    unav = Counter(r.get("unavailable_reason") for r in rows if r.get("status") != "recorded")
    reasons = "、".join(f"{k} {v}" for k, v in sorted(unav.items())) if unav else "无"
    return f"覆盖：记录 {rec} / 不可得 {sum(unav.values())}（{reasons}）"


def _assess_safe(tenor: str, state_dir, as_of: Optional[str], *, freeze: bool = False) -> dict:
    """`freeze=True` 只由 `render_markdown(freeze=True)` ← `write_local_report(freeze=True)` ← 日报钩子传；
    MCP 两条路径都走缺省 False（就绪但未冻结 ⇒「已就绪，等待日报冻结」，不写文件）。

    失败**必须打 WARNING**（2026-09-26 二次检查）：原实现只把异常变成报告里的一行字，日志里什么都没有——
    冻结文件坏了 / 冻结时磁盘满（ENOSPC），日报钩子每天都失败、每天都没人知道（钩子只在
    `write_local_report` **抛**时才打 warning，而这里把异常吞成了返回值）。冻结路径另打一句专门的，
    写明「检验不会被冻结」。"""
    try:
        return LG.assess(tenor, state_dir=state_dir, as_of=as_of, freeze=freeze)
    except Exception as exc:  # noqa: BLE001 - 报告不因 assess 失败而空白，但原因要写出来、日志要响
        if freeze:
            _log.warning("卖权预注册检验 就绪度判定/冻结失败（%s，日报钩子冻结路径）：%s: %s——"
                         "本地报告只写一行原因，检验**不会**被冻结；修好之前每天都会在这里失败",
                         tenor, type(exc).__name__, exc, exc_info=True)
        else:
            _log.warning("卖权账本就绪度判定失败（%s，只读路径 as_of=%s）：%s: %s",
                         tenor, as_of, type(exc).__name__, exc)
        return {"tenor": tenor, "status": "error", "error": f"{type(exc).__name__}: {exc}"}


def _assess_line(a: dict) -> str:
    if a.get("status") == "error":
        return f"⚠️ {a.get('tenor')}：就绪度判定失败（{a.get('error')}）—— 环境路由仅供参考"
    line = LG.summary_line(a)
    if a.get("status") == "ready":
        if "test" in a:
            dec = " · ".join(f"{s}: {(a.get('test') or {}).get(s, {}).get('decision')}" for s in LG.SIDES)
            line += f"；预注册检验（单侧、按日分块置换、α_each={LG.PREREG['alpha_each']}）：{dec}"
        elif a.get("awaiting_freeze"):
            pass              # summary_line 已写「已就绪，等待日报冻结」
        else:
            # 冻结结果存在但本日早于冻结日（as_of 回看）：检验只跑一次，不在更早的样本上重算
            line += (f"；预注册检验已于 {(a.get('frozen') or {}).get('ready_date')} 冻结，"
                     "早于该日的判定不给检验结果")
    return line


def render_markdown(as_of: str, *, state_dir=None, freeze: bool = False) -> str:
    """当日两个 tenor 的本地报告。当日无任何行 ⇒ 仍返回非空文字（写明今日无数据与可能原因）。
    `freeze=True`（只有日报钩子经 `write_local_report` 传）⇒ 首次就绪时由这里的 assess 跑检验并冻结。"""
    LG._check_date(as_of)
    lines = [f"# 卖权行权价候选 · {as_of}", "", f"> {DISCLAIMER}", "", "**局限**："]
    lines += [f"- {c}" for c in CAVEATS]
    lines += ["", f"**收益口径**：{YIELD_NOTE}"]
    by_tenor = {}
    for tenor in LG.TENORS:
        try:
            by_tenor[tenor] = LG.rows_for_date(as_of, tenor, state_dir)
        except Exception as exc:  # noqa: BLE001
            by_tenor[tenor] = []
            lines += ["", f"⚠️ {tenor} 账本读取失败：{type(exc).__name__}: {exc}"]
    assessed = {t: _assess_safe(t, state_dir, as_of, freeze=freeze) for t in LG.TENORS}

    if not any(by_tenor.values()):
        lines += ["", "## 今日无数据", "",
                  f"账本里没有 {as_of} 的任何行（月度 0 行 / 周度 0 行；记录 0、不可得 0）。"
                  "可能原因：当日钩子没跑、`run_for_date` 在记录前失败、或当日不是扫描日。",
                  "", "**就绪度**："]
        lines += [f"- {_assess_line(assessed[t])}" for t in LG.TENORS]
        return "\n".join(lines) + "\n"

    for tenor in LG.TENORS:
        rows = by_tenor[tenor]
        lines += ["", f"## {_TENOR_LABEL.get(tenor, tenor)}", "", _assess_line(assessed[tenor]), "",
                  _coverage_line(rows)]
        rec = [r for r in rows if r.get("status") == "recorded"]
        if not rec:
            lines += ["", "今日该档无可用候选。"]
            continue
        for r in rec:
            lines += ["", f"### {r.get('ticker')} · 到期 {r.get('expiry')}（{r.get('dte')} DTE）"
                      f" · 财报 {r.get('earnings_status') or '—'}"
                      + (f"（{r.get('earnings_date')}）" if r.get("earnings_date") else ""), "",
                      _env_line(r), "", _route_line(r), ""]
            lines += _ladder_table(r)
            lines += [""]
            lines += _structure_table(r)
    lines += ["", "> 路由只决定取梯子的哪一档，不产出任何加减分；检验协议见 "
              "`experiments/sell_strike_routing_prereg.md`。"]
    return "\n".join(lines) + "\n"


def report_path(as_of: str, *, state_dir=None) -> Path:
    return LG._state_dir(state_dir) / "reports" / f"sell-strike-{LG._check_date(as_of)}.md"


def write_local_report(as_of: str, *, state_dir=None, freeze: bool = False) -> Path:
    """写 `<state>/reports/sell-strike-<as_of>.md`（原子写），返回路径。

    `freeze=True` ⇒ 预注册检验首次就绪时在这里跑并冻结。**只有日报钩子传 True**——冻结是本协议唯一
    一次不可逆的写，只许一个写者（MCP 工具标了 readOnly，它不写）。"""
    path = report_path(as_of, state_dir=state_dir)
    text = render_markdown(as_of, state_dir=state_dir, freeze=freeze)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# ─────────────────────────────── MCP 现算 / 读账本

def levels_summary(level_map: dict) -> dict:
    """水平地图摘要：每个视图的 zero gamma 状态、majors、totals 与计数（不含逐行权价表）。"""
    out = {"schema_version": level_map.get("schema_version"), "units": level_map.get("units"),
           "sign_convention": level_map.get("sign_convention"), "views": {}}
    for name, v in (level_map.get("views") or {}).items():
        zg = v.get("zero_gamma") or {}
        prof = v.get("profile") or {}
        out["views"][name] = {
            "expiries": v.get("expiries"), "n_contracts": v.get("n_contracts"),
            "zero_gamma": {k: zg.get(k) for k in ("curve_state", "sign_at_spot", "total_at_spot",
                                                  "crossings", "nearest", "nearest_below",
                                                  "nearest_above", "zg_below_pct", "zg_above_pct",
                                                  "n_contracts", "excluded_no_iv")},
            "majors": v.get("majors"), "totals": prof.get("totals"), "counts": prof.get("counts"),
        }
    return out


#: MCP 读账本时，检验尚未冻结就剔除的结算字段——**少递一把刀，不是盲化**（2026-09-24 最终评审）：
#: route flag 与现价是本产品每天的输出、收盘价是公开行情，到期日那天记录的行的 underlying_price
#: 就是到期收盘，两次 MCP 调用照样拼得出每个单位的 pnl_over_credit。代码能保证的只有「不算、不显示效应量」；
#: 从任何出口自己拼结果 = 偷看 = 协议变更（预注册文档 §7）。settle_status 保留：它只说结没结，不带方向。
#: **只有全部 tenor 都冻结才返回**（`_all_unblinded`，2026-09-26 二次检查）：周度到期日多数同时是月度到期日
#: （二次检查统计约 94%），月度先冻结就返回月度行的 expiry_close，等于把仍在盲期的周度单位的结果递了出去。
BLINDED_ROW_FIELDS = ("expiry_close", "expiry_close_date", "expiry_close_source", "settled_on",
                      "settle_attempts", "settle_give_up_reason", "settle_give_up_on", "settle_last_attempt_on")


def _unblinded(a: dict) -> bool:
    """该 tenor 的检验**已冻结且适用**；就绪但未冻结（等日报钩子）、判定失败（status=error）都按未冻结处理。
    单独一个 tenor 冻结**不够**解盲，见 `_all_unblinded`。"""
    return a.get("status") == "ready" and (a.get("frozen") or {}).get("applies") is True


def _all_unblinded(assessed: Dict[str, dict]) -> bool:
    """**每个** tenor 都冻结且适用才返回结算字段（任一 tenor 缺席 / 未冻结 / 判定失败 ⇒ 不返回）。
    同一个 (ticker, 到期日) 往往同时是月度与周度的单位：结算字段按到期日泄露，不按 tenor。"""
    return all(_unblinded(assessed.get(t) or {}) for t in LG.TENORS)


def _row_view(row: dict, *, blind: bool = True) -> dict:
    keep = ("date", "ticker", "tenor", "status", "unavailable_reason", "vintage_date",
            "underlying_price", "underlying_price_source", "payload_last_trade_time", "session_live",
            "iv30", "fetch_counts", "expiry", "dte", "earnings_date", "earnings_status", "env", "route",
            "ladder", "settle_status")
    out = {k: row.get(k) for k in keep}
    if blind:
        out["settlement_blinded"] = True
    else:
        out.update({k: row.get(k) for k in BLINDED_ROW_FIELDS})
    if row.get("status") == "recorded":
        out["structures"] = structures_for_row(row)
    return out


def _assess_brief(a: dict) -> dict:
    return {"status": a.get("status"), "summary": (_assess_line(a)),
            "awaiting_freeze": a.get("awaiting_freeze"),
            "progress": a.get("progress"), "frozen": a.get("frozen"), "error": a.get("error"),
            # 读的是哪个账本、在不在（缺目录 ⇒ undetermined 是「没找到」不是「空」，见 LG.state_dir_status）
            "state_dir": a.get("state_dir")}


def rows_for_ticker(as_of: str, ticker: str, *, state_dir=None) -> dict:
    """读账本：某日某票两个 tenor 的行（MCP 给了 date 时用）。永不抛错、**不写任何文件**
    （assess 走 freeze=False：就绪但未冻结只显示「已就绪，等待日报冻结」）。
    **任一** tenor 检验未冻结 ⇒ 两档行视图都剔除结算字段（`BLINDED_ROW_FIELDS`；那不是盲化，见那里）。"""
    try:
        t = str(ticker).upper().strip()
        out = {"data_available": False, "ticker": t, "as_of": as_of, "source": "ledger",
               "tenors": {}, "assess": {}, "yield_note": YIELD_NOTE,
               "caveats": list(CAVEATS), "disclaimer": DISCLAIMER}
        assessed = {tenor: _assess_safe(tenor, state_dir, None) for tenor in LG.TENORS}
        blind = not _all_unblinded(assessed)
        for tenor in LG.TENORS:
            rows = [r for r in LG.rows_for_date(as_of, tenor, state_dir)
                    if str(r.get("ticker")).upper() == t]
            out["tenors"][tenor] = _row_view(rows[0], blind=blind) if rows else None
            out["assess"][tenor] = _assess_brief(assessed[tenor])
        out["data_available"] = any(v is not None for v in out["tenors"].values())
        if not out["data_available"]:
            # 缺目录 ≠ 当日无行：前者多半是 MCP 进程没拿到 ALPHA_HIVE_HOME（读到了代码目录）
            loc = LG.state_dir_status(state_dir)
            out["reason"] = "no_ledger_rows_for_date" if loc["exists"] else "ledger_state_dir_missing"
            out["state_dir"] = loc
        return out
    except Exception as exc:  # noqa: BLE001
        return {"data_available": False, "ticker": ticker, "as_of": as_of,
                "reason": f"exception:{type(exc).__name__}: {exc}"}


def _invalidate_cached_payload(ticker: str) -> None:
    """现算前清掉该票的进程内 payload 缓存。

    MCP 服务是常驻进程：`_fetch_cboe_payload` 的缓存在同一 vintage 日内可新鲜 4h（`_CACHE_MAX_AGE`），
    10:00 拉过的报价 13:35 再问会原样命中，而 `fetched_at` 记的是调用时刻——3.5 小时前的 bid/ask
    会被当成现价（评审探针实测）。用户盘中按需问一次，多发一个请求是值得的。
    清不掉（模块导入失败）就算了：取链那一步会以同样的原因失败并写进 reason。"""
    try:
        import cboe_options
        cboe_options.invalidate_payload_cache(ticker)
    except Exception:  # noqa: BLE001 - 见 docstring
        pass


def compute_live(ticker: str, *, tenors=("monthly", "weekly"), fetch_fn=None,
                 upcoming_fn=None, state_dir=None) -> dict:
    """MCP 现算：取链（`as_of=None` ⇒ 以 payload vintage 为 as_of）→ 水平地图 → 两档行 + 结构报价。

    **不写任何文件**（只调纯函数 `build_tenor_row`，不调 record / settle；assess 走 freeze=False，
    就绪但未冻结只显示「已就绪，等待日报冻结」）；永不抛错；
    不可得 ⇒ `{"data_available": False, "reason": ...}`。`upcoming_fn` 缺省不查财报（MCP 不为此打网），
    earnings_status 记 "unknown"。assess 状态只读账本（状态目录缺省 `PATHS.sell_strike_state`）。

    缺省取数（`fetch_fn=None`）先清该票的进程内 payload 缓存（`_invalidate_cached_payload`），
    输出带 `payload_last_trade_time`（报价真正是哪一刻的）——判新旧看它，别看 `fetched_at`（调用时刻）。
    """
    t = str(ticker or "").upper().strip()
    try:
        if not t:
            return {"data_available": False, "ticker": t, "reason": "empty_ticker"}
        if fetch_fn is None:
            _invalidate_cached_payload(t)
            fetch_fn = LG._default_fetch
        raw, reason = fetch_fn(t, as_of=None)
        if raw is None:
            return {"data_available": False, "ticker": t, "reason": reason or "fetch_returned_none",
                    "caveats": list(CAVEATS), "disclaimer": DISCLAIMER}
        lm = L.level_map(raw.get("contracts") or [], raw.get("underlying_price"))
        as_of = raw.get("as_of") or raw.get("vintage_date")
        info = None
        if upcoming_fn is not None:
            try:
                info = upcoming_fn(t)
            except Exception:  # noqa: BLE001 - 查不到就是 unknown，行上会写明
                info = None
        out_t = {}
        for tenor in tenors:
            row = LG.build_tenor_row(raw, lm, tenor, as_of=as_of, earnings_info=info, ticker=t)
            # 现算行还没结算，没有可盲的结算字段（blind=True 会多一个名不副实的 settlement_blinded）
            out_t[tenor] = _row_view(row, blind=False)
        return {"data_available": True, "source": "live", "ticker": t, "as_of": as_of,
                "vintage_date": raw.get("vintage_date"),
                "underlying_price": raw.get("underlying_price"),
                "underlying_price_source": raw.get("underlying_price_source"),
                "payload_last_trade_time": raw.get("payload_last_trade_time"),
                "session_live": raw.get("session_live"),
                "iv30": raw.get("iv30"),
                "fetched_at": raw.get("fetched_at"),
                "levels": levels_summary(lm), "tenors": out_t, "yield_note": YIELD_NOTE,
                "assess": {tenor: _assess_brief(_assess_safe(tenor, state_dir, None)) for tenor in tenors},
                "caveats": list(CAVEATS), "disclaimer": DISCLAIMER}
    except Exception as exc:  # noqa: BLE001
        return {"data_available": False, "ticker": t, "reason": f"exception:{type(exc).__name__}: {exc}"}


# ─────────────────────────────── Alpha Bot（本机前端）只读视图（v0.45.387）
#
# 唯一调用方是 `alphabot/service.py`（火墙 ALLOWED_IMPORTERS 显式登记）。本节全部**只读**：
# 不调 record / settle / write_local_report，assess 一律 freeze=False——冻结的唯一写者仍是日报钩子。
# 盲期规则与 MCP 同一份：按日期读账本的行视图，结算字段只在**全部** tenor 冻结后返回（`_all_unblinded`）；
# 历史水平叠其后价格（`env_history`）在那之前整体不给（预注册 §7：历史路由 / 行权价与其后价格不同框）。

#: 展示层逐行权价表只保留现价 ±band 的行权价（远翼对图没有信息，徒增载荷）
DETAIL_BAND_PCT = 0.20
#: 到期盈亏曲线的价格网格点数（现价 ±band）
PAYOFF_GRID_POINTS = 121


def _in_band(strike, S: float, band: float) -> bool:
    k = _num(strike)
    return k is not None and S > 0 and abs(k / S - 1.0) <= band + 1e-12


def _atm_iv(contracts: List[dict], S: float, expiry: Optional[str] = None) -> Optional[float]:
    """离现价最近的行权价上 call / put IV 的均值（有几个取几个）；该到期日没有 IV ⇒ None。"""
    best_k, ivs = None, []
    for c in contracts or []:
        if not isinstance(c, dict) or (expiry is not None and c.get("expiry") != expiry):
            continue
        k, iv = _num(c.get("strike")), _num(c.get("iv"))
        if k is None or iv is None or iv <= 0:
            continue
        if best_k is None or abs(k - S) < abs(best_k - S) - 1e-12:
            best_k, ivs = k, [iv]
        elif abs(k - best_k) < 1e-9:
            ivs.append(iv)
    return (sum(ivs) / len(ivs)) if ivs else None


def _strike_rows(rows: List[dict], S: float, band: float) -> List[dict]:
    return [r for r in rows or [] if _in_band(r.get("strike"), S, band)]


def _detail_view(name: str, contracts: List[dict], level_view: dict, S: float, band: float) -> dict:
    sub = L.view_contracts(contracts, name)
    prof = level_view.get("profile") or {}
    # 成交量口径（GEXBot Classic 的 by-volume）：同一条 strike_profile 公式把 OI 换成当日成交量。
    # 仅展示——成交量不带方向（买 / 卖看不出），当日来回会重复计数。
    vol_prof = L.strike_profile([{**c, "oi": c.get("volume")} for c in sub if isinstance(c, dict)], S)
    zg = level_view.get("zero_gamma") or {}
    return {
        "expiries": level_view.get("expiries"), "n_contracts": level_view.get("n_contracts"),
        "zero_gamma": {k: zg.get(k) for k in ("curve_state", "sign_at_spot", "total_at_spot", "crossings",
                                              "nearest", "nearest_below", "nearest_above",
                                              "zg_below_pct", "zg_above_pct", "n_contracts",
                                              "excluded_no_iv", "excluded_no_oi")},
        "majors": level_view.get("majors"), "totals": prof.get("totals"), "counts": prof.get("counts"),
        "strikes": _strike_rows(prof.get("rows"), S, band),
        "volume_strikes": [{"strike": r["strike"], "net_gex_usd_per_1pct": r["net_gex_usd_per_1pct"],
                            "call_gex_usd_per_1pct": r["call_gex_usd_per_1pct"],
                            "put_gex_usd_per_1pct": r["put_gex_usd_per_1pct"]}
                           for r in _strike_rows(vol_prof.get("rows"), S, band)],
        "volume_totals": {"net_gex_usd_per_1pct": (vol_prof.get("totals") or {}).get("net_gex_usd_per_1pct")},
        # 缺省网格 = zero_gamma_sweep 的缺省网格（测试钉住）：图上的曲线就是路由读的那条
        "curve": L.gex_curve(sub, S),
    }


def _expiry_breakdown(contracts: List[dict], S: float) -> List[dict]:
    """逐到期日：净 GEX / OI / 成交量 / ATM IV（Research 式期限结构）。"""
    by: Dict[str, List[dict]] = {}
    for c in contracts or []:
        if isinstance(c, dict) and c.get("expiry"):
            by.setdefault(str(c["expiry"]), []).append(c)
    out = []
    for exp in sorted(by):
        cs = by[exp]
        dtes = [_num(c.get("dte")) for c in cs if _num(c.get("dte")) is not None]
        tot = (L.strike_profile(cs, S).get("totals") or {})
        out.append({"expiry": exp, "dte": int(min(dtes)) if dtes else None, "n_contracts": len(cs),
                    "net_gex_usd_per_1pct": tot.get("net_gex_usd_per_1pct"),
                    "call_oi": tot.get("call_oi"), "put_oi": tot.get("put_oi"),
                    "volume": sum(_num(c.get("volume")) or 0.0 for c in cs),
                    "atm_iv": _atm_iv(cs, S)})
    return out


def _smile(contracts: List[dict], S: float, expiry: Optional[str], band: float) -> List[dict]:
    """某到期日逐行权价 call / put IV 与 OI（IV 微笑 / 偏度点）。"""
    rows: Dict[float, dict] = {}
    for c in contracts or []:
        if not isinstance(c, dict) or c.get("expiry") != expiry or not _in_band(c.get("strike"), S, band):
            continue
        k = float(c["strike"])
        side = "call" if c.get("cp") == "C" else ("put" if c.get("cp") == "P" else None)
        if side is None:
            continue
        r = rows.setdefault(k, {"strike": k, "call_iv": None, "put_iv": None, "call_oi": 0.0, "put_oi": 0.0})
        iv = _num(c.get("iv"))
        if iv is not None and iv > 0:
            r[f"{side}_iv"] = iv
        r[f"{side}_oi"] += _num(c.get("oi")) or 0.0
    return [rows[k] for k in sorted(rows)]


def _payoffs(row: dict, S: float, band: float) -> dict:
    """六个结构在 route 档与基线档的到期盈亏曲线（每股）——用 `structure_pnl_at_expiry` 算，不在前端另写公式。"""
    ladder = row.get("ladder")
    if not isinstance(ladder, dict) or not S:
        return {}
    grid = [S * (1.0 - band) + i * (2.0 * band * S) / (PAYOFF_GRID_POINTS - 1) for i in range(PAYOFF_GRID_POINTS)]
    rr = route_rungs(row)
    out = {"grid": grid, "route": {}, "base": {}}
    for label, rungs in (("route", rr), ("base", {s: C.BASE_RUNG for s in LG.SIDES})):
        for st in C.STRUCTURES:
            if st in ("short_put", "bull_put_spread"):
                rung = rungs["put"]
            elif st in ("short_call", "bear_call_spread"):
                rung = rungs["call"]
            else:
                rung = dict(rungs) if all(v is not None for v in rungs.values()) else None
            q = C.structure_quote(ladder, st, rung)
            if not q.get("quotable"):
                out[label][st] = {"quotable": False, "reason": q.get("reason")}
                continue
            out[label][st] = {"quotable": True, "legs": q.get("legs"), "credit": q.get("credit"),
                              "breakevens": q.get("breakevens"), "max_loss": q.get("max_loss"),
                              "pnl": [C.structure_pnl_at_expiry(q, p) for p in grid]}
    return out


def _ladder_quotes(row: dict) -> dict:
    """梯子每档单腿卖权的报价（收益率等）——同本地报告 `_ladder_table` 的算法，前端不另写公式。"""
    ladder = row.get("ladder")
    if not isinstance(ladder, dict):
        return {}
    out = {}
    for side in LG.SIDES:
        out[side] = {}
        for d in C.LADDER_DELTAS:
            q = C.structure_quote(ladder, f"short_{side}", d)
            out[side][f"{d:.2f}"] = {k: q.get(k) for k in ("quotable", "reason", "credit", "collateral",
                                                          "yield_raw", "yield_annualized", "breakevens")}
    return out


def compute_live_detail(ticker: str, *, fetch_fn=None, upcoming_fn=None, state_dir=None,
                        band_pct: float = DETAIL_BAND_PCT) -> dict:
    """Alpha Bot 现算：`compute_live` 的全部内容 + 展示用的明细（逐行权价表、gamma 曲线、成交量口径、
    逐到期日拆分、IV 微笑、到期盈亏曲线、1σ 期望波动）。

    与 `compute_live` 同一套保证：**不写任何文件**（只调纯函数）、永不抛错、缺省取数先清该票进程内缓存、
    assess 走 freeze=False；不可得 ⇒ `{"data_available": False, "reason": ...}`。
    """
    t = str(ticker or "").upper().strip()
    try:
        if not t:
            return {"data_available": False, "ticker": t, "reason": "empty_ticker"}
        if fetch_fn is None:
            _invalidate_cached_payload(t)
            fetch_fn = LG._default_fetch
        raw, reason = fetch_fn(t, as_of=None)
        if raw is None:
            return {"data_available": False, "ticker": t, "reason": reason or "fetch_returned_none",
                    "caveats": list(CAVEATS), "disclaimer": DISCLAIMER}
        contracts = raw.get("contracts") or []
        S = _num(raw.get("underlying_price"))
        if S is None or S <= 0:
            return {"data_available": False, "ticker": t, "reason": "underlying_price_unavailable",
                    "caveats": list(CAVEATS), "disclaimer": DISCLAIMER}
        band = float(band_pct)
        lm = L.level_map(contracts, S)
        as_of = raw.get("as_of") or raw.get("vintage_date")
        info = None
        if upcoming_fn is not None:
            try:
                info = upcoming_fn(t)
            except Exception:  # noqa: BLE001 - 查不到就是 unknown，行上会写明
                info = None
        tenors, smile, payoff, em = {}, {}, {}, {}
        for tenor in LG.TENORS:
            row = LG.build_tenor_row(raw, lm, tenor, as_of=as_of, earnings_info=info, ticker=t)
            tenors[tenor] = _row_view(row, blind=False)
            tenors[tenor]["ladder_quotes"] = _ladder_quotes(row)
            exp = row.get("expiry")
            smile[tenor] = _smile(contracts, S, exp, band) if exp else []
            payoff[tenor] = _payoffs(row, S, band) if row.get("status") == "recorded" else {}
            iv = _atm_iv(contracts, S, exp) if exp else None
            move = L.expected_move_1sigma(S, iv, row.get("dte")) if iv is not None else None
            em[tenor] = {"expiry": exp, "dte": row.get("dte"), "atm_iv": iv, "move_1sigma": move,
                         "lo": (S - move) if move is not None else None,
                         "hi": (S + move) if move is not None else None}
        views = {name: _detail_view(name, contracts, v, S, band)
                 for name, v in (lm.get("views") or {}).items()}
        return {"data_available": True, "source": "live", "ticker": t, "as_of": as_of,
                "vintage_date": raw.get("vintage_date"), "underlying_price": S,
                "underlying_price_source": raw.get("underlying_price_source"),
                "payload_last_trade_time": raw.get("payload_last_trade_time"),
                "session_live": raw.get("session_live"), "iv30": raw.get("iv30"),
                "fetched_at": raw.get("fetched_at"), "band_pct": band,
                "units": lm.get("units"), "sign_convention": lm.get("sign_convention"),
                "route_view": C.ROUTE_VIEW, "views": views, "expiries": _expiry_breakdown(contracts, S),
                "tenors": tenors, "smile": smile, "payoff": payoff, "expected_move": em,
                "yield_note": YIELD_NOTE,
                "assess": {tenor: _assess_brief(_assess_safe(tenor, state_dir, None)) for tenor in LG.TENORS},
                "caveats": list(CAVEATS), "disclaimer": DISCLAIMER}
    except Exception as exc:  # noqa: BLE001
        _log.warning("[%s] Alpha Bot 现算失败：%s: %s", t, type(exc).__name__, exc)
        return {"data_available": False, "ticker": t, "reason": f"exception:{type(exc).__name__}: {exc}"}


def _side_brief(row: dict, side: str, rung) -> Optional[dict]:
    if rung is None:
        return None
    slot = ((row.get("ladder") or {}).get(side) or {}).get(C.rung_key(rung)) or {}
    leg = slot.get("short")
    if not isinstance(leg, dict):
        return {"rung": rung, "strike": None, "reasons": slot.get("reasons")}
    q = C.structure_quote(row.get("ladder"), f"short_{side}", rung)
    return {"rung": rung, "strike": leg.get("strike"), "delta": leg.get("delta"),
            "itm_prob": leg.get("itm_prob"), "bid": leg.get("bid"), "spread_pct": leg.get("spread_pct"),
            "quotable": q.get("quotable"), "reason": q.get("reason"),
            "yield_raw": q.get("yield_raw"), "yield_annualized": q.get("yield_annualized")}


def row_brief(row: dict, *, blind: bool) -> dict:
    """总览用的精简行：不带整张梯子，只带路由档 / 基线档的短腿摘要。结算字段按 blind 剔除（同 `_row_view`）。"""
    keep = ("date", "ticker", "tenor", "status", "unavailable_reason", "underlying_price",
            "underlying_price_source", "payload_last_trade_time", "session_live", "iv30", "expiry", "dte",
            "earnings_date", "earnings_status", "env", "route", "settle_status")
    out = {k: row.get(k) for k in keep}
    if blind:
        out["settlement_blinded"] = True
    else:
        out.update({k: row.get(k) for k in BLINDED_ROW_FIELDS})
    if row.get("status") == "recorded":
        rr = route_rungs(row)
        out["route_legs"] = {s: _side_brief(row, s, rr.get(s)) for s in LG.SIDES}
        out["base_legs"] = {s: _side_brief(row, s, C.BASE_RUNG) for s in LG.SIDES}
    return out


def ledger_dates(*, state_dir=None, limit: int = 60) -> List[str]:
    """账本里有行的日期（降序，最多 `limit` 个；只读最近两个月分片）。缺目录 ⇒ []（出口另判 state_dir_status）。"""
    dates = set()
    for tenor in LG.TENORS:
        for p in LG._shard_paths(tenor, state_dir)[-2:]:
            rows, _bad = LG._load_shard(p)
            dates.update(str(r.get("date")) for r in rows if r.get("date"))
    return sorted(dates, reverse=True)[:max(0, int(limit))]


def rows_for_date_view(as_of: str, *, state_dir=None, compact: bool = True) -> dict:
    """某日全部标的两个 tenor 的行视图（总览 / 账本浏览）。永不抛错、不写文件；
    **任一** tenor 未冻结 ⇒ 结算字段剔除（`_all_unblinded`，与 `rows_for_ticker` 同一判据）。"""
    try:
        LG._check_date(as_of)
        assessed = {tenor: _assess_safe(tenor, state_dir, None) for tenor in LG.TENORS}
        blind = not _all_unblinded(assessed)
        view = row_brief if compact else _row_view
        tenors = {tenor: [view(r, blind=blind) for r in LG.rows_for_date(as_of, tenor, state_dir)]
                  for tenor in LG.TENORS}
        return {"data_available": any(tenors.values()), "as_of": as_of, "tenors": tenors,
                "settlement_blinded": blind,
                "assess": {tenor: _assess_brief(assessed[tenor]) for tenor in LG.TENORS},
                "state_dir": LG.state_dir_status(state_dir), "disclaimer": DISCLAIMER}
    except Exception as exc:  # noqa: BLE001
        return {"data_available": False, "as_of": as_of, "reason": f"exception:{type(exc).__name__}: {exc}"}


def assess_overview(*, state_dir=None) -> dict:
    """两个 tenor 的就绪度全貌（只读、freeze=False）：闸门、进度、按档校准（不分 flag，§8）、冻结状态。
    `test`（判定 / p 值）只在该 tenor 已冻结且适用时由 assess 自己给出——这里不另算任何效应量。"""
    out = {"tenors": {}, "prereg": {k: (list(v) if isinstance(v, tuple) else v) for k, v in LG.PREREG.items()},
           "state_dir": LG.state_dir_status(state_dir), "disclaimer": DISCLAIMER}
    assessed = {}
    for tenor in LG.TENORS:
        a = _assess_safe(tenor, state_dir, None)
        assessed[tenor] = a
        keep = ("status", "ready", "need", "gates", "progress", "calibration", "frozen", "awaiting_freeze",
                "error", "note")
        view = {k: a.get(k) for k in keep}
        view["summary"] = _assess_line(a)
        if _unblinded(a) and "test" in a:
            view["test"] = a["test"]
        out["tenors"][tenor] = view
    out["all_unblinded"] = _all_unblinded(assessed)
    return out


def env_history(ticker: str, *, state_dir=None) -> dict:
    """某票历次记录的水平（ZG / Major / 现价处符号 / 路由）按日期排列——**全部 tenor 冻结后才给**。

    冻结前把历史路由 / 水平与其后的价格放进同一张图，就是预注册 §7 说的「从任何出口重建、按 flag 比较」
    的捷径；所以这里在盲期整体返回 locked，不是逐字段剔除。"""
    try:
        t = str(ticker or "").upper().strip()
        assessed = {tenor: _assess_safe(tenor, state_dir, None) for tenor in LG.TENORS}
        if not _all_unblinded(assessed):
            return {"locked": True, "ticker": t,
                    "reason": "预注册检验尚未在全部 tenor 冻结：历史水平叠其后价格属于盲期内不提供的视图（预注册 §7）"}
        series = {}
        for tenor in LG.TENORS:
            series[tenor] = [
                {"date": r.get("date"), "underlying_price": r.get("underlying_price"),
                 "expiry": r.get("expiry"), "expiry_close": r.get("expiry_close"),
                 "zg_nearest": (r.get("env") or {}).get("zg_nearest"),
                 "sign_at_spot": (r.get("env") or {}).get("sign_at_spot"),
                 "net_major_pos_strike": (r.get("env") or {}).get("net_major_pos_strike"),
                 "net_major_neg_strike": (r.get("env") or {}).get("net_major_neg_strike"),
                 "route": {k: (r.get("route") or {}).get(k) for k in ("put", "call", "flag_put", "flag_call")}}
                for r in LG.load_rows(tenor, state_dir)
                if str(r.get("ticker")).upper() == t and r.get("status") == "recorded"]
        return {"locked": False, "ticker": t, "series": series}
    except Exception as exc:  # noqa: BLE001
        return {"locked": True, "ticker": ticker, "reason": f"exception:{type(exc).__name__}: {exc}"}
