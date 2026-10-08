#!/usr/bin/env python3
"""财报跨式 × GEX 政体：预注册检验（v0.45.424）。协议全文：experiments/straddle_gex_prereg.md。

问的唯一问题
------------
信号日做市商处在**正 gamma** 政体的财报事件，实际事件波动相对期权隐含的事件波动，是否比**负 gamma** 政体的更小？
（成立 ⇒ 卖跨式在正 gamma 下更有利；这是「GEX 能不能当跨式过滤器」的必要条件，不是充分条件。）

本模块做三件事，全部**只读**输入、唯一的写是冻结结果：
  · `units(rows)`：从 `earnings_signals.jsonl` 的行里按协议构造独立单位（不看结果就能决定的过滤全在这里）；
  · `progress(rows)`：就绪度——**只有计数**（单位数、各政体数、信息块数），**不算任何效应量**；
  · `run_once()`：就绪后跑**一次**分块置换检验并冻结到 `<数据根>/options_paper_state/straddle_gex_prereg_result.json`
    （`os.link` 先到者赢，不覆盖）。之后只读那份文件、不重算。未就绪时拒绝运行。

盲期：冻结之前，任何代码路径都不计算、不返回两组的结果差（`BLINDED_KEYS` 不出现在 `progress` 的返回里，测试钉住）。
Alpha Bot「跨式账本」页只显示 `progress`。自己把信号行的 `gex_ctx` 和其后的实际波动拼起来按政体比较 = 偷看 = 协议变更。
"""
from __future__ import annotations

import json
import math
import os
import random
import tempfile
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hive_logger import PATHS, get_logger

_log = get_logger("straddle_gex_prereg")

PREREG = {
    "version": 2,                      # 修订 1（v0.45.428，登记后、尚无任何记录）：GEX 须是收盘后记录的
    "registered": "2026-10-07",
    "doc": "experiments/straddle_gex_prereg.md",
    "unit": "ticker+earnings_date",
    "regimes": ("positive_gex", "negative_gex"),
    "gex_ctx_schema": 2,
    "realized_floor_pct": 0.01,        # ln 的下限：实际波动 0.00% 时按 0.01% 算（只防 ln(0)，不改排序）
    "block": "earnings_iso_week",
    "min_units": 60,                   # 有效单位（带可用 GEX 且已结算）总数
    "min_per_group": 20,               # 信息块内每个政体的单位数
    "min_informative_blocks": 8,       # 两种政体都出现的财报周数
    "alpha": 0.05,                     # 单个检验、单侧
    "n_perm": 5000,
    "seed": 20261007,
}
#: 冻结前不许出现在任何返回值里的键（效应量 / p 值 / 判定）
BLINDED_KEYS = frozenset({"observed", "p_value", "decision", "group_means", "null_quantiles"})
RESULT_NAME = "straddle_gex_prereg_result.json"


def public_constants() -> dict:
    """给页面 / 帮助页显示的协议常量（规则只在这里维护一份）。"""
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in PREREG.items()}


