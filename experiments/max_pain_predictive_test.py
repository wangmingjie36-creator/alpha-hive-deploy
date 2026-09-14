"""Max Pain（「近端磁吸目标价」）有没有预测力？—— 横截面检验。

网站把 Max Pain 标成「磁吸目标价」，隐含的主张是**到期时股价会被拉向它**。
本脚本检验这个主张。结果只描述历史，不写生产、不改评分（v0.45.229）。

────────────────────────────────────────────────────────────────────
预注册（看结果之前写死；改动须在报告里标「事后」）
────────────────────────────────────────────────────────────────────
观测单位：(标的, 到期日 E) 一对只取**一条**观测，防同一到期日被多天重复计数。
    取 E 之前 1~7 个日历日内（与生产 ≤7 天窗口同口径）**最早**那份快照 t，
    即预测期尽量接近一周。
变量：
    S_t = 快照里的 `_snapshot_stock_price`（生产当时实际用的价）
    X   = 只用到期日 E 这一期持仓算的 Max Pain（生产函数 `_max_pain_from_oi`）
    g   = ln(X / S_t)     「磁吸缺口」：正 = 磁吸位在现价上方
    r   = ln(C_E / S_t)   C_E = 到期日 E 的收盘价
检验（族内 3 条，Bonferroni α = 0.05/3）：
    T1 方向  每个到期日做横截面 Spearman IC(g, r)（当日 ≥8 只才算），
             同一 ISO 周的 IC 取均值 → 对周序列做单样本 t 检验。
             **不池化**：见 auto-memory alpha-hive-cross-sectional-pooling。
    T2 钉住  逐条算 gain = |ln(C_E/S_t)| − |ln(C_E/X)|（正 = 磁吸位比「现价不动」
             更接近到期收盘价）。按周取均值 → t 检验。基准是随机游走。
    T3 去反转 横截面上把 rank(g) 对 rank(过去 5 日收益) 回归取残差，再算 IC(残差, r)。
             理由：股价刚涨过一段后，持仓质心往往留在下方 ⇒ g 与短期反转天然相关，
             T1 若显著，可能只是反转因子换了个名字。
稳健性（不计入族，只看方向是否一致）：
    R1 跳一天  r' = ln(C_E / 快照之后第一个收盘价)。S_t 同时出现在 g 与 r 里，
               S_t 的噪声会造出**虚假正相关**；跳一天把共享噪声拆开。
    R2 生产口径  X 换成网站实际显示的 ≤7 天聚合 Max Pain。
    R3 分期    只用 ≥2026-06-30（CBOE 成为期权主源）/ ≥2026-08-04（扩到 30 只）。
剔除：
    文件名带 `_backfilled`（事后补抓，OI 是抓取日的，含未来信息）；
    缺逐到期日持仓（`call_exp_oi`）的老快照；不在 `config.WATCHLIST` 的标的；
    E 晚于价格数据末日。

────────────────────────────────────────────────────────────────────
持仓量日期（v0.45.229 实测，决定有没有未来函数）
────────────────────────────────────────────────────────────────────
CBOE 延迟行情的 open_interest **不是抓取当天的**：交易日 d 的收盘持仓在 d+1 白天才
进 feed。故快照 t 里的 OI 至多是 t−1 收盘，价格至多是 t 收盘 ⇒ 无未来函数。
取证见报告第 1 节。

用法：
    /usr/local/bin/python3 experiments/max_pain_predictive_test.py \
        --closes <收盘价 CSV，列=标的，索引=日期>  [--out 结果.json]
    不给 --closes 时用 yfinance 批量下载一次（免费，无 API 费用）。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 代码锚点：import 仓内模块

import config  # noqa: E402
from hive_logger import PATHS  # noqa: E402
from swarm_agents.oracle_bee import OracleBeeEcho  # noqa: E402

MIN_CS = 8               # 横截面最少标的数
DTE_MIN, DTE_MAX = 1, 7
PDT_CLOSE_MIN = 13 * 60 + 5   # 快照时间戳是本机 PDT；13:05 之后视为当日已收盘
_NAME = re.compile(r"options_snapshot_(.+?)_(\d{4}-\d{2}-\d{2})(_backfilled.*)?\.json$")


# ── 数据装载 ─────────────────────────────────────────────────────────
def load_snapshots(cache_dir: Path, watchlist: set):
    snaps, skipped = [], defaultdict(int)
    for f in sorted(cache_dir.glob("options_snapshot_*_2026-*.json")):
        m = _NAME.search(f.name)
        if not m or " " in f.name:
            skipped["名字不合规/iCloud副本"] += 1
            continue
        tk, d, bf = m.groups()
        if bf:
            skipped["事后回补"] += 1
            continue
        if tk not in watchlist:
            skipped["不在名单"] += 1
            continue
        try:
            js = json.loads(f.read_text())
        except (OSError, ValueError):
            skipped["JSON坏"] += 1
            continue
        fc = js.get("full_chain_oi") or {}
        px = js.get("_snapshot_stock_price") or 0
        if not fc.get("call_exp_oi") or not px:
            skipped["缺逐到期日OI或价格"] += 1
            continue
        snaps.append(dict(ticker=tk, date=date.fromisoformat(d),
                          ts=datetime.fromisoformat(js["timestamp"]), px=float(px), fc=fc))
    return snaps, dict(skipped)


def single_expiry_mp(fc: dict, expiry: str, px: float):
    call = {float(k): int(v.get(expiry) or 0) for k, v in fc["call_exp_oi"].items() if v.get(expiry)}
    put = {float(k): int(v.get(expiry) or 0) for k, v in (fc.get("put_exp_oi") or {}).items() if v.get(expiry)}
    return OracleBeeEcho._max_pain_from_oi(call, put, px)


def window_mp(fc: dict, today: date, px: float):
    c, p, _used, _bad = OracleBeeEcho._near_oi_by_strike(fc, today, OracleBeeEcho.NEAR_WINDOW_DAYS)
    return OracleBeeEcho._max_pain_from_oi(c, p, px)


# ── 观测构造 ─────────────────────────────────────────────────────────
def build_obs(snaps, closes: pd.DataFrame):
    tdays = [d.date() for d in closes.index]
    last_day = tdays[-1]
    by_key = {}   # (ticker, E) -> 最早的合格快照
    for s in snaps:
        exps = {e for m in s["fc"]["call_exp_oi"].values() for e in m}
        for e in exps:
            try:
                ed = date.fromisoformat(str(e)[:10])
            except ValueError:
                continue
            dte = (ed - s["date"]).days
            if not (DTE_MIN <= dte <= DTE_MAX) or ed > last_day:
                continue
            k = (s["ticker"], str(e)[:10])
            if k not in by_key or s["date"] < by_key[k]["date"]:
                by_key[k] = s

    obs, drop = [], defaultdict(int)
    for (tk, e), s in by_key.items():
        ed = date.fromisoformat(e)
        if pd.Timestamp(ed) not in closes.index:
            drop["到期日无收盘价(非交易日)"] += 1
            continue
        c_e = closes.at[pd.Timestamp(ed), tk]
        x = single_expiry_mp(s["fc"], e, s["px"])
        if x is None or not np.isfinite(c_e):
            drop["该期OI过薄或价格缺失"] += 1
            continue
        # 快照时刻「最后一个已完成收盘」与「之后第一个收盘」
        mins = s["ts"].hour * 60 + s["ts"].minute
        cutoff = s["ts"].date() if mins >= PDT_CLOSE_MIN else s["ts"].date() - timedelta(days=1)
        past = [d for d in tdays if d <= cutoff]
        if len(past) < 6:
            drop["历史不足5日"] += 1
            continue
        i_last = tdays.index(past[-1])
        c_last, c_5 = closes.iloc[i_last][tk], closes.iloc[i_last - 5][tk]
        nxt = tdays[i_last + 1] if i_last + 1 < len(tdays) else None
        xa = window_mp(s["fc"], s["date"], s["px"])
        obs.append(dict(
            ticker=tk, expiry=e, snap=str(s["date"]), dte=(ed - s["date"]).days,
            week=f"{ed.isocalendar()[0]}-W{ed.isocalendar()[1]:02d}",
            px=s["px"], mp=x, mp_win=xa, close_e=float(c_e),
            g=math.log(x / s["px"]), r=math.log(c_e / s["px"]),
            g_win=(math.log(xa / s["px"]) if xa else None),
            past5=math.log(c_last / c_5),
            past1=math.log(c_last / closes.iloc[i_last - 1][tk]),
            r_skip=(math.log(c_e / closes.loc[pd.Timestamp(nxt), tk]) if nxt and nxt < ed else None),
            r_day1=(math.log(closes.loc[pd.Timestamp(nxt), tk] / s["px"]) if nxt and nxt < ed else None),
            px_vs_close=s["px"] / c_last - 1,
        ))
    return pd.DataFrame(obs), dict(drop)


# ── 统计 ────────────────────────────────────────────────────────────
def weekly_t(per_expiry: pd.DataFrame, col: str):
    wk = per_expiry.groupby("week")[col].mean().dropna()
    n = len(wk)
    if n < 3:
        return dict(n_weeks=n, mean=None, t=None, p=None)
    t, p = stats.ttest_1samp(wk.values, 0.0)
    return dict(n_weeks=n, mean=float(wk.mean()), sd=float(wk.std(ddof=1)), t=float(t), p=float(p),
                pos_weeks=int((wk > 0).sum()), mde_80=float(2.8 * wk.std(ddof=1) / math.sqrt(n)))


def cross_sections(df: pd.DataFrame, xcol: str, ycol: str, resid_on: str | None = None):
    rows = []
    for (e, week), g in df.groupby(["expiry", "week"]):
        g = g.dropna(subset=[xcol, ycol] + ([resid_on] if resid_on else []))
        if len(g) < MIN_CS or g[xcol].nunique() < 2:
            continue
        x = g[xcol].rank()
        if resid_on:
            z = g[resid_on].rank()
            beta = np.polyfit(z, x, 1)
            x = x - np.polyval(beta, z)
        rho = stats.spearmanr(x, g[ycol]).statistic
        rows.append(dict(expiry=e, week=week, n=len(g), ic=float(rho)))
    return pd.DataFrame(rows)


def pin_gain(df: pd.DataFrame):
    d = df.assign(gain=(df["r"].abs() - np.log(df["close_e"] / df["mp"]).abs()) * 1e4)   # 基点
    per = d.groupby(["expiry", "week"]).agg(gain=("gain", "mean"), n=("gain", "size")).reset_index()
    per = per[per["n"] >= MIN_CS]
    win = float((d["gain"] > 0).mean())
    return per, dict(win_rate=win, pooled_mean_bp=float(d["gain"].mean()), median_bp=float(d["gain"].median()))


def run(df: pd.DataFrame, label: str):
    out = {"label": label, "n_obs": len(df), "n_tickers": int(df["ticker"].nunique()) if len(df) else 0}
    if not len(df):
        return out
    t1 = cross_sections(df, "g", "r")
    out["T1_ic"] = weekly_t(t1, "ic") | {"n_cross_sections": len(t1),
                                        "pooled_spearman": float(stats.spearmanr(df["g"], df["r"]).statistic)}
    per, pooled = pin_gain(df)
    out["T2_pin_bp"] = weekly_t(per, "gain") | pooled
    t3 = cross_sections(df, "g", "r", resid_on="past5")
    out["T3_ic_resid"] = weekly_t(t3, "ic")
    out["ref_reversal_ic"] = weekly_t(cross_sections(df.assign(neg_past5=-df["past5"]), "neg_past5", "r"), "ic")
    out["R1_skip_day_ic"] = weekly_t(cross_sections(df.dropna(subset=["r_skip"]), "g", "r_skip"), "ic")
    out["R2_window_ic"] = weekly_t(cross_sections(df.dropna(subset=["g_win"]), "g_win", "r"), "ic")
    out["weeks_detail"] = t1.groupby("week").agg(ic=("ic", "mean"), n_sections=("ic", "size"),
                                                  n_obs=("n", "sum")).round(3).reset_index().to_dict("records")
    return out


def posthoc(df: pd.DataFrame):
    """⚠️ 事后诊断（看过 v1 结果后才加，不属于预注册族，不做显著性结论）。

    v1 现象：T1 显著，但 R1（跳过快照后第一天）归零 ⇒ 信号若存在，全部挤在第一天。
    两种解释要分开：
      (a) 共享噪声——S_t 不是真实收盘价（盘中/盘后价），g 与 r 同含 −ln S_t 造出正相关；
      (b) 真实的次日效应——S_t 就是收盘价，而次日价格朝磁吸位走。
    判别：按「快照价是否≈上一个已完成收盘价」分组。(a) 预测干净组 IC 消失，(b) 预测干净组仍在。
    """
    df = df.dropna(subset=["r_day1", "r_skip"])
    clean = df[df["px_vs_close"].abs() < 0.001]
    dirty = df[df["px_vs_close"].abs() >= 0.001]
    out = {"n_clean": len(clean), "n_dirty": len(dirty),
           "dirty_share_by_era": {
               "<08-04": float((df[df["snap"] < "2026-08-04"]["px_vs_close"].abs() >= 0.001).mean()),
               ">=08-04": float((df[df["snap"] >= "2026-08-04"]["px_vs_close"].abs() >= 0.001).mean())},
           "dirty_median_abs_dev_pct": float(dirty["px_vs_close"].abs().median() * 100) if len(dirty) else None}
    for name, sub in (("all", df), ("clean", clean), ("dirty", dirty)):
        # 脏组逐截面样本少，放宽到 ≥5 只，仅作方向参考
        global MIN_CS
        keep, MIN_CS = MIN_CS, (MIN_CS if name != "dirty" else 5)
        try:
            out[name] = {c: weekly_t(cross_sections(sub, "g", c), "ic") for c in ("r", "r_day1", "r_skip")}
            out[name]["pooled"] = {c: float(stats.spearmanr(sub["g"], sub[c]).statistic) for c in ("r", "r_day1", "r_skip")}
        finally:
            MIN_CS = keep
    # 第二层事后：干净组的「第一天」效应是不是 1 日反转？（当日跌破磁吸位的票次日反弹）
    c = clean.dropna(subset=["past1"])
    out["clean_day1_vs_reversal"] = {
        "ic_neg_past1_day1": weekly_t(cross_sections(c.assign(neg_past1=-c["past1"]), "neg_past1", "r_day1"), "ic"),
        "ic_g_resid_past1_day1": weekly_t(cross_sections(c, "g", "r_day1", resid_on="past1"), "ic"),
        "corr_g_neg_past1": float(stats.spearmanr(c["g"], -c["past1"]).statistic),
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--closes", help="收盘价 CSV（index=日期，列=标的）")
    ap.add_argument("--out")
    a = ap.parse_args()
    wl = set(config.WATCHLIST)
    if a.closes:
        closes = pd.read_csv(a.closes, index_col=0, parse_dates=True)
    else:
        import yfinance as yf
        df = yf.download(sorted(wl), start="2026-05-01", auto_adjust=False, progress=False, threads=False)
        closes = df["Close"]
    closes = closes.sort_index()

    snaps, skipped = load_snapshots(PATHS.cache_dir, wl)
    df, dropped = build_obs(snaps, closes)
    if df.empty:
        sys.exit("❌ 没有构造出任何观测——检查快照目录与价格数据，不输出空结论")

    res = {"skipped_files": skipped, "dropped_obs": dropped, "n_snapshots_used": len(snaps)}
    # 价格自证：收盘后抓的快照，快照价应≈当日收盘价；对不上说明价格对齐错了
    res["px_check"] = {"median_abs_dev_pct": float(df["px_vs_close"].abs().median() * 100),
                       "share_within_1pct": float((df["px_vs_close"].abs() < 0.01).mean())}
    res["full"] = run(df, "全样本")
    res["R3_since_0630"] = run(df[df["snap"] >= "2026-06-30"], "≥06-30 CBOE 主源")
    res["R3_since_0804"] = run(df[df["snap"] >= "2026-08-04"], "≥08-04 30 只")
    res["posthoc_day1_decomposition"] = posthoc(df)
    res["describe"] = {"median_abs_gap_pct": float(df["g"].abs().median() * 100),
                       "median_abs_move_pct": float(df["r"].abs().median() * 100),
                       "dte_counts": df["dte"].value_counts().sort_index().to_dict()}
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    if a.out:
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
        df.to_csv(Path(a.out).with_suffix(".obs.csv"), index=False)


if __name__ == "__main__":
    main()
