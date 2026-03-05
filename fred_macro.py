"""
🐝 Alpha Hive - FRED 宏观经济层 (P5)
为 GuardBeeSentinel 提供宏观环境过滤器

数据源（免费，无需 API Key）：
- ^VIX → CBOE 恐慌指数（yfinance）
- ^TNX → 10年期美债收益率（yfinance）
- ^FVX → 5年期美债收益率（yfinance，收益率曲线 fallback）
- ^DXY / DX-Y.NYB → 美元指数（yfinance）
- ^GSPC → 标普 500（yfinance，判断大盘环境）
- TLT → 长债 ETF（债市情绪）
- 11 SPDR 板块 ETF → 板块轮动跟踪

可选 FRED API（免费注册 fred.stlouisfed.org 获取 key）：
- 设置环境变量 FRED_API_KEY 可解锁 CPI、PMI、2Y 国债收益率等
"""

import logging
import os
import time
import threading
from typing import Dict, Tuple

_log = logging.getLogger("alpha_hive.fred_macro")


def _load_fred_key() -> str:
    """加载 FRED API Key：环境变量 > ~/.alpha_hive_fred_key 文件"""
    # 1. 环境变量
    key = os.environ.get("FRED_API_KEY", "").strip()
    if key:
        return key
    # 2. 文件
    key_file = os.path.expanduser("~/.alpha_hive_fred_key")
    try:
        with open(key_file) as f:
            key = f.read().strip()
            if key:
                return key
    except (OSError, UnicodeDecodeError):
        pass
    return ""

_CACHE: Dict = {}
_CACHE_TS: float = 0.0
try:
    from config import CACHE_CONFIG as _CC
    _CACHE_TTL = _CC["ttl"].get("fred_macro", 1800)
except (ImportError, KeyError):
    _CACHE_TTL = 1800
_lock = threading.Lock()


def get_macro_context() -> Dict:
    """
    获取当前宏观经济环境快照

    Returns:
        {
            "macro_regime": "risk_on"/"risk_off"/"neutral",
            "macro_score": float (0-10, 10=极度风险偏好),
            "vix": float,
            "vix_regime": "low"/"elevated"/"high"/"spike",
            "treasury_10y": float,
            "rate_environment": "low"/"rising"/"high",
            "dollar_trend": "strong"/"weak"/"neutral",
            "market_trend": "bull"/"bear"/"neutral",
            "macro_headwinds": list[str],
            "macro_tailwinds": list[str],
            "summary": str,
            "data_source": str,
        }
    """
    global _CACHE, _CACHE_TS

    with _lock:
        if _CACHE and (time.time() - _CACHE_TS) < _CACHE_TTL:
            return _CACHE

    result = _fetch_macro_data()

    with _lock:
        _CACHE = result
        _CACHE_TS = time.time()

    return result


