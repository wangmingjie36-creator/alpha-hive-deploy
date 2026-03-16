"""
IV Crush 历史分析模块
━━━━━━━━━━━━━━━━━━━
投行必看数据：每次财报后实际涨跌幅 vs 隐含涨跌幅的历史命中率
数据源：yfinance（历史价格 + 财报日期 + 当前期权链）

功能：
  - 过去 8 个季度实际财报日涨跌幅
  - 当前最近财报 ATM 跨式期权隐含涨跌幅（Straddle Implied Move）
  - 历史 Beat Rate（实际幅度超过隐含幅度的比例）
  - 财报方向偏好（多少次向上 vs 向下）
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta
from typing import Dict, List, Optional

_log = logging.getLogger("alpha_hive.iv_crush")


def get_iv_crush_data(ticker: str, lookback: int = 8) -> Dict:
    """
    计算 ticker 历史 IV Crush 数据。

    返回：
      {
        "ticker": str,
        "next_earnings_date": str | None,       # 下次财报日 YYYY-MM-DD
        "next_earnings_days": int | None,        # 距今天数
        "historical_moves": [float],             # 过去 N 次财报日涨跌幅（%）
        "avg_abs_move": float,                   # 平均绝对幅度（%）
        "avg_up_move": float,                    # 上涨时平均幅度
        "avg_down_move": float,                  # 下跌时平均幅度
        "up_count": int,                         # 上涨次数
        "down_count": int,                       # 下跌次数
        "current_implied_move": float | None,    # 当前最近期权隐含涨跌幅（%）
        "implied_move_expiry": str | None,        # 对应期权到期日
        "historical_beat_rate": float | None,    # 实际 > 隐含 的历史比例（用历史均值估算）
        "earnings_surprise_avg": float | None,   # 平均 EPS surprise（%）
        "source": str,
      }
    """
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        return _empty(ticker, "yfinance not installed")

    result: Dict = {
        "ticker": ticker,
        "next_earnings_date": None,
        "next_earnings_days": None,
        "historical_moves": [],
        "avg_abs_move": 0.0,
        "avg_up_move": 0.0,
        "avg_down_move": 0.0,
        "up_count": 0,
        "down_count": 0,
        "current_implied_move": None,
        "implied_move_expiry": None,
        "historical_beat_rate": None,
        "earnings_surprise_avg": None,
        "source": "yfinance",
    }

    try:
        stock = yf.Ticker(ticker)

        # ── 1. 财报日期（历史 + 即将） ──────────────────────────────────────
        earnings_df = stock.earnings_dates
        if earnings_df is None or earnings_df.empty:
            return {**result, "source": "yfinance:no_earnings_dates"}

        # 分离已发布（有 Reported EPS）vs 即将（NaN）
        past = earnings_df[earnings_df["Reported EPS"].notna()].head(lookback)
        upcoming = earnings_df[earnings_df["Reported EPS"].isna()]

        if not upcoming.empty:
            next_ed = pd.Timestamp(upcoming.index[0]).tz_localize(None)
            result["next_earnings_date"] = next_ed.strftime("%Y-%m-%d")
            result["next_earnings_days"] = (next_ed - datetime.now()).days

        # EPS surprise 均值
        surprise_col = "Surprise(%)"
        if surprise_col in past.columns and not past[surprise_col].isna().all():
            result["earnings_surprise_avg"] = round(
                float(past[surprise_col].dropna().mean()), 2
            )

        # ── 2. 历史财报日价格变动 ─────────────────────────────────────────
        hist = stock.history(period="3y", interval="1d")
        if hist.empty:
            return {**result, "source": "yfinance:no_price_history"}

        # 去时区
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index

        moves: List[float] = []
        for date_ts, _ in past.iterrows():
            ed = pd.Timestamp(date_ts).tz_localize(None)
            before = hist[hist.index <= ed].tail(1)
            after = hist[hist.index > ed].head(1)
            if before.empty or after.empty:
                continue
            pre = float(before.iloc[-1]["Close"])
            post = float(after.iloc[0]["Close"])
            if pre > 0:
                move = (post - pre) / pre * 100
                moves.append(round(move, 2))

        if moves:
            result["historical_moves"] = moves
            abs_moves = [abs(m) for m in moves]
            result["avg_abs_move"] = round(statistics.mean(abs_moves), 2)
            up = [m for m in moves if m > 0]
            down = [m for m in moves if m < 0]
            result["up_count"] = len(up)
            result["down_count"] = len(down)
            result["avg_up_move"] = round(statistics.mean(up), 2) if up else 0.0
            result["avg_down_move"] = round(statistics.mean(down), 2) if down else 0.0

        # ── 3. 当前 ATM 跨式隐含涨跌幅（最近财报对应期权） ──────────────
        try:
            exps = stock.options
            if exps:
                current_price = float(hist.iloc[-1]["Close"])

                # 优先选择在下次财报后到期的最近期权
                target_exp = exps[0]  # 默认最近
                if result["next_earnings_date"]:
                    next_ed_str = result["next_earnings_date"]
                    for exp in exps:
                        if exp >= next_ed_str:
                            target_exp = exp
                            break

                chain = stock.option_chain(target_exp)
                calls = chain.calls
                puts = chain.puts

                if not calls.empty and not puts.empty:
                    # ATM call
                    atm_c_idx = (calls["strike"] - current_price).abs().idxmin()
                    atm_c = float(calls.loc[atm_c_idx, "lastPrice"])

                    # ATM put（同行权价）
                    atm_strike = float(calls.loc[atm_c_idx, "strike"])
                    put_row = puts[abs(puts["strike"] - atm_strike) < 1.0]
                    atm_p = float(put_row.iloc[0]["lastPrice"]) if not put_row.empty else 0.0

                    straddle = atm_c + atm_p
                    if current_price > 0:
                        result["current_implied_move"] = round(straddle / current_price * 100, 2)
                        result["implied_move_expiry"] = target_exp
        except Exception as e:
            _log.debug("IV crush: options error for %s: %s", ticker, e)

        # ── 4. 历史 Beat Rate（实际 abs > 当前隐含移动的概率，用历史均值代理） ──
        if result["avg_abs_move"] and result["current_implied_move"]:
            implied = result["current_implied_move"]
            beat_count = sum(1 for m in [abs(x) for x in moves] if m > implied)
            result["historical_beat_rate"] = round(beat_count / len(moves), 2) if moves else None

    except Exception as e:
        _log.warning("IV crush error for %s: %s", ticker, e)
        return {**result, "source": f"error:{e}"}

    return result


def format_iv_crush_summary(data: Dict) -> str:
    """返回注入 LLM prompt 的简洁文字摘要"""
    if not data.get("historical_moves"):
        return ""

    lines = [f"【IV Crush 历史数据 · {data['ticker']}】"]

    if data.get("next_earnings_date"):
        days = data.get("next_earnings_days", "?")
        lines.append(f"下次财报: {data['next_earnings_date']} (T-{days}天)")

    lines.append(
        f"历史平均财报日波动: ±{data['avg_abs_move']}% "
        f"| 上涨{data['up_count']}次 avg {data['avg_up_move']:+.1f}% "
        f"| 下跌{data['down_count']}次 avg {data['avg_down_move']:+.1f}%"
    )

    if data.get("current_implied_move"):
        lines.append(
            f"当前隐含涨跌幅(ATM Straddle): ±{data['current_implied_move']}% "
            f"(到期: {data.get('implied_move_expiry', '?')})"
        )

    if data.get("historical_beat_rate") is not None:
        beat_pct = int(data["historical_beat_rate"] * 100)
        verdict = "历史上常超出隐含幅度" if beat_pct >= 60 else (
            "历史上常低于隐含幅度" if beat_pct <= 40 else "隐含幅度定价较准确"
        )
        lines.append(f"实际超过隐含幅度概率: {beat_pct}% → {verdict}")

    if data.get("earnings_surprise_avg") is not None:
        lines.append(f"EPS beat 均值: {data['earnings_surprise_avg']:+.1f}%")

    # 过去4次明细
    recent = data["historical_moves"][:4]
    lines.append("近4次财报日涨跌: " + " | ".join(f"{m:+.1f}%" for m in recent))

    return "\n".join(lines)


def _empty(ticker: str, reason: str) -> Dict:
    return {
        "ticker": ticker, "next_earnings_date": None, "next_earnings_days": None,
        "historical_moves": [], "avg_abs_move": 0.0, "avg_up_move": 0.0,
        "avg_down_move": 0.0, "up_count": 0, "down_count": 0,
        "current_implied_move": None, "implied_move_expiry": None,
        "historical_beat_rate": None, "earnings_surprise_avg": None,
        "source": reason,
    }


if __name__ == "__main__":
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    data = get_iv_crush_data(t)
    print(format_iv_crush_summary(data))
    print("\nRaw:", data)
