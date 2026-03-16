#!/usr/bin/env python3
"""
Alpha Hive MCP Server  —  alpha_hive_mcp
=========================================
Exposes Alpha Hive analysis data, live yfinance market data, and SEC EDGAR
insider-filing data to Claude Cowork via the Model Context Protocol.

Transport : stdio  (local, runs on user's Mac as a subprocess of Claude Desktop)
Version   : 1.0.0

Available Tools
---------------
  alphahive_list_reports          List all analysis JSON files on disk
  alphahive_get_analysis          Full analysis JSON for a ticker + date
  alphahive_get_swarm_scores      Focused swarm/bee score + confidence band
  alphahive_get_gex               Dealer GEX snapshot from analysis JSON
  alphahive_get_options_snapshot  Live P/C ratio + OI from yfinance
  alphahive_get_quote             Live price + fundamentals from yfinance
  alphahive_get_price_history     OHLCV candles from yfinance
  alphahive_get_insider_trades    Recent Form 4 filings from SEC EDGAR

Claude Desktop config  (~/.claude/claude_desktop_config.json):
  {
    "mcpServers": {
      "alpha_hive": {
        "command": "python3",
        "args": ["/Users/<you>/Desktop/Alpha Hive/alpha_hive_mcp.py"]
      }
    }
  }

Dependencies (all already used by Alpha Hive):
  pip install mcp yfinance httpx pydantic
"""

import json
import asyncio
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import httpx
import yfinance as yf
from pydantic import BaseModel, Field, ConfigDict, field_validator
from mcp.server.fastmcp import FastMCP

# ─── Server ───────────────────────────────────────────────────────────────────
mcp = FastMCP("alpha_hive_mcp")

# ─── Directory Constants ──────────────────────────────────────────────────────
# Adjust these if your folders live elsewhere
_HIVE_DIR = Path.home() / "Desktop" / "Alpha Hive"
_DEEP_DIR = Path.home() / "Desktop" / "深度分析报告" / "深度"

# ─── Shared Utilities ─────────────────────────────────────────────────────────