def _fetch_macro_data() -> Dict:
    """内部：实际拉取宏观数据"""

    base = {
        "macro_regime": "neutral",
        "macro_score": 5.0,
        "vix": 20.0,
        "vix_regime": "elevated",
        "treasury_10y": 4.5,
        "rate_environment": "high",
        "dollar_trend": "neutral",
        "market_trend": "neutral",
        "macro_headwinds": [],
        "macro_tailwinds": [],
        "summary": "宏观数据不可用（降级到默认值）",
        "data_source": "fallback",
    }

    try:
        import yfinance as yf

        symbols = {
            "VIX":    "^VIX",
            "TNX":    "^TNX",
            "FVX":    "^FVX",      # 5Y Treasury（收益率曲线 fallback）
            "DXY":    "DX-Y.NYB",
            "SPX":    "^GSPC",
            "TLT":    "TLT",
        }

        data = {}
        for name, sym in symbols.items():
            try:
                t = yf.Ticker(sym)
                hist = t.history(period="5d", interval="1d")
                if not hist.empty:
                    data[name] = {
                        "last": float(hist["Close"].iloc[-1]),
                        "prev": float(hist["Close"].iloc[-2]) if len(hist) >= 2 else float(hist["Close"].iloc[-1]),
                        "change_pct": 0.0,
                    }
                    if data[name]["prev"] != 0:
                        data[name]["change_pct"] = (data[name]["last"] / data[name]["prev"] - 1) * 100
            except Exception as e:
                _log.debug("宏观数据获取失败 %s: %s", sym, e)

        if not data:
            return base

        # ---- VIX 分析 ----
        vix = data.get("VIX", {}).get("last", 20.0)
        vix_change = data.get("VIX", {}).get("change_pct", 0.0)

        if vix < 15:
            vix_regime = "low"
        elif vix < 20:
            vix_regime = "moderate"
        elif vix < 30:
            vix_regime = "elevated"
        elif vix < 40:
            vix_regime = "high"
        else:
            vix_regime = "spike"

        # ---- 10Y 利率分析 ----
        tnx = data.get("TNX", {}).get("last", 4.5)
        tnx_change = data.get("TNX", {}).get("change_pct", 0.0)

        if tnx < 3.0:
            rate_env = "low"
        elif tnx < 4.5:
            rate_env = "moderate"
        else:
            rate_env = "high"

        # ---- 美元趋势 ----
        dxy_change = data.get("DXY", {}).get("change_pct", 0.0)
        if dxy_change > 0.3:
            dollar_trend = "strong"
        elif dxy_change < -0.3:
            dollar_trend = "weak"
        else:
            dollar_trend = "neutral"

        # ---- 大盘趋势（5日）----
        spx_change = data.get("SPX", {}).get("change_pct", 0.0)
        if spx_change > 0.5:
            market_trend = "bull"
        elif spx_change < -0.5:
            market_trend = "bear"
        else:
            market_trend = "neutral"

        # ---- 收益率曲线（2Y-10Y 利差）----
        # 优先 FRED 2Y（精确），fallback 用 5Y 推算
        treasury_2y = None
        yield_spread = None
        yield_curve = "unknown"
        # fred_data 稍后获取，此处先用 5Y fallback
        fvx = data.get("FVX", {}).get("last")
        if fvx is not None and tnx > 0:
            # 5Y 近似 2Y：通常 2Y 比 5Y 高 20-40bp（扁平化时差更小）
            approx_2y = fvx + 0.15  # 保守近似
            treasury_2y = round(approx_2y, 3)
            yield_spread = round((tnx - approx_2y) * 100, 1)  # bp
            if yield_spread < -10:
                yield_curve = "inverted"
            elif yield_spread < 20:
                yield_curve = "flat"
            else:
                yield_curve = "normal"

        # ---- FRED API（提前获取以修正 2Y 曲线数据）----
        fred_data = {}
        fred_key = _load_fred_key()
        if fred_key:
            fred_data = _fetch_fred_series(fred_key)
            # FRED 精确 2Y 覆盖 5Y 近似
            if fred_data.get("treasury_2y") is not None:
                treasury_2y = fred_data["treasury_2y"]
                yield_spread = round((tnx - treasury_2y) * 100, 1)
                if yield_spread < -10:
                    yield_curve = "inverted"
                elif yield_spread < 20:
                    yield_curve = "flat"
                else:
                    yield_curve = "normal"

        # ---- 板块轮动 ----
        sector_rotation = _fetch_sector_rotation(yf)

        # ---- 宏观综合评分（0-10）----
        # 越多顺风 → 分越高
        score = 5.0

        # VIX 贡献
        if vix_regime == "low":
            score += 2.0
        elif vix_regime == "moderate":
            score += 1.0
        elif vix_regime == "high":
            score -= 1.5
        elif vix_regime == "spike":
            score -= 3.0

        # 利率贡献（高利率压制成长股）
        if rate_env == "low":
            score += 1.0
        elif rate_env == "high":
            score -= 1.0

        # 大盘贡献
        if market_trend == "bull":
            score += 1.0
        elif market_trend == "bear":
            score -= 1.5

        # 美元贡献（强美元压制非美收益）
        if dollar_trend == "strong":
            score -= 0.5
        elif dollar_trend == "weak":
            score += 0.5

        # 收益率曲线贡献
        if yield_curve == "inverted":
            score -= 1.5
        elif yield_curve == "flat":
            score -= 0.5

        score = max(1.0, min(10.0, score))

        # ---- 宏观政体判断 ----
        if score >= 7:
            macro_regime = "risk_on"
        elif score <= 4:
            macro_regime = "risk_off"
        else:
            macro_regime = "neutral"

        # ---- 顺逆风汇总 ----
        headwinds = []
        tailwinds = []

        if vix_regime in ("high", "spike"):
            headwinds.append(f"VIX {vix:.1f}（市场恐慌{'' if vix_regime=='high' else '极度'}升温）")
        if vix_change > 10:
            headwinds.append(f"VIX 单日飙升 {vix_change:+.1f}%（波动率冲击）")
        if rate_env == "high" and tnx_change > 2:
            headwinds.append(f"10Y 利率 {tnx:.2f}% 持续走高（压制成长估值）")
        if dollar_trend == "strong":
            headwinds.append(f"美元走强（新兴市场 + 大宗商品承压）")
        if market_trend == "bear":
            headwinds.append(f"大盘下行（{spx_change:+.1f}%，贝塔风险放大）")

        if yield_curve == "inverted":
            headwinds.append(f"收益率曲线倒挂（2Y-10Y利差{yield_spread:+.0f}bp，衰退信号）")
        elif yield_curve == "flat":
            headwinds.append(f"收益率曲线趋平（2Y-10Y利差{yield_spread:+.0f}bp）")

        if vix_regime in ("low", "moderate"):
            tailwinds.append(f"VIX {vix:.1f}（低波动，风险偏好良好）")
        if market_trend == "bull":
            tailwinds.append(f"大盘上行（{spx_change:+.1f}%，贝塔顺风）")
        if dollar_trend == "weak":
            tailwinds.append("美元偏弱（出口商 + 大宗商品受益）")
        if rate_env == "low":
            tailwinds.append(f"低利率环境（成长股估值友好）")

        # ---- FRED CPI/FFR 补充评分（FRED 数据已在 score 之前获取）----
        if fred_data:
            if fred_data.get("cpi_yoy") is not None:
                cpi = fred_data["cpi_yoy"]
                if cpi > 4.0:
                    headwinds.append(f"CPI同比 {cpi:.1f}%（通胀偏高，加息压力仍存）")
                    score -= 0.5
                elif cpi > 2.5:
                    headwinds.append(f"CPI同比 {cpi:.1f}%（通胀略偏高）")
                else:
                    tailwinds.append(f"CPI同比 {cpi:.1f}%（通胀受控，货币政策友好）")
                    score += 0.3
                score = max(1.0, min(10.0, score))
            if fred_data.get("fed_funds_rate") is not None:
                ffr = fred_data["fed_funds_rate"]
                if ffr >= 5.0:
                    headwinds.append(f"联邦基金利率 {ffr:.2f}%（高利率压制成长估值）")
                elif ffr <= 2.0:
                    tailwinds.append(f"联邦基金利率 {ffr:.2f}%（宽松环境）")

        summary_parts = [
            f"VIX {vix:.1f}({vix_regime})",
            f"10Y {tnx:.2f}%",
            f"大盘{spx_change:+.1f}%",
            f"宏观:{macro_regime}",
        ]
        if yield_curve != "unknown":
            yc_label = {"normal": "正常", "flat": "趋平", "inverted": "倒挂"}
            summary_parts.append(f"曲线:{yc_label.get(yield_curve, yield_curve)}")
        if fred_data.get("cpi_yoy") is not None:
            summary_parts.append(f"CPI同比{fred_data['cpi_yoy']:.1f}%")

        return {
            "macro_regime": macro_regime,
            "macro_score": round(score, 1),
            "vix": round(vix, 2),
            "vix_change_pct": round(vix_change, 2),
            "vix_regime": vix_regime,
            "treasury_10y": round(tnx, 3),
            "treasury_2y": treasury_2y,
            "yield_spread": yield_spread,
            "yield_curve": yield_curve,
            "rate_environment": rate_env,
            "dollar_trend": dollar_trend,
            "market_trend": market_trend,
            "spx_change_pct": round(spx_change, 2),
            "sector_rotation": sector_rotation,
            "macro_headwinds": headwinds,
            "macro_tailwinds": tailwinds,
            "fred_extras": fred_data,
            "cpi_yoy": fred_data.get("cpi_yoy"),
            "unemployment": fred_data.get("unemployment"),
            "fed_funds_rate": fred_data.get("fed_funds_rate"),
            "summary": " | ".join(summary_parts),
            "data_source": "yfinance" + ("+fred" if fred_data else ""),
        }

    except ImportError:
        _log.warning("yfinance 不可用，宏观数据降级")
        return base
    except Exception as e:
        _log.warning("宏观数据获取失败: %s", e)
        return base


