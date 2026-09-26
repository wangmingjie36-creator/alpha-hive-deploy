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
    MCP 两条路径都走缺省 False（就绪但未冻结 ⇒「已就绪，等待日报冻结」，不写文件）。"""
    try:
        return LG.assess(tenor, state_dir=state_dir, as_of=as_of, freeze=freeze)
    except Exception as exc:  # noqa: BLE001 - 报告不因 assess 失败而空白，但原因要写出来
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
BLINDED_ROW_FIELDS = ("expiry_close", "expiry_close_date", "expiry_close_source", "settled_on",
                      "settle_attempts", "settle_give_up_reason", "settle_give_up_on")


def _unblinded(a: dict) -> bool:
    """该 tenor 的检验**已冻结且适用**才返回结算字段；就绪但未冻结（等日报钩子）、判定失败（status=error）
    都按未冻结处理。"""
    return a.get("status") == "ready" and (a.get("frozen") or {}).get("applies") is True


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
            "progress": a.get("progress"), "frozen": a.get("frozen"), "error": a.get("error")}


def rows_for_ticker(as_of: str, ticker: str, *, state_dir=None) -> dict:
    """读账本：某日某票两个 tenor 的行（MCP 给了 date 时用）。永不抛错、**不写任何文件**
    （assess 走 freeze=False：就绪但未冻结只显示「已就绪，等待日报冻结」）。
    该 tenor 检验未冻结 ⇒ 行视图剔除结算字段（`BLINDED_ROW_FIELDS`；那不是盲化，见那里）。"""
    try:
        t = str(ticker).upper().strip()
        out = {"data_available": False, "ticker": t, "as_of": as_of, "source": "ledger",
               "tenors": {}, "assess": {}, "yield_note": YIELD_NOTE,
               "caveats": list(CAVEATS), "disclaimer": DISCLAIMER}
        for tenor in LG.TENORS:
            a = _assess_safe(tenor, state_dir, None)
            rows = [r for r in LG.rows_for_date(as_of, tenor, state_dir)
                    if str(r.get("ticker")).upper() == t]
            out["tenors"][tenor] = _row_view(rows[0], blind=not _unblinded(a)) if rows else None
            out["assess"][tenor] = _assess_brief(a)
        out["data_available"] = any(v is not None for v in out["tenors"].values())
        if not out["data_available"]:
            out["reason"] = "no_ledger_rows_for_date"
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