def _num(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _ctx_problem(ctx) -> Optional[str]:
    """影子记录为什么不能用；能用返回 None。逐条判、各报各的原因（v0.45.428：此前一律报成 captured_on≠as_of）。
    「当天 + 收盘后 + 可得 + 结构版本对」与 `earnings_vol_signal._usable_capture` 同一判据（测试钉住），两处一起改。"""
    if not isinstance(ctx, dict):
        return "no_gex_ctx"
    if ctx.get("schema_version") != PREREG["gex_ctx_schema"]:
        return f"schema:{ctx.get('schema_version')}"
    if ctx.get("available") is not True:
        return f"unavailable:{ctx.get('reason') or '—'}"
    if ctx.get("regime") not in PREREG["regimes"]:
        return f"regime:{ctx.get('regime')}"
    if not ctx.get("captured_on") or ctx.get("captured_on") != ctx.get("as_of"):
        return "captured_on≠as_of"
    if ctx.get("session_live") is not False:
        return "intraday_capture" if ctx.get("session_live") else "session_unknown"
    return None


def _ctx_ok(ctx) -> bool:
    return _ctx_problem(ctx) is None


def _block(earnings_date: str) -> Optional[str]:
    try:
        y, w, _ = date.fromisoformat(str(earnings_date)[:10]).isocalendar()
    except (TypeError, ValueError):
        return None
    return f"{y}-W{w:02d}"


def units(rows: List[dict]) -> List[dict]:
    """协议 §3：每个 (ticker, earnings_date) 一个单位；代表行 = 按信号日期最早、合格且隐含事件波动 > 0 的那一行。
    **先定代表行、再看它的 GEX**：代表行的影子记录不可用 ⇒ 整个单位不进检验（不往后找一条可用的顶上）。
    返回每个单位的事前字段 + `settled` 与 `y`（结果）——调用方在冻结前只许数个数，不许按政体汇总 `y`。"""
    by: Dict[Tuple[str, str], List[dict]] = {}
    for r in rows or []:
        if r.get("eligible") and r.get("earnings_date") and r.get("ticker"):
            by.setdefault((str(r["ticker"]), str(r["earnings_date"])[:10]), []).append(r)
    out = []
    for (tk, ed), rs in sorted(by.items()):
        rs.sort(key=lambda r: str(r.get("as_of") or ""))
        rep = next((r for r in rs if (_num(r.get("implied_event_move_pct")) or 0.0) > 0), None)
        if rep is None:
            out.append({"ticker": tk, "earnings_date": ed, "status": "no_usable_signal"})
            continue
        ctx = rep.get("gex_ctx")
        problem = _ctx_problem(ctx)
        u = {"ticker": tk, "earnings_date": ed, "as_of": rep.get("as_of"), "block": _block(ed),
             "regime": ctx.get("regime") if problem is None else None,
             # no_gex_ctx：v0.45.424 之前的行 / 未接线；其余写清是哪一条不满足
             "status": "ok" if problem is None else (problem if problem == "no_gex_ctx" else f"gex_unusable:{problem}")}
        realized = None
        for r in rs:                                       # settle 把同一事件的所有合格行都回填成同一个实际波动
            realized = _num(r.get("realized_abs_move_pct"))
            if realized is not None:
                break
        implied = _num(rep.get("implied_event_move_pct"))
        u["settled"] = realized is not None
        u["y"] = (math.log(max(realized, PREREG["realized_floor_pct"]) / implied)
                  if (realized is not None and implied) else None)
        out.append(u)
    return out


def _informative(us: List[dict]) -> Tuple[List[dict], set]:
    """信息块 = 两种政体都出现的财报周；只有它们对分块置换有贡献。"""
    seen: Dict[str, set] = {}
    for u in us:
        seen.setdefault(u["block"], set()).add(u["regime"])
    blocks = {b for b, rg in seen.items() if b and len(rg) == 2}
    return [u for u in us if u["block"] in blocks], blocks


def progress(rows: List[dict], *, root=None) -> dict:
    """就绪度：**只有计数**。不返回 `y`、不按政体汇总结果（盲期）。`root` = 数据根（冻结文件在那里）。"""
    us = units(rows)
    eligible = [u for u in us if u.get("status") == "ok" and u.get("settled")]
    inf, blocks = _informative(eligible)
    by_status: Dict[str, int] = {}
    for u in us:
        by_status[u["status"]] = by_status.get(u["status"], 0) + 1
    pos = sum(1 for u in inf if u["regime"] == "positive_gex")
    neg = sum(1 for u in inf if u["regime"] == "negative_gex")
    need = {k: PREREG[k] for k in ("min_units", "min_per_group", "min_informative_blocks")}
    ready = (len(eligible) >= need["min_units"] and pos >= need["min_per_group"] and neg >= need["min_per_group"]
             and len(blocks) >= need["min_informative_blocks"])
    frozen = read_result(root)
    return {
        "available": True, "prereg_version": PREREG["version"], "ready": ready, "frozen": frozen is not None,
        "n_events": len(us), "by_status": by_status,
        "n_with_gex": sum(1 for u in us if u.get("status") == "ok"),
        "n_with_gex_by_regime": {rg: sum(1 for u in us if u.get("status") == "ok" and u["regime"] == rg)
                                 for rg in PREREG["regimes"]},
        "n_eligible_settled": len(eligible),
        "n_informative": {"positive_gex": pos, "negative_gex": neg, "blocks": len(blocks)},
        "need": need,
    }


# ─────────────────────────────── 检验（就绪后只跑一次）

def blocked_permutation_test(us: List[dict], *, n_perm: int, seed: int) -> dict:
    """T = mean(y | positive) − mean(y | negative)；H1：T < 0。只在同一财报周内打乱政体标签（保留每周各政体个数）。
    p = (1 + #{T_null ≤ T_obs}) / (1 + n_perm)，并列算「≤」（保守）。只用信息块。"""
    inf, _blocks = _informative([u for u in us if u.get("y") is not None and u.get("regime")])
    pos = [u["y"] for u in inf if u["regime"] == "positive_gex"]
    neg = [u["y"] for u in inf if u["regime"] == "negative_gex"]
    if not pos or not neg:
        return {"testable": False, "reason": "empty_group"}
    obs = sum(pos) / len(pos) - sum(neg) / len(neg)
    by_block: Dict[str, List[dict]] = {}
    for u in inf:
        by_block.setdefault(u["block"], []).append(u)
    rng = random.Random(seed)
    order = sorted(by_block)
    n_le = 0
    for _ in range(n_perm):
        ps, ns = [], []
        for b in order:
            grp = by_block[b]
            labels = [u["regime"] for u in grp]
            rng.shuffle(labels)
            for u, lab in zip(grp, labels):
                (ps if lab == "positive_gex" else ns).append(u["y"])
        if sum(ps) / len(ps) - sum(ns) / len(ns) <= obs:
            n_le += 1
    p = (1 + n_le) / (1 + n_perm)
    return {"testable": True, "observed": obs, "p_value": p, "n_positive": len(pos), "n_negative": len(neg),
            "decision": "reject_h0" if p <= PREREG["alpha"] else "fail_to_reject_h0"}


def _result_path(root=None) -> Path:
    """缺省 = `PATHS.straddle_prereg_result`（调用时求值）；`root` 只给测试 / 指定数据根用。"""
    if root is None:
        return PATHS.straddle_prereg_result
    return Path(root) / "options_paper_state" / RESULT_NAME


def read_result(root=None) -> Optional[dict]:
    p = _result_path(root)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _log.error("冻结结果文件读不了（不重算、不覆盖，须人工处理）：%s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


def run_once(rows: List[dict], *, root=None, today: Optional[str] = None) -> dict:
    """就绪后跑一次并冻结；已冻结 ⇒ 原样返回那份；未就绪 ⇒ 拒绝（不算任何效应量）。"""
    done = read_result(root)
    if done is not None:
        return {**done, "already_frozen": True}
    pr = progress(rows, root=root)
    if not pr["ready"]:
        return {"ran": False, "reason": "not_ready", "progress": pr}
    us = [u for u in units(rows) if u.get("status") == "ok" and u.get("settled")]
    res = blocked_permutation_test(us, n_perm=PREREG["n_perm"], seed=PREREG["seed"])
    out = {"prereg_version": PREREG["version"], "ready_date": today, "progress": pr, **res,
           "unit_keys": [[u["ticker"], u["earnings_date"], u["as_of"]] for u in us]}
    path = _result_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=RESULT_NAME + ".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        os.link(tmp, path)                        # 先到者赢：已存在就抛 FileExistsError，不覆盖
    except FileExistsError:
        return {**(read_result(root) or {}), "already_frozen": True}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    _log.info("跨式 GEX 预注册检验已冻结：%s", {k: out[k] for k in ("decision", "p_value") if k in out})
    return out


def _load_rows(root=None) -> List[dict]:
    home = Path(root) if root is not None else PATHS.home
    p = home / "options_paper_state" / "earnings_signals.jsonl"
    rows = []
    if p.is_file():
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(ln) if ln.strip() else None
            except ValueError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="财报跨式 × GEX 政体预注册检验：缺省只看就绪度；--run-once 就绪后跑一次并冻结")
    ap.add_argument("--run-once", action="store_true")
    args = ap.parse_args(argv)
    rows = _load_rows()
    if not args.run_once:
        print(json.dumps(progress(rows), ensure_ascii=False, indent=1))
        return 0
    from hive_logger import pdt_today
    res = run_once(rows, today=pdt_today())
    print(json.dumps({k: v for k, v in res.items() if k != "unit_keys"}, ensure_ascii=False, indent=1))
    return 0 if res.get("ran", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