def _fetch_fred_series(api_key: str) -> Dict:
    """从 FRED API 获取 CPI（同比）、失业率等月度数据（使用 requests 解决 macOS SSL 问题）"""
    result = {}
    try:
        import requests as _req
        base = "https://api.stlouisfed.org/fred/series/observations"

        # CPI 同比：取最近 13 个月做真正 YoY（而非月环比年化）
        r = _req.get(base, params={
            "series_id": "CPIAUCSL", "api_key": api_key,
            "file_type": "json", "sort_order": "desc", "limit": "13"
        }, timeout=8)
        if r.ok:
            obs = r.json().get("observations", [])
            if len(obs) >= 13:
                v_now = float(obs[0]["value"])
                v_year_ago = float(obs[12]["value"])
                result["cpi_yoy"] = round((v_now / v_year_ago - 1) * 100, 2)
                result["cpi_date"] = obs[0]["date"]

        # 失业率：最新值
        r2 = _req.get(base, params={
            "series_id": "UNRATE", "api_key": api_key,
            "file_type": "json", "sort_order": "desc", "limit": "1"
        }, timeout=8)
        if r2.ok:
            obs2 = r2.json().get("observations", [])
            if obs2:
                result["unemployment"] = float(obs2[0]["value"])
                result["unemployment_date"] = obs2[0]["date"]

        # 联邦基金利率（实际有效利率）
        r3 = _req.get(base, params={
            "series_id": "DFF", "api_key": api_key,
            "file_type": "json", "sort_order": "desc", "limit": "1"
        }, timeout=8)
        if r3.ok:
            obs3 = r3.json().get("observations", [])
            if obs3:
                result["fed_funds_rate"] = float(obs3[0]["value"])

        # 2Y 国债收益率（收益率曲线精确数据）
        r4 = _req.get(base, params={
            "series_id": "DGS2", "api_key": api_key,
            "file_type": "json", "sort_order": "desc", "limit": "1"
        }, timeout=8)
        if r4.ok:
            obs4 = r4.json().get("observations", [])
            if obs4 and obs4[0].get("value", ".") != ".":
                result["treasury_2y"] = float(obs4[0]["value"])

    except Exception as e:
        _log.debug("FRED API 调用失败: %s", e)
    return result


