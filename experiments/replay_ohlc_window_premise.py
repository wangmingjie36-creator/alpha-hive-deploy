"""回放 OHLC 窗口（`paper_portfolio.replay_ohlc_window`，v0.45.391）的等价前提核对——只读诊断，**打真 Yahoo**。

前提：同一标的「窄区间一次取」与「宽区间取了再切片」逐根相同（两边都经 `_bars_from_history`）。
它是观测到的事实，不是 Yahoo 的契约——v0.45.391 在真实数据上核过一次（CHANGELOG 同版），本脚本把
这次核对变成一条随时可重跑的显式命令。刻意不写成默认测试：pytest 的 `network` 标记有「只减不增」
的棘轮（`tests/test_network_marker_discipline.py`），写成 skip 又等于没有。

做法：在窗口里跑一次 F&G 前瞻检验的真实 `run()`，记下回放向 `_fetch_ohlc` 发出的每个**不同**请求及
窗口给出的切片；再在窗口外逐个用直连路径同一个调用（`yf.Ticker(t).history(start, end,
auto_adjust=False)` + `_bars_from_history`）重取，逐请求比对。

    /usr/local/bin/python3 experiments/replay_ohlc_window_premise.py [--today YYYY-MM-DD] [--json]

退出码：0 全部相同；1 有请求不同（逐条列出差在哪几天）；3 无法判定（`run()` 没打开窗口 / 窗口没服务任何
请求 / 直连重取失败）。⚠️ 盘中跑会把「今天那根 bar 在两次取数之间变了」报成不同——收盘后跑，
或看差异日期是不是只有今天。

只读：重放本身在临时沙箱里（`run_replay` 的 `_REPLAY_MODE` 屏蔽屏障回写），本脚本不写任何文件。
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 代码锚点：import 仓内模块


def _load_fwd():
    path = Path(__file__).resolve().parent / "fg_exposure_gate_forward_test.py"  # 代码锚点
    spec = importlib.util.spec_from_file_location("fg_exposure_gate_forward_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(today: Optional[str] = None, fwd_module=None) -> Dict:
    import paper_portfolio as pp
    fwd = fwd_module if fwd_module is not None else _load_fwd()

    served: Dict = {}
    windows = []
    orig_fetch, orig_window = pp._fetch_ohlc, pp.replay_ohlc_window

    def recording_fetch(ticker, start, end):
        out = orig_fetch(ticker, start, end)
        win = pp._REPLAY_OHLC_WINDOW
        if win is not None and ticker not in win.fallback_tickers:
            served.setdefault((ticker, start, end), out)
        return out

    @contextlib.contextmanager
    def recording_window(start, end):
        with orig_window(start, end) as win:
            windows.append(win)
            yield win

    pp._fetch_ohlc, pp.replay_ohlc_window = recording_fetch, recording_window
    try:
        run_res = fwd.run(today=today)
    finally:
        pp._fetch_ohlc, pp.replay_ohlc_window = orig_fetch, orig_window

    out: Dict = {"run_status": run_res.get("status"), "n_windows": len(windows)}
    if not windows:
        return {**out, "status": "cannot_judge",
                "reason": "run() 没有打开回放窗口（没有前瞻样本 / 种子不可用 / 提前返回）"}
    win = windows[0]
    out.update(window=[win.start, win.end], fallback_tickers=dict(win.fallback_tickers),
               out_of_window=win.out_of_window)
    # 退回直连 / 窗口外的请求本来就走直连路径，与「改动前」逐字相同，不需要也不能拿来核前提。
    todo = {k: v for k, v in served.items()
            if k[0] not in win.fallback_tickers and win.start <= k[1] < k[2] <= win.end}
    out["n_requests"] = len(todo)
    out["n_tickers"] = len({k[0] for k in todo})
    if not todo:
        return {**out, "status": "cannot_judge", "reason": "窗口没有服务任何请求，无从核对"}

    import yfinance as yf
    mismatches, failures = [], []
    for (ticker, start, end), sliced in sorted(todo.items()):
        try:
            hist = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=False)
            direct = {} if hist is None or len(hist) == 0 else pp._bars_from_history(ticker, hist)
        except Exception as e:  # noqa: BLE001 —— 进 failures ⇒ cannot_judge，不吞
            failures.append({"ticker": ticker, "start": start, "end": end, "error": f"{type(e).__name__}: {e}"})
            continue
        if direct != sliced:
            days = sorted(d for d in set(direct) | set(sliced) if direct.get(d) != sliced.get(d))
            mismatches.append({"ticker": ticker, "start": start, "end": end, "days": days})
    out["mismatches"] = mismatches
    if failures:
        return {**out, "status": "cannot_judge", "failures": failures,
                "reason": f"{len(failures)} 个请求直连重取失败，前提无从核对"}
    if mismatches:
        return {**out, "status": "mismatch",
                "reason": f"{len(mismatches)}/{len(todo)} 个请求「窄取」≠「宽取切片」"}
    return {**out, "status": "ok"}


_EXIT = {"ok": 0, "mismatch": 1}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="回放 OHLC 窗口等价前提核对（打真 Yahoo，只读）")
    ap.add_argument("--today", help="YYYY-MM-DD，传给 F&G 前瞻检验的 run()")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    res = check(today=args.today)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        s = res["status"]
        print(f"{'✅' if s == 'ok' else '⚠️'} 回放 OHLC 窗口前提：{s}"
              f"（{res.get('n_requests', 0)} 个请求 / {res.get('n_tickers', 0)} 个标的，"
              f"窗口 {res.get('window')}）{('— ' + res['reason']) if res.get('reason') else ''}")
        for m in res.get("mismatches", [])[:20]:
            print(f"   ≠ {m['ticker']} [{m['start']}, {m['end']}) 差在 {m['days']}")
    return _EXIT.get(res["status"], 3)


if __name__ == "__main__":
    sys.exit(main())