def _load_json(ticker: str, date_str: str) -> dict:
    """Load analysis-{TICKER}-ml-{DATE}.json from _HIVE_DIR."""
    path = _HIVE_DIR / f"analysis-{ticker.upper()}-ml-{date_str}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Report not found: {path.name}  "
            f"(run alpha_hive.py --ticker {ticker} first)"
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _latest_date(ticker: str) -> Optional[str]:
    """Return the date string of the most-recently-modified JSON for ticker."""
    files = sorted(
        _HIVE_DIR.glob(f"analysis-{ticker.upper()}-ml-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        return None
    parts = files[0].stem.split("-ml-")
    return parts[1] if len(parts) == 2 else None


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


def _ok(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _native(d: dict) -> dict:
    """Convert numpy scalars → native Python types so json.dumps encodes them
    as numbers rather than strings (avoids numpy.float64 → '"115.0"' issue)."""
    out: dict = {}
    for k, v in d.items():
        if hasattr(v, "item"):          # numpy scalar (float64, int64, …)
            v = v.item()
        if isinstance(v, float) and math.isnan(v):
            v = None                    # NaN is not valid JSON
        out[k] = v
    return out


# ─── Input Models ─────────────────────────────────────────────────────────────

class TickerDateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    ticker: str = Field(
        ...,
        description="Stock ticker symbol, e.g. 'NVDA', 'AAPL'",
        min_length=1, max_length=10,
    )
    date_str: Optional[str] = Field(
        default=None,
        description=(
            "Analysis date in YYYY-MM-DD format. "
            "If omitted, the most-recently-generated report is used."
        ),
    )

    @field_validator("ticker")
    @classmethod
    def _up(cls, v: str) -> str:
        return v.upper().strip()


class TickerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    ticker: str = Field(
        ..., description="Stock ticker symbol, e.g. 'NVDA'", min_length=1, max_length=10
    )

    @field_validator("ticker")
    @classmethod
    def _up(cls, v: str) -> str:
        return v.upper().strip()


class PriceHistoryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    ticker: str = Field(..., description="Stock ticker symbol", min_length=1, max_length=10)
    period: str = Field(
        default="1mo",
        description="Time period: '1d','5d','1mo','3mo','6mo','1y','2y','5y'",
    )
    interval: str = Field(
        default="1d",
        description="Bar interval: '1m','5m','15m','1h','1d','1wk','1mo'",
    )

    @field_validator("ticker")
    @classmethod
    def _up(cls, v: str) -> str:
        return v.upper().strip()


class InsiderTradesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    ticker: str = Field(..., description="Stock ticker symbol", min_length=1, max_length=10)
    days_back: int = Field(
        default=90,
        description="Number of past days to search for Form 4 filings",
        ge=1, le=365,
    )

    @field_validator("ticker")
    @classmethod
    def _up(cls, v: str) -> str:
        return v.upper().strip()


# ─── Tool 1 · List Reports ────────────────────────────────────────────────────

@mcp.tool(
    name="alphahive_list_reports",
    annotations={
        "title": "List Alpha Hive Reports",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def alphahive_list_reports() -> str:
    """List all available Alpha Hive analysis JSON reports stored on disk.

    Scans ~/Desktop/Alpha Hive/ for analysis-{TICKER}-ml-{DATE}.json files
    and returns a summary sorted by ticker then date (newest first).

    Returns:
        str: JSON object with keys:
            - total (int): number of reports found
            - reports (list): each entry has ticker, date, file, size_kb
    """
    try:
        files = sorted(_HIVE_DIR.glob("analysis-*-ml-*.json"))
        reports = []
        for f in files:
            parts = f.stem.split("-ml-")
            if len(parts) != 2:
                continue
            ticker_part = parts[0].replace("analysis-", "")
            reports.append({
                "ticker":   ticker_part,
                "date":     parts[1],
                "file":     f.name,
                "size_kb":  round(f.stat().st_size / 1024, 1),
            })
        # Sort newest date first within each ticker
        reports.sort(key=lambda r: (r["ticker"], r["date"]), reverse=True)
        if not reports:
            return _ok({
                "message": "No analysis reports found.",
                "path": str(_HIVE_DIR),
            })
        return _ok({"total": len(reports), "reports": reports})
    except Exception as e:
        return _err(str(e))


# ─── Tool 2 · Full Analysis JSON ──────────────────────────────────────────────

@mcp.tool(
    name="alphahive_get_analysis",
    annotations={
        "title": "Get Full Alpha Hive Analysis",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def alphahive_get_analysis(params: TickerDateInput) -> str:
    """Retrieve the complete Alpha Hive analysis JSON for a ticker.

    Reads analysis-{TICKER}-ml-{DATE}.json from ~/Desktop/Alpha Hive/.
    If date_str is omitted, the most-recently-modified report is used.

    Returns the entire JSON object, including swarm_results, advanced_analysis,
    macro_context, signals, and all per-bee outputs. Use alphahive_get_swarm_scores
    for a lighter, focused summary.

    Args:
        params (TickerDateInput):
            - ticker (str): e.g. "NVDA"
            - date_str (Optional[str]): e.g. "2026-03-14"

    Returns:
        str: Full analysis JSON (can be large — 50–200 KB).
    """
    try:
        d = params.date_str or _latest_date(params.ticker)
        if not d:
            return _err(f"No analysis report found for {params.ticker}.")
        return _ok(_load_json(params.ticker, d))
    except FileNotFoundError as e:
        return _err(str(e))
    except Exception as e:
        return _err(str(e))


# ─── Tool 3 · Swarm Scores ────────────────────────────────────────────────────

@mcp.tool(
    name="alphahive_get_swarm_scores",
    annotations={
        "title": "Get Swarm Scores + Confidence",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def alphahive_get_swarm_scores(params: TickerDateInput) -> str:
    """Extract the 7-bee swarm scores and confidence calibration from a report.

    Returns a focused, lightweight summary: composite score, individual bee
    scores, confidence band, win probability, and stop-loss levels.

    Args:
        params (TickerDateInput):
            - ticker (str): required
            - date_str (Optional[str]): defaults to latest

    Returns:
        str: JSON with:
            - ticker, date
            - composite_score (float 0-10)
            - bias (str): "bullish" / "bearish" / "neutral"
            - bee_scores (dict): per-bee score, sentiment, weight
            - confidence_calibration: band [lo,hi], band_width, discrimination, dimension_std
            - win_probability_pct (float)
            - sample_size (int): historical n — caveat if < 10
            - stop_loss (dict): conservative / moderate / aggressive levels
    """
    try:
        d = params.date_str or _latest_date(params.ticker)
        if not d:
            return _err(f"No analysis report found for {params.ticker}.")
        data = _load_json(params.ticker, d)

        sr   = data.get("swarm_results", {})
        aa   = data.get("advanced_analysis", {})
        cc   = sr.get("confidence_calibration", {})
        prob = aa.get("probability_analysis", {})
        hist = aa.get("historical_analysis", {}).get("expected_returns", {})

        bee_scores: dict = {}
        for key, val in sr.items():
            if isinstance(val, dict) and "score" in val:
                bee_scores[key] = {
                    "score":     val.get("score"),
                    "sentiment": val.get("sentiment"),
                    "weight":    val.get("weight"),
                }

        n = hist.get("sample_size")
        result = {
            "ticker":          params.ticker,
            "date":            d,
            "composite_score": sr.get("composite_score"),
            "bias":            sr.get("bias"),
            "bee_scores":      bee_scores,
            "confidence_calibration": {
                "confidence_band": cc.get("confidence_band"),
                "band_width":      cc.get("band_width"),
                "discrimination":  cc.get("discrimination"),
                "dimension_std":   cc.get("dimension_std"),
            },
            "win_probability_pct": prob.get("win_probability_pct"),
            "sample_size":         n,
            "sample_caveat": (
                f"⚠️ n={n}, 样本量不足10条，统计意义有限" if n is not None and int(n) < 10 else None
            ),
            "stop_loss": aa.get("position_management", {}).get("stop_loss", {}),
        }
        return _ok(result)
    except FileNotFoundError as e:
        return _err(str(e))
    except Exception as e:
        return _err(str(e))


# ─── Tool 4 · GEX Snapshot ────────────────────────────────────────────────────

@mcp.tool(
    name="alphahive_get_gex",
    annotations={
        "title": "Get Dealer GEX from Analysis",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def alphahive_get_gex(params: TickerDateInput) -> str:
    """Extract the Dealer GEX (Gamma Exposure) snapshot from an analysis report.

    Reads advanced_analysis.dealer_gex which is computed by DealerGEXAnalyzer
    using Black-Scholes. Returns regime, flip point, call/put walls.

    Args:
        params (TickerDateInput): ticker + optional date_str

    Returns:
        str: JSON with:
            - total_gex (float): net gamma exposure in $ millions
            - regime (str): "positive_gex" | "negative_gex"
            - gex_flip (float): price where GEX changes sign
            - largest_call_wall (float): highest OI call strike
            - largest_put_wall (float): highest OI put strike
            - interpretation (str): human-readable regime explanation
    """
    try:
        d = params.date_str or _latest_date(params.ticker)
        if not d:
            return _err(f"No analysis report found for {params.ticker}.")
        data  = _load_json(params.ticker, d)
        dgex  = data.get("advanced_analysis", {}).get("dealer_gex", {}) or {}

        if not dgex or float(dgex.get("total_gex", 0) or 0) == 0.0:
            return _ok({
                "ticker":         params.ticker,
                "date":           d,
                "total_gex":      0.0,
                "regime":         None,
                "gex_flip":       None,
                "largest_call_wall": None,
                "largest_put_wall":  None,
                "interpretation": "⚠️ GEX数据缺失 — 请重新运行 alpha_hive.py 采集期权链数据",
            })

        regime = dgex.get("regime", "")
        interp = {
            "positive_gex": "✅ 正GEX：做市商持净多gamma，价格被压制在区间内，波动率偏低",
            "negative_gex": "⚠️ 负GEX：做市商持净空gamma，价格走势被放大，尾部风险上升",
        }.get(regime, "中性GEX")

        return _ok({
            "ticker":            params.ticker,
            "date":              d,
            "total_gex":         dgex.get("total_gex"),
            "regime":            regime,
            "gex_flip":          dgex.get("gex_flip"),
            "largest_call_wall": dgex.get("largest_call_wall"),
            "largest_put_wall":  dgex.get("largest_put_wall"),
            "interpretation":    interp,
        })
    except FileNotFoundError as e:
        return _err(str(e))
    except Exception as e:
        return _err(str(e))


# ─── Tool 5 · Live Options Snapshot ──────────────────────────────────────────

@mcp.tool(
    name="alphahive_get_options_snapshot",
    annotations={
        "title": "Get Live Options Snapshot (yfinance)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def alphahive_get_options_snapshot(params: TickerInput) -> str:
    """Fetch a live options market snapshot for a ticker via yfinance.

    Uses the nearest expiry options chain to compute Put/Call ratios,
    highest-OI strikes, and ATM implied volatility.

    NOTE: Requires live network access on the machine running this server.

    Args:
        params (TickerInput): ticker symbol

    Returns:
        str: JSON with:
            - nearest_expiry (str): e.g. "2026-03-21"
            - put_call_oi_ratio (float): total put OI / total call OI
            - put_call_vol_ratio (float): today's put vol / call vol
            - total_call_oi, total_put_oi (int)
            - total_call_volume, total_put_volume (int)
            - highest_oi_call (dict): strike, openInterest, impliedVolatility
            - highest_oi_put (dict): strike, openInterest, impliedVolatility
    """
    try:
        def _fetch():
            tkr  = yf.Ticker(params.ticker)
            exps = tkr.options
            if not exps:
                return None, None, None
            chain = tkr.option_chain(exps[0])
            return exps[0], chain.calls, chain.puts

        exp, calls, puts = await asyncio.to_thread(_fetch)
        if calls is None:
            return _err(f"No options data available for {params.ticker}.")

        c_oi  = int(calls["openInterest"].fillna(0).sum())
        p_oi  = int(puts["openInterest"].fillna(0).sum())
        c_vol = int(calls["volume"].fillna(0).sum())
        p_vol = int(puts["volume"].fillna(0).sum())

        # dropna guard: nlargest crashes if ALL openInterest values are NaN
        calls_valid = calls.dropna(subset=["openInterest"])
        puts_valid  = puts.dropna(subset=["openInterest"])
        top_c = [
            _native(r)
            for r in calls_valid.nlargest(1, "openInterest")[
                ["strike", "openInterest", "impliedVolatility"]
            ].to_dict("records")
        ]
        top_p = [
            _native(r)
            for r in puts_valid.nlargest(1, "openInterest")[
                ["strike", "openInterest", "impliedVolatility"]
            ].to_dict("records")
        ]

        return _ok({
            "ticker":             params.ticker,
            "nearest_expiry":     exp,
            "put_call_oi_ratio":  round(p_oi  / c_oi,  3) if c_oi  else None,
            "put_call_vol_ratio": round(p_vol / c_vol, 3) if c_vol else None,
            "total_call_oi":      c_oi,
            "total_put_oi":       p_oi,
            "total_call_volume":  c_vol,
            "total_put_volume":   p_vol,
            "highest_oi_call":    top_c[0] if top_c else None,
            "highest_oi_put":     top_p[0] if top_p else None,
        })
    except Exception as e:
        return _err(str(e))


# ─── Tool 6 · Live Quote ──────────────────────────────────────────────────────

@mcp.tool(
    name="alphahive_get_quote",
    annotations={
        "title": "Get Live Stock Quote (yfinance)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def alphahive_get_quote(params: TickerInput) -> str:
    """Fetch the current stock price and key fundamentals via yfinance.

    Returns real-time price, intraday change, volume, market cap, P/E ratios,
    beta, 52-week range, analyst target, short interest, and sector info.

    Args:
        params (TickerInput): ticker symbol

    Returns:
        str: JSON with:
            - currentPrice, previousClose, change, change_pct
            - volume, averageVolume
            - marketCap, trailingPE, forwardPE, forwardEps
            - beta, fiftyTwoWeekLow, fiftyTwoWeekHigh
            - targetMeanPrice, recommendationKey
            - shortName, sector, industry
            - short_interest_pct (derived from sharesShort / floatShares)
    """
    try:
        def _fetch():
            return yf.Ticker(params.ticker).info

        info = await asyncio.to_thread(_fetch)

        wanted = [
            "currentPrice", "previousClose", "open", "dayLow", "dayHigh",
            "volume", "averageVolume", "marketCap", "trailingPE", "forwardPE",
            "beta", "fiftyTwoWeekLow", "fiftyTwoWeekHigh", "targetMeanPrice",
            "recommendationKey", "shortName", "sector", "industry",
            "forwardEps", "dividendYield", "floatShares", "sharesShort",
        ]
        result: dict = {"ticker": params.ticker}
        for k in wanted:
            v = info.get(k)
            if v is not None:
                result[k] = v

        if result.get("currentPrice") and result.get("previousClose"):
            chg = result["currentPrice"] - result["previousClose"]
            result["change"]     = round(chg, 4)
            result["change_pct"] = round(chg / result["previousClose"] * 100, 2)

        if result.get("sharesShort") and result.get("floatShares"):
            result["short_interest_pct"] = round(
                result["sharesShort"] / result["floatShares"] * 100, 2
            )

        return _ok(result)
    except Exception as e:
        return _err(str(e))


# ─── Tool 7 · Price History ───────────────────────────────────────────────────

@mcp.tool(
    name="alphahive_get_price_history",
    annotations={
        "title": "Get OHLCV Price History (yfinance)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def alphahive_get_price_history(params: PriceHistoryInput) -> str:
    """Fetch OHLCV candlestick price history for a ticker via yfinance.

    Args:
        params (PriceHistoryInput):
            - ticker (str): required
            - period (str): '1d','5d','1mo','3mo','6mo','1y','2y','5y' (default '1mo')
            - interval (str): '1m','5m','15m','1h','1d','1wk','1mo' (default '1d')

    Returns:
        str: JSON with:
            - ticker, period, interval, count
            - candles (list): [{date, open, high, low, close, volume}, ...]
    """
    try:
        def _fetch():
            return yf.Ticker(params.ticker).history(
                period=params.period, interval=params.interval
            )

        hist = await asyncio.to_thread(_fetch)
        if hist.empty:
            return _err(f"No price history found for {params.ticker}.")

        candles = [
            {
                "date":   str(idx.date()) if hasattr(idx, "date") else str(idx),
                "open":   round(float(row["Open"]),   4),
                "high":   round(float(row["High"]),   4),
                "low":    round(float(row["Low"]),    4),
                "close":  round(float(row["Close"]),  4),
                "volume": int(row["Volume"]),
            }
            for idx, row in hist.iterrows()
        ]
        return _ok({
            "ticker":   params.ticker,
            "period":   params.period,
            "interval": params.interval,
            "count":    len(candles),
            "candles":  candles,
        })
    except Exception as e:
        return _err(str(e))


# ─── Tool 8 · SEC Insider Trades ─────────────────────────────────────────────

@mcp.tool(
    name="alphahive_get_insider_trades",
    annotations={
        "title": "Get SEC Form 4 Insider Trades",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def alphahive_get_insider_trades(params: InsiderTradesInput) -> str:
    """Fetch recent SEC Form 4 insider trade filings for a ticker via EDGAR.

    Queries the SEC EDGAR full-text search API for Form 4 filings within
    the specified lookback window. Returns filing metadata; use the accession
    URLs for full XML detail.

    Args:
        params (InsiderTradesInput):
            - ticker (str): required
            - days_back (int): default 90, max 365

    Returns:
        str: JSON with:
            - ticker, period, total_found
            - filings (list, up to 20): filed_at, entity_name, form, accession, url
    """
    try:
        end_dt   = date.today()
        start_dt = end_dt - timedelta(days=params.days_back)
        url = (
            f"https://efts.sec.gov/LATEST/search-index"
            f"?q=%22{params.ticker}%22"
            f"&dateRange=custom&startdt={start_dt}&enddt={end_dt}&forms=4"
        )

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "AlphaHive-MCP contact@alphahive.local"},
            )
            resp.raise_for_status()
            data = resp.json()

        hits = data.get("hits", {}).get("hits", [])
        filings = []
        for h in hits[:20]:
            src = h.get("_source", {})
            acc = src.get("accession_no", "").replace("-", "")
            eid = src.get("entity_id", "")
            filings.append({
                "filed_at":    src.get("file_date"),
                "entity_name": src.get("entity_name"),
                "form":        src.get("form_type"),
                "accession":   src.get("accession_no"),
                "url": (
                    f"https://www.sec.gov/Archives/edgar/data/{eid}/{acc}/"
                    if eid and acc else None
                ),
            })

        return _ok({
            "ticker":      params.ticker,
            "period":      f"{start_dt} to {end_dt}",
            "total_found": data.get("hits", {}).get("total", {}).get("value", 0),
            "filings":     filings,
        })
    except httpx.HTTPStatusError as e:
        return _err(f"SEC EDGAR error: HTTP {e.response.status_code}")
    except Exception as e:
        return _err(str(e))


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run()