_SECTOR_ETFS = {
    "XLK": "科技", "XLV": "医疗", "XLE": "能源",
    "XLF": "金融", "XLI": "工业", "XLY": "可选消费",
    "XLP": "必需消费", "XLU": "公用事业", "XLRE": "房地产",
    "XLC": "通信", "XLB": "材料",
}

# 板块 ETF → config.WATCHLIST sector 映射
_SECTOR_TO_ETF = {
    "Technology": "XLK", "Healthcare": "XLV", "Energy": "XLE",
    "Financial": "XLF", "Industrial": "XLI", "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP", "Utilities": "XLU", "Real Estate": "XLRE",
    "Communication": "XLC", "Materials": "XLB",
    # 别名映射
    "Automotive": "XLY", "Biotech": "XLV", "Fintech": "XLK",
    "Semiconductor": "XLK", "E-Commerce": "XLY",
}


def _fetch_sector_rotation(yf_module=None) -> Dict:
    """获取 11 个 SPDR 板块 ETF 的 5 日表现，返回板块轮动数据"""
    result = {"hot": [], "cold": [], "full": {}}
    try:
        yf = yf_module
        if yf is None:
            import yfinance as yf
        tickers = list(_SECTOR_ETFS.keys())
        performances = []
        for etf in tickers:
            try:
                t = yf.Ticker(etf)
                hist = t.history(period="5d", interval="1d")
                if hist is not None and len(hist) >= 2:
                    first_close = float(hist["Close"].iloc[0])
                    last_close = float(hist["Close"].iloc[-1])
                    if first_close > 0:
                        chg = round((last_close / first_close - 1) * 100, 2)
                        name = _SECTOR_ETFS[etf]
                        performances.append((etf, name, chg))
                        result["full"][etf] = (name, chg)
            except Exception:
                pass
        if performances:
            performances.sort(key=lambda x: x[2], reverse=True)
            if len(performances) >= 6:
                result["hot"] = performances[:3]
                result["cold"] = performances[-3:]
            elif len(performances) >= 2:
                mid = len(performances) // 2
                result["hot"] = performances[:mid]
                result["cold"] = performances[mid:]
            else:
                result["hot"] = performances[:1]
                result["cold"] = []
    except Exception as e:
        _log.debug("板块轮动数据获取失败: %s", e)
    return result


def get_sector_etf_for_ticker(ticker: str) -> str:
    """根据 ticker 的板块返回对应的板块 ETF 代码"""
    try:
        from config import WATCHLIST
        sector = WATCHLIST.get(ticker, {}).get("sector", "")
        return _SECTOR_TO_ETF.get(sector, "")
    except (ImportError, KeyError):
        return ""


def get_macro_risk_adjustment(macro: Dict) -> Tuple:
    """
    将宏观数据转换为 GuardBeeSentinel 可用的风险调整因子

    Returns:
        (adjustment: float, description: str)
        adjustment: -2.0 ~ +1.5（对 guard score 的加减分）
    """
    regime = macro.get("macro_regime", "neutral")
    vix_regime = macro.get("vix_regime", "elevated")
    score = macro.get("macro_score", 5.0)

    if regime == "risk_off":
        if vix_regime == "spike":
            return (-2.0, f"宏观极度风险厌恶（VIX={macro.get('vix', 0):.0f}，全面减仓环境）")
        return (-1.2, f"宏观风险偏好走弱（{macro.get('summary', '')}）")
    elif regime == "risk_on":
        return (+1.0, f"宏观顺风环境（{macro.get('summary', '')}）")
    else:
        if vix_regime in ("high", "spike"):
            return (-0.5, f"宏观中性但波动偏高（VIX={macro.get('vix', 0):.0f}）")
        return (0.0, f"宏观中性（{macro.get('summary', '')}）")
