"""Buzz 价格通道长历史回测（预注册：`experiments/price_channel_backtest_prereg.md`，v0.45.697）。

两步，各只做一件事：

  --dev      只看开发期（采样日 ≤ dev_end）：C0–C3 的对照数字 + E1–E6 的选入（写 `price_channel_backtest_dev_selection.json`）
  --holdout  留出期**只跑一次**：读 dev_selection，检验 C0–C3（+ 选入的 S），写 `price_channel_backtest_holdout.json`；
             结果文件已存在 ⇒ 拒绝（exit 2），不存在「多看一次」

取数：yfinance 日线，`--cache DIR` 把原始帧存成 pickle（不进仓库）；第二次跑读缓存、不打网。
口径全部照生产：窗口 (t − 1 个日历月, t] 用 `data_pipeline._volatility_20d_window_lo`，三字段用 `price_metrics.history_price_metrics`，
通道映射照 `swarm_agents/buzz_bee.py` 第 1–3 节、阈值与权重读 `config.AGENT_SCORING`（None ⇒ 中性 50，与生产同）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import pickle
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

PREREG_DOC = _HERE / "price_channel_backtest_prereg.md"
DEV_SELECTION = _HERE / "price_channel_backtest_dev_selection.json"
DEV_RESULT = _HERE / "price_channel_backtest_dev.json"
HOLDOUT_RESULT = _HERE / "price_channel_backtest_holdout.json"

#: 与预注册 §8 逐项相等（测试解析文档核对）
PREREG = {
    "dev_start": "2010-01-04",
    "dev_end": "2019-12-31",
    "holdout_start": "2020-01-01",
    "holdout_last_outcome": "2026-10-09",
    "horizon_trading_days": 7,
    "min_tickers": 15,
    "alpha": 0.01,
    "explore_alpha": 0.008333,
    "nw_lag": 2,
    "confirmatory": ["C0", "C1", "C2", "C3"],
    "exploratory": ["E1", "E2", "E3", "E4", "E5", "E6"],
    "tickers": ["NVDA", "TSLA", "MSFT", "QCOM", "VKTX", "META", "BILI", "AMZN", "RKLB", "CRCL", "CVX", "VZ",
                "JNJ", "XOM", "COST", "BRK-B", "AMC", "ABBV", "T", "DELL", "DE", "CRM", "MU", "WMT", "TMO",
                "TMUS", "ENPH", "NFLX", "NEE", "SNOW"],
}

FETCH_START = "2008-06-01"   # E4 要 t 往前 252 根；开发期首个采样日之前留足一年半
SENSITIVITY_WEEKDAYS = (1, 2, 3, 4)   # 周二..周五（0 = 周一是主分析的「每周第一个交易日」口径，另算）


# ── 通道映射（照 buzz_bee.py 第 1–3 节）─────────────────────────────────────

def _scoring() -> Dict:
    import config
    return config.AGENT_SCORING


def momentum_channel(mom: Optional[float]) -> float:
    if mom is None:
        return 50.0
    m = max(-10, min(10, mom))
    return (m + 10) / 20 * 100


def volume_channel(ratio: Optional[float], thresholds: Optional[Dict] = None) -> float:
    vt = thresholds if thresholds is not None else _scoring().get("volume_thresholds", {})
    if ratio is None:
        return 50
    if ratio > vt.get("very_high", 2.0):
        return 80
    if ratio > vt.get("high", 1.5):
        return 65
    if ratio > vt.get("normal", 1.0):
        return 50
    if ratio > vt.get("low", 0.5):
        return 35
    return 20


def volatility_channel(vol20: Optional[float], thresholds: Optional[Dict] = None) -> float:
    vlt = thresholds if thresholds is not None else _scoring().get("volatility_thresholds", {})
    if vol20 is None:
        return 50
    if vol20 > vlt.get("extreme", 60):
        return 25
    if vol20 > vlt.get("high", 40):
        return 40
    if vol20 > vlt.get("moderate", 20):
        return 60
    return 75


def composite(mom_c: float, vol_c: float, sig_c: float, weights: Optional[Dict] = None) -> float:
    bw = weights if weights is not None else _scoring().get("buzz_weights", {})
    wm, wv, ws = bw.get("momentum", 0.20), bw.get("volume", 0.10), bw.get("volatility", 0.05)
    return (mom_c * wm + vol_c * wv + sig_c * ws) / (wm + wv + ws)


# ── 取数 ─────────────────────────────────────────────────────────────────

def fetch(tickers: Sequence[str], end_exclusive: str, cache: Optional[Path]) -> Dict[str, "object"]:
    """每只票一帧（DatetimeIndex 无时区，Open/High/Low/Close/Volume）。缓存命中就不打网。"""
    import pandas as pd
    import price_metrics
    frames: Dict[str, object] = {}
    cache_file = cache / f"frames_{FETCH_START}_{end_exclusive}.pkl" if cache else None
    if cache_file and cache_file.exists():
        with open(cache_file, "rb") as f:
            return pickle.load(f)
    import yfinance as yf
    for tk in tickers:
        raw = yf.download(tk, start=FETCH_START, end=end_exclusive, auto_adjust=True, actions=True,
                          progress=False, multi_level_index=False, threads=False)
        if raw is None or len(raw) == 0:
            raise RuntimeError(f"{tk}: yfinance 返回空帧（限流或代码错）——不当成「没有这只票」继续")
        raw = price_metrics.drop_positions(raw, price_metrics.event_only_positions(raw))
        idx = raw.index.tz_localize(None) if raw.index.tz is not None else raw.index
        df = pd.DataFrame({c: raw[c].values for c in ("Open", "High", "Low", "Close", "Volume")}, index=idx)
        df = df[df["Close"].notna() & (df["Close"] > 0)]
        frames[tk] = df
        print(f"  取到 {tk}: {len(df)} 根 {df.index[0].date()} ~ {df.index[-1].date()}", file=sys.stderr)
    if cache_file:
        cache.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "wb") as f:
            pickle.dump(frames, f)
    return frames


def fingerprint(df) -> str:
    h = hashlib.sha256()
    h.update(df.to_csv(float_format="%.6f").encode())
    return h.hexdigest()[:16]


# ── 信号 ─────────────────────────────────────────────────────────────────

def signals_at(df, i: int) -> Dict[str, Optional[float]]:
    """`df` 第 i 根（= 信号日 t）收盘时能算的全部变体值；只用 ≤ t 的行。"""
    import data_pipeline
    import price_metrics
    t = df.index[i].date()
    lo = data_pipeline._volatility_20d_window_lo(t)
    upto = df.iloc[: i + 1]
    tail = df.iloc[max(0, i - 40): i + 1]          # 一个日历月最多约 23 根；多取一截再按日期截，省得每次扫整段历史
    win = tail[tail.index.date > lo]
    m = price_metrics.history_price_metrics(win, "bt")
    c1 = momentum_channel(m["momentum_5d"])
    c2 = volume_channel(m["volume_ratio"])
    c3 = volatility_channel(m["volatility_20d"])
    close = upto["Close"].values

    def mom_n(n: int) -> Optional[float]:
        return float(close[-1] / close[-1 - n] - 1) if len(close) > n else None

    e4 = float(close[-22] / close[-253] - 1) if len(close) > 252 else None
    vr, v20 = m["volume_ratio"], m["volatility_20d"]
    return {
        "C0": composite(c1, c2, c3), "C1": c1, "C2": c2, "C3": c3,
        "E1": mom_n(10), "E2": mom_n(20), "E3": mom_n(60), "E4": e4,
        "E5": math.log(vr) if vr is not None and vr > 0 else None,
        "E6": -v20 if v20 is not None else None,
    }


def trading_calendar(frames: Dict[str, object]) -> List[dt.date]:
    days = set()
    for df in frames.values():
        days.update(df.index.date)
    return sorted(days)


def sample_days(cal: List[dt.date], weekday_offset: int = 0) -> List[dt.date]:
    """每个 ISO 周取第 (1 + weekday_offset) 个有数据的交易日（0 = 第一个；周内不够那么多天 ⇒ 该周跳过）。"""
    by_week: Dict[Tuple[int, int], List[dt.date]] = {}
    for d in cal:
        by_week.setdefault(d.isocalendar()[:2], []).append(d)
    return [ds[weekday_offset] for _, ds in sorted(by_week.items()) if len(ds) > weekday_offset]


def build_panel(frames: Dict[str, object], days: Sequence[dt.date], cal: List[dt.date], horizon: int,
                last_outcome: dt.date) -> Dict[dt.date, List[Tuple[str, Dict, float]]]:
    """{采样日: [(票, 变体值, 前瞻收益)]}。前瞻收益 = 全市场日历第 +horizon 个交易日的收盘 / t 收盘 − 1；
    那天该票没 K 线或晚于 last_outcome ⇒ 不收。"""
    pos = {d: k for k, d in enumerate(cal)}
    out: Dict[dt.date, List] = {}
    for tk, df in frames.items():
        loc = {d: k for k, d in enumerate(df.index.date)}
        close = df["Close"].values
        for d in days:
            if d not in loc:
                continue
            j = pos[d] + horizon
            if j >= len(cal) or cal[j] > last_outcome or cal[j] not in loc:
                continue
            i = loc[d]
            if i < 4:
                continue
            ret = float(close[loc[cal[j]]] / close[i] - 1)
            out.setdefault(d, []).append((tk, signals_at(df, i), ret))
    return out


# ── 统计 ─────────────────────────────────────────────────────────────────

def spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    import pandas as pd
    s = pd.Series(xs).rank().corr(pd.Series(ys).rank())
    return None if s is None or not math.isfinite(s) else float(s)


def ic_series(panel, variant: str, min_n: int) -> List[Tuple[dt.date, float]]:
    out = []
    for d in sorted(panel):
        rows = [(r[1][variant], r[2]) for r in panel[d] if r[1][variant] is not None]
        if len(rows) < min_n:
            continue
        ic = spearman([a for a, _ in rows], [b for _, b in rows])
        if ic is not None:
            out.append((d, ic))
    return out


def nw_t(xs: Sequence[float], lag: int) -> Tuple[float, float, float]:
    """(均值, Newey-West 标准误, t)。lag = 0 即普通标准误。"""
    n = len(xs)
    mu = sum(xs) / n
    e = [x - mu for x in xs]
    s = sum(v * v for v in e) / n
    for k in range(1, lag + 1):
        w = 1 - k / (lag + 1)
        s += 2 * w * sum(e[i] * e[i - k] for i in range(k, n)) / n
    se = math.sqrt(s / n) if s > 0 else float("nan")
    return mu, se, mu / se if se and math.isfinite(se) else float("nan")


def p_two_sided(t: float, df: int) -> float:
    from scipy import stats
    return float(2 * stats.t.sf(abs(t), df))


def tercile_ls(panel, variant: str, min_n: int) -> Tuple[Optional[float], Optional[float], int]:
    """高 1/3 − 低 1/3 的周度等权收益：(均值 %, 胜率 %, 周数)。排序 key 只放信号（并列按票名，不按收益——见 memory 方法坑）。"""
    spreads = []
    for d in sorted(panel):
        rows = sorted(((r[1][variant], r[0], r[2]) for r in panel[d] if r[1][variant] is not None),
                      key=lambda x: (x[0], x[1]))
        if len(rows) < min_n:
            continue
        k = len(rows) // 3
        lo, hi = rows[:k], rows[-k:]
        spreads.append(sum(r[2] for r in hi) / k - sum(r[2] for r in lo) / k)
    if not spreads:
        return None, None, 0
    return (100 * sum(spreads) / len(spreads), 100 * sum(s > 0 for s in spreads) / len(spreads), len(spreads))


def summarize(panel, variant: str, lag: int, min_n: int) -> Dict:
    ser = ic_series(panel, variant, min_n)
    if len(ser) < 3:
        return {"weeks": len(ser), "mean_ic": None}
    xs = [v for _, v in ser]
    mu, se, t = nw_t(xs, lag)
    _, se0, t0 = nw_t(xs, 0)
    ls_mean, ls_hit, ls_n = tercile_ls(panel, variant, min_n)
    return {
        "weeks": len(ser), "first": str(ser[0][0]), "last": str(ser[-1][0]),
        "mean_ic": round(mu, 5), "nw_se": round(se, 5), "t": round(t, 3), "p": p_two_sided(t, len(ser) - 1),
        "t_plain": round(t0, 3), "ic_positive_weeks_pct": round(100 * sum(v > 0 for v in xs) / len(xs), 1),
        "tercile_ls_mean_pct": None if ls_mean is None else round(ls_mean, 4),
        "tercile_ls_hit_pct": None if ls_hit is None else round(ls_hit, 1),
        "avg_tickers": round(sum(len(panel[d]) for d, _ in ser) / len(ser), 1),
    }


def _restrict(panel, lo: dt.date, hi: dt.date):
    return {d: v for d, v in panel.items() if lo <= d <= hi}


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


# ── 两个入口 ─────────────────────────────────────────────────────────────

def run_dev(frames) -> Dict:
    P = PREREG
    cal = [d for d in trading_calendar(frames) if d <= _d(P["holdout_last_outcome"])]
    days = [d for d in sample_days(cal) if _d(P["dev_start"]) <= d <= _d(P["dev_end"])]
    # 结果日也不许越过 dev_end：开发期一根留出期的收盘价都不用（最后一两周因此凑不齐 T+7、不进样本）
    panel = build_panel(frames, days, cal, P["horizon_trading_days"], _d(P["dev_end"]))
    res = {v: summarize(panel, v, P["nw_lag"], P["min_tickers"]) for v in P["confirmatory"] + P["exploratory"]}
    passing = [(abs(res[v]["t"]), v) for v in P["exploratory"]
               if res[v].get("mean_ic") is not None and res[v]["p"] < P["explore_alpha"]]
    selected = max(passing)[1] if passing else None
    return {"results": res, "selected": selected,
            "selected_rule": "E1–E6 中开发期双侧 p < explore_alpha 者取 |t| 最大；无则 None"}


def run_holdout(frames, selected: Optional[str]) -> Dict:
    P = PREREG
    last = _d(P["holdout_last_outcome"])
    cal = [d for d in trading_calendar(frames) if d <= last]
    variants = P["confirmatory"] + ([selected] if selected else [])
    out: Dict = {"variants": variants, "alpha": P["alpha"]}

    def one(weekday_offset: int, lo: dt.date, hi: dt.date):
        days = [d for d in sample_days(cal, weekday_offset) if lo <= d <= hi]
        panel = build_panel(frames, days, cal, P["horizon_trading_days"], last)
        return {v: summarize(panel, v, P["nw_lag"], P["min_tickers"]) for v in variants}

    lo = _d(P["holdout_start"])
    main = one(0, lo, last)
    for v, r in main.items():
        r["verdict"] = ("未检出" if r.get("mean_ic") is None or r["p"] >= P["alpha"]
                        else ("检出·正" if r["mean_ic"] > 0 else "检出·负"))
    out["main"] = main
    out["sensitivity_ex_2026"] = one(0, lo, dt.date(2025, 12, 31))
    out["sensitivity_weekday"] = {str(k): one(k, lo, last) for k in SENSITIVITY_WEEKDAYS}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dev", action="store_true")
    g.add_argument("--holdout", action="store_true")
    ap.add_argument("--cache", type=Path, help="原始日线缓存目录（不进仓库）")
    a = ap.parse_args(argv)

    if a.holdout:
        if HOLDOUT_RESULT.exists():
            print(f"拒绝：{HOLDOUT_RESULT.name} 已存在——留出期只跑一次（预注册 §3）", file=sys.stderr)
            return 2
        if not DEV_SELECTION.exists():
            print("拒绝：先跑 --dev 产出选入结果", file=sys.stderr)
            return 2

    end_excl = str(_d(PREREG["holdout_last_outcome"]) + dt.timedelta(days=1))
    frames = fetch(PREREG["tickers"], end_excl, a.cache)
    meta = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "fingerprints": {tk: fingerprint(df) for tk, df in frames.items()},
            "first_bar": {tk: str(df.index[0].date()) for tk, df in frames.items()}}

    if a.dev:
        r = run_dev(frames)
        DEV_RESULT.write_text(json.dumps({**meta, **r}, ensure_ascii=False, indent=1), encoding="utf-8")
        DEV_SELECTION.write_text(json.dumps({"selected": r["selected"], "generated_at": meta["generated_at"]},
                                            ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0

    sel = json.loads(DEV_SELECTION.read_text(encoding="utf-8"))["selected"]
    r = run_holdout(frames, sel)
    HOLDOUT_RESULT.write_text(json.dumps({**meta, **r}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(r["main"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
