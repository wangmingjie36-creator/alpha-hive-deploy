#!/usr/bin/env python3
"""F&G 组合层敞口控制门 前瞻检验（预注册，v0.45.262，2026-09-15）

问题
----
`BuzzBeeWhisper` 把 F&G 当逐标的情绪信号用（当天全池同一常数加进逐标的连续分数，
再过类别阈值判方向）已实测证据不支持——顺周期（生产现状）明显是"顺周期/去掉/逆周期"
三种处理里最差的，去掉和逆周期几乎没有差距，且全部差距远未过检验（15 周，
本仓功效标准要 ~25 周，见 [[alpha-hive-power-analysis]]）。更根本的问题是结构性的：
F&G 是当天全池唯一一个常数，加到逐标的连续分数上不改变横截面排序（秩相关对同一常数
平移不变），它唯一的效应来自"谁卡在类别阈值附近被推过还是推不过"——本质是往阈值分类
里注入噪音，跟顺/逆周期无关。CNN 官方把 Fear & Greed Index 设计成大盘/组合择时工具，
不是逐标的选股信号——用错了层次。完整推导见 auto-memory `alpha-hive-fear-greed-dead-wire.md`。

`paper_portfolio.CONFIG["fg_exposure_gate"]`（v0.45.262 新增，默认 `enabled=False`）把
F&G 挪到它真正适用的层次：组合层的仓位敞口控制——极度贪婪日新开多头减仓、极度恐惧日
新开空头减仓（只收紧顺势方向，不给逆势方向加仓），完全不触碰任何标的的评分或方向判定。

为什么是前瞻而不是直接改
------------------------
这个假设至今**一次都没有真实运行过**（`enabled` 从落地起就是 `False`），没有任何样本内
证据可用——不像共振加成那次还有半年生产数据可供事后分析（v0.45.242）。唯一诚实的验证
方式是让它先在沙盒里跑一段真实前瞻期，而不是凭理论推导直接把 `enabled` 改成 `True`。

────────────────────────────────────────────────────────────────────
预注册（2026-09-15 写死，早于任何前瞻样本；改动须在 CHANGELOG 标「事后」并写理由）
────────────────────────────────────────────────────────────────────
样本  前瞻窗口 = [FORWARD_START, 今天)（`FORWARD_START` = 登记次日 2026-09-16）内，
      `paper_portfolio` 真实产出过 `report_snapshots/*.json` 的交易日
      （`paper_portfolio._all_snapshot_dates()`）。
变体  A（baseline）= `run_replay({}, ...)`，即当前生产默认配置（`fg_exposure_gate.enabled=False`）；
      B（treatment）= `run_replay(FG_GATE_TEST_CONFIG, ...)`，`enabled=True`，其余参数逐字节等于
      `paper_portfolio.CONFIG["fg_exposure_gate"]` 落地时写的默认值（`extreme_fear=25` /
      `extreme_greed=75` / `long_size_mult=0.5` / `short_size_mult=0.5`）——测的是"把默认参数
      打开会怎样"，不是另找一组参数调优（调参本身需要更多证据支撑，不在这次范围内）。
      两者都用**真实** `paper_portfolio.run_replay`，各自独立沙盒 state_dir，不碰生产状态。
自证  （前提，不满足即"无法判定" exit 3）A（baseline）重放出的"窗口内新开仓位"集合
      （`(ticker, entry_date, direction, round(size_usd,2))` 四元组）与生产**实际记录**的
      `paper_portfolio_state/`（`closed_trades.jsonl` ∪ 仍在场的 `positions.jsonl`，
      entry_date 落在窗口内的那些）重合比例 >= SELFPROOF_MIN_RATE=0.95。
      ⇒ 若评分链、`paper_portfolio.CONFIG` 其它参数、或本身的重放机制在期间被改动，
      这里会红，而不是静默算出一个没意义的数。
统计量 两个变体各自的 `equity_curve.jsonl` 按 ISO 周取"本周首个交易日 NAV"，相邻两个
      取值点间的百分比变化即该周收益率；ΔNAV_pct = B周收益率 − A周收益率。
      非极端 F&G 日两个变体的仓位应逐笔相同 ⇒ 多数周 ΔNAV_pct=0——这是**预期正常**，
      不是异常，会稀释均值、需要更多周才能检出效应，不代表实现有问题。
      对周序列做单侧 t 检验（H1：均值 > 0，即敞口门让组合更好）。不用横截面 IC——
      敞口门改的是仓位大小、不改排序，横截面秩相关在这里没有意义。
检视  成组序贯两次，按**时间顺序最先攒到的**前 N 个合格周计算：
        中期  N=15  单侧 p < 0.02   → 确认
        终期  N=30  单侧 p < 0.045  → 确认；否则 → 未确认（结案）
      两次合计单侧 α ≈ 0.06。**N 高于共振加成那次（10/20）是明确写出的估计，不是算出来的**：
      极度恐惧/贪婪日历史约占 15% 交易日（CNN 141 天 21 天 <25，[[alpha-hive-fear-greed-dead-wire]]），
      远比共振加成"天天都有效应"稀疏，多数周 delta=0 会拉长检出所需的周数——没有做严格功效分析
      （本身就没有先验效应量可供估计），这里选的是"比共振加成更保守"的直觉调整，如实记录，
      不假装精确。
盲化  未到检视点时只报进度（合格周数、自证率），**不输出任何效应量**；中期未过线同样
      只报"未过中期界，继续"。
结论  确认 → 评估把 `fg_exposure_gate.enabled` 改 `True`（这会改变纸面组合的真实交易，
      需要 paper_portfolio 自己的 `run_replay` 对比新旧参数、用户拍板，不是本脚本自动执行）。
      未确认 → 保持默认关闭，登记结案，勿再拿这轮样本内证据提议打开。
附带  （不参与判定）敞口门实际调整过的仓位笔数与调整方向分布、这些笔已平仓部分的
      已实现盈亏对比（仅描述，非统计检验）。

样本内复核：`--insample` 在 `FORWARD_START` 之前的历史窗口上跑同一套 A/B 重放
（此时 A 的自证对象是"整个历史"而非"前瞻窗口"）。**它就是生成假设的那份数据，
只供复核报告数字与检查代码机制，不能拿来确认**——F&G 敞口门此前从未真正运行过，
这不像共振加成还有"半年生产分数"可供样本内探索，这里的 `--insample` 更多是自证脚本
本身没写错，而不是提前偷看效应。

用法
----
    /usr/local/bin/python3 experiments/fg_exposure_gate_forward_test.py            # 前瞻（默认）
    /usr/local/bin/python3 experiments/fg_exposure_gate_forward_test.py --json
    /usr/local/bin/python3 experiments/fg_exposure_gate_forward_test.py --insample

承载物：`ic_rerun_readiness.py` 每周被只读诊断任务调用，其摘要行会带上本检验的进度
（`status_line`），攒够周数时显著提示来跑本脚本。

退出码：0 = 已到检视点并给出结论 / 1 = 未就绪（正常）/ 3 = 无法判定
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 代码锚点：import 仓内模块

from ic_diagnostics import basic_stats, normal_two_sided_p  # noqa: E402

# ── 预注册常量（改动 = 事后，须在 CHANGELOG 说明；测试钉住）──────────────────
FORWARD_START = "2026-09-16"
LOOKS: Tuple[Tuple[int, float, str], ...] = ((15, 0.02, "中期"), (30, 0.045, "终期"))
SELFPROOF_MIN_RATE = 0.95
FG_GATE_TEST_CONFIG: Dict = {
    "fg_exposure_gate": {
        "enabled": True,
        "extreme_fear": 25,
        "extreme_greed": 75,
        "long_size_mult": 0.5,
        "short_size_mult": 0.5,
    },
}

STALE_DAYS = 21  # 登记后这么多天仍无前瞻样本 ⇒ 不是"还在攒"，是扫描停了或路径错了


# ── 重放（全部调真实 paper_portfolio 代码）─────────────────────────────────────
def _replay_variant(config_overrides: Dict, state_dir: Path, dates: List[str]) -> Dict:
    """跑一个变体，返回 `run_replay` 原样结果 + 该沙盒里最终仍持有的仓位。"""
    import paper_portfolio as pp
    result = pp.run_replay(config_overrides, state_dir, dates=dates)
    result["open_positions"] = pp._load_jsonl(state_dir / "positions.jsonl")
    return result


def _entry_key(t: Dict) -> Tuple[str, str, str, float]:
    """建仓市值：`Position`（仍在场）直接有 `size_usd`；`ClosedTrade`（已平仓）没有这个字段，
    只能用 `shares × entry_price` 反推——两者对同一笔仓位应给出同一个数（下单时 `shares =
    size_usd / entry_price` 就是这么算的），敞口门测的正是这个建仓时刻的市值差异。"""
    size = t["size_usd"] if "size_usd" in t else t["shares"] * t["entry_price"]
    return (t["ticker"], t["entry_date"], t["direction"], round(float(size), 2))


def _entries_in_window(closed: List[Dict], open_positions: List[Dict],
                       since: str, before: str) -> set:
    """(ticker, entry_date, direction, size_usd) 四元组集合——窗口内所有"曾经开过"的仓位，
    不管现在是已平仓还是还在场（两个来源合并，覆盖"平仓时点晚于检查时点"这一类）。"""
    out = set()
    for t in closed + open_positions:
        ed = t.get("entry_date", "")
        if since <= ed < before:
            out.add(_entry_key(t))
    return out


def _real_recorded_entries(since: str, before: str) -> set:
    """生产**实际记录**的 `paper_portfolio_state/`——自证的比对基准，只读，不碰。"""
    import paper_portfolio as pp
    closed = pp._load_jsonl(pp.CLOSED_FILE)
    open_positions = pp._load_jsonl(pp.POSITIONS_FILE)
    return _entries_in_window(closed, open_positions, since, before)


def _weekly_nav_returns(equity: List[Dict]) -> Dict[Tuple[int, int], float]:
    """NAV 序列 → 每 ISO 周的收益率（%）：本周首个交易日 NAV 相对上一个"首个交易日"取值点
    的变化。相邻两个取值点之间可能不足严格 7 天（数据有缺口时），这是近似，不强求精确周边界。
    """
    by_day = {e["date"]: e["nav"] for e in equity if e.get("nav") is not None}
    first_of_week: "collections.OrderedDict" = collections.OrderedDict()
    for d in sorted(by_day):
        wk = dt.date.fromisoformat(d).isocalendar()[:2]
        if wk not in first_of_week:
            first_of_week[wk] = by_day[d]
    weeks = list(first_of_week)
    out = {}
    for i in range(1, len(weeks)):
        prev, cur = first_of_week[weeks[i - 1]], first_of_week[weeks[i]]
        if prev and prev > 0:
            out[weeks[i]] = (cur / prev - 1) * 100.0
    return out


# ── 统计（复用 ic_diagnostics 的规范实现，只补一层单侧转换）────────────────────
def one_sided_greater_than_zero(values: List[float]) -> Dict:
    """H1: 均值 > 0。全部相同（含全 0——多数周没有极端 F&G 触发时的正常形态）时
    `se=0`，`ic_diagnostics.basic_stats` 给的 t 是 nan，这里按符号显式判 p。"""
    m, se, t, n = basic_stats(values)
    if n < 2:
        return {"n": n, "mean": m, "t": None, "p": 1.0}
    if se == 0 or not (t == t):  # se==0（全同值）或 t 非有限
        return {"n": n, "mean": m, "t": None, "p": 0.0 if m > 0 else 1.0}
    p_two = normal_two_sided_p(t)
    return {"n": n, "mean": m, "t": t, "p": (p_two / 2 if t > 0 else 1 - p_two / 2)}


def weekly_deltas(a_equity: List[Dict], b_equity: List[Dict]) -> List[Dict]:
    ra, rb = _weekly_nav_returns(a_equity), _weekly_nav_returns(b_equity)
    common = sorted(set(ra) & set(rb))
    return [{"week": f"{wk[0]}-W{wk[1]:02d}", "ret_a": ra[wk], "ret_b": rb[wk],
             "delta": rb[wk] - ra[wk]} for wk in common]


def decide(weeks: List[Dict], looks=LOOKS) -> Dict:
    """成组序贯判定。**未到结论时返回值里不含任何效应量**（盲化在数据结构上，不在打印上）。"""
    k = len(weeks)
    for n_req, alpha, label in looks:
        if k < n_req:
            break
        st = one_sided_greater_than_zero([w["delta"] for w in weeks[:n_req]])
        if st["p"] < alpha:
            return {"status": "confirmed", "look": label, "alpha": alpha, "stats": st,
                    "weeks_used": [w["week"] for w in weeks[:n_req]]}
        if (n_req, alpha, label) == looks[-1]:
            return {"status": "not_confirmed", "look": label, "alpha": alpha, "stats": st,
                    "weeks_used": [w["week"] for w in weeks[:n_req]]}
    passed = [lab for n_req, _, lab in looks if k >= n_req]
    nxt = next(n_req for n_req, _, _ in looks if k < n_req)
    return {"status": "not_ready", "weeks": k, "next_look_at": nxt,
            "looks_passed_without_verdict": passed}


# ── 编排 ──────────────────────────────────────────────────────────────────────
def _adjusted_trades_summary(a_closed: List[Dict], b_closed: List[Dict],
                             since: str, before: str) -> Dict:
    """附带统计（不参与判定）：哪些笔的仓位大小被敞口门改了，改完已平仓的那部分盈亏对比。"""
    a_by_key = {(t["ticker"], t["entry_date"]): t for t in a_closed if since <= t["entry_date"] < before}
    b_by_key = {(t["ticker"], t["entry_date"]): t for t in b_closed if since <= t["entry_date"] < before}
    # "被调整"的判据：同一笔（ticker, entry_date）的建仓市值（shares × entry_price）在
    # 两个变体里不同——直接比 shares 也行（同一 entry_price 下等价），用市值更直观。
    adjusted = [(k, a_by_key[k], b_by_key[k]) for k in a_by_key
               if k in b_by_key and round(a_by_key[k]["shares"] * a_by_key[k]["entry_price"], 2)
               != round(b_by_key[k]["shares"] * b_by_key[k]["entry_price"], 2)]
    return {
        "adjusted_closed_trades": len(adjusted),
        "pnl_sum_a": round(sum(t[1]["pnl_usd"] for t in adjusted), 2),
        "pnl_sum_b": round(sum(t[2]["pnl_usd"] for t in adjusted), 2),
    }


def evaluate(dates: List[str], since: str, before: str, sandbox_root: Path,
            *, insample: bool = False) -> Dict:
    a_dir, b_dir = sandbox_root / "A_baseline", sandbox_root / "B_treatment"
    a = _replay_variant({}, a_dir, dates)
    b = _replay_variant(FG_GATE_TEST_CONFIG, b_dir, dates)

    out: Dict = {"mode": "insample" if insample else "forward", "n_dates": len(dates)}

    if insample:
        # 样本内没有"生产实际记录"可比——自证换成检查机制本身没写错：
        # A（enabled=False）与"完全不传 fg_exposure_gate 覆盖"必须逐笔相同
        # （这正是 Part 1 落地时的核心前提，这里顺带在真实前瞻脚本路径上再核一遍）。
        a_entries = _entries_in_window(a["closed"], a["open_positions"], since, before)
        a_again = _replay_variant({"fg_exposure_gate": {"enabled": False}}, sandbox_root / "A_check", dates)
        a_again_entries = _entries_in_window(a_again["closed"], a_again["open_positions"], since, before)
        mechanism_ok = a_entries == a_again_entries
        out["mechanism_selfcheck_ok"] = mechanism_ok
        if not mechanism_ok:
            return {**out, "status": "cannot_judge",
                    "reason": "机制自检失败：baseline(默认配置) 与显式 enabled=False 重放结果不一致，"
                             "重放机制本身有问题，不是效应"}
    else:
        real_entries = _real_recorded_entries(since, before)
        a_entries = _entries_in_window(a["closed"], a["open_positions"], since, before)
        reproduced = len(real_entries & a_entries)
        total = len(real_entries)
        rate = reproduced / total if total else None
        out["selfproof"] = {"real_entries": total, "reproduced": reproduced}
        out["selfproof_rate"] = rate
        if total == 0:
            return {**out, "status": "not_ready", "weeks": 0, "next_look_at": LOOKS[0][0],
                    "looks_passed_without_verdict": [],
                    "reason": f"窗口内生产还没有任何真实开仓记录（{since}~{before}）"}
        if rate is None or rate < SELFPROOF_MIN_RATE:
            return {**out, "status": "cannot_judge",
                    "reason": (f"A（baseline）重放复现生产记录仅 {reproduced}/{total}"
                              f"（< {SELFPROOF_MIN_RATE:.0%}）——评分链/组合层配置已被改动，"
                              "或重放机制本身有问题，本检验前提不成立")}

    weeks = weekly_deltas(a["equity"], b["equity"])
    out["weeks_available"] = len(weeks)
    out["adjusted_trades"] = _adjusted_trades_summary(a["closed"], b["closed"], since, before)

    if insample:  # 样本内不盲化、不走检视：它是生成假设/自检机制的数据
        return {**out, "status": "insample",
                "stats_all_weeks": one_sided_greater_than_zero([w["delta"] for w in weeks]) if weeks else None,
                "weekly_deltas": weeks}

    verdict = decide(weeks)
    out.update(verdict)
    return out


def _snapshot_dates_in_window(since: str, before: str) -> List[str]:
    import paper_portfolio as pp
    return sorted(d for d in pp._all_snapshot_dates()
                  if pp.CONFIG["bootstrap_date"] <= d and since <= d < before)


def run(sandbox_root: Optional[Path] = None, insample: bool = False,
       today: Optional[str] = None) -> Dict:
    import paper_portfolio as pp

    if not any(pp.SNAPSHOT_DIR.glob("analysis-*-ml-*.json")) and not any(pp.SNAPSHOT_DIR.glob("*_*.json")):
        return {"status": "cannot_judge", "reason": f"{pp.SNAPSHOT_DIR} 下没有任何历史快照（路径错了？）"}

    today_d = today or dt.date.today().isoformat()
    since, before = (pp.CONFIG["bootstrap_date"], FORWARD_START) if insample else (FORWARD_START, today_d)
    dates = _snapshot_dates_in_window(since, before)

    if not insample and not dates:
        days = (dt.date.fromisoformat(today_d) - dt.date.fromisoformat(FORWARD_START)).days
        res = {"status": "not_ready", "weeks": 0, "next_look_at": LOOKS[0][0],
              "looks_passed_without_verdict": [], "mode": "forward", "n_dates": 0,
              "reason": f"尚无 {FORWARD_START} 起的前瞻样本"}
        if days > STALE_DAYS:
            res["stale"] = True
            res["reason"] = f"登记后 {days} 天仍无前瞻样本 —— 扫描停了，或快照不在 {pp.SNAPSHOT_DIR}"
        return res

    import tempfile
    with tempfile.TemporaryDirectory(prefix="fg_gate_fwd_") as tmp:
        return evaluate(dates, since, before, Path(tmp), insample=insample)


def status_line(res: Dict) -> str:
    """一行进度，供 `ic_rerun_readiness` 摘要行引用。不含效应量。"""
    s = res.get("status")
    if s == "cannot_judge":
        return f"⚠️ F&G 敞口门前瞻检验无法判定：{res.get('reason')}"
    if s == "not_ready":
        extra = f"（中期未过界，继续攒到 {res['next_look_at']} 周）" if res.get("looks_passed_without_verdict") else ""
        rate = res.get("selfproof_rate")
        proof = f"，自证 {rate:.0%}" if rate is not None else ""
        why = f"（{res['reason']}）" if res.get("reason") else ""
        icon = "⚠️" if res.get("stale") else "⏳"
        return f"{icon} F&G 敞口门前瞻检验：{res.get('weeks', 0)}/{res['next_look_at']} 个合格周{extra}{proof}{why}"
    if s in ("confirmed", "not_confirmed"):
        return (f"🔔 F&G 敞口门前瞻检验已到{res['look']}检视点 —— 跑 "
                "`/usr/local/bin/python3 experiments/fg_exposure_gate_forward_test.py` 看结论"
                "（需人判断，勿自动改 CONFIG）")
    return f"⚠️ F&G 敞口门前瞻检验状态未知：{s}"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="F&G 组合层敞口控制门前瞻检验（预注册）")
    ap.add_argument("--insample", action="store_true", help="样本内复核（生成假设/自检机制，不能用来确认）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--today", help="覆盖今天的日期（测试用，YYYY-MM-DD）")
    args = ap.parse_args(argv)
    res = run(insample=args.insample, today=args.today)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(res)
    return {"confirmed": 0, "not_confirmed": 0, "insample": 0, "not_ready": 1}.get(res.get("status"), 3)


def _print_human(res: Dict) -> None:
    print("━" * 72)
    print(f"🐝 F&G 敞口门前瞻检验（{res.get('mode', '?')}）")
    print("━" * 72)
    print(f"  日期数 {res.get('n_dates')}｜自证 {res.get('selfproof')}｜可用周数 {res.get('weeks_available')}")
    s = res.get("status")
    if s in ("confirmed", "not_confirmed"):
        st = res["stats"]
        print(f"  {res['look']}检视（前 {st['n']} 个合格周，单侧 α={res['alpha']}）："
              f"周均 ΔNAV% {st['mean']:+.4f}  t={st['t'] if st['t'] is None else round(st['t'], 2)}  p={st['p']:.4f}")
        print(f"  附带：被调整并已平仓的笔数 {res['adjusted_trades']['adjusted_closed_trades']}｜"
              f"这些笔的已实现盈亏 A={res['adjusted_trades']['pnl_sum_a']:+.2f} "
              f"vs B={res['adjusted_trades']['pnl_sum_b']:+.2f}")
        print("  结论：" + ("✅ 确认 —— 评估把 CONFIG['fg_exposure_gate']['enabled'] 改 True（用户拍板，非自动）"
                         if s == "confirmed" else "❌ 未确认 —— 保持默认关闭，登记结案"))
    elif s == "insample":
        st = res.get("stats_all_weeks")
        if st:
            print(f"  样本内（仅复核，不能确认）：{res['weeks_available']} 周，"
                  f"周均 ΔNAV% {st['mean']:+.4f} 单侧 p={st['p']:.4f}")
        print(f"  机制自检：{'✅ 通过' if res.get('mechanism_selfcheck_ok') else '❌ 失败'}")
    else:
        print("  " + status_line(res))


if __name__ == "__main__":
    sys.exit(main())
