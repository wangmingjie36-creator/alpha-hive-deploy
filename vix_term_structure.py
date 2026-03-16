"""
VIX 波动率期限结构模块
━━━━━━━━━━━━━━━━━━━━━
数据源：
  1. vixcentral.com  — VX 月度期货曲线（M1~M8）
  2. yfinance ^VIX   — VIX 现货（30日隐含波动率）
  3. FRED via mcp-fred（可选）— VIXCLS 历史序列

期限结构含义：
  Contango     (M1 < M2 < M3...)  → 市场平静，Vol 卖方有利，月期权卖 Theta 胜率高
  Backwardation(M1 > M2 > M3...)  → 市场恐慌，期权买方有利，Tail hedge 价值提升
  Flat         (差异 < 2%)        → 过渡期，方向不明

对月期权交易者的含义：
  Contango + slope > 8%  → 卖期权（sell covered call / CSP）环境好
  Backwardation          → 买保护或方向性 debit spread
  VIX Spike > 30         → 经典 IV Crush 买入时机（财报后卖 IV）
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

_log = logging.getLogger("alpha_hive.vix_term_structure")

_CACHE_PATH = Path(__file__).parent / "cache" / "vix_term_structure.json"
_CACHE_TTL = 1800  # 30 分钟
_lock = threading.Lock()


def get_vix_term_structure(force_refresh: bool = False) -> Dict:
    """
    获取 VIX 期限结构。

    返回：
      {
        "spot_vix": float,               # VIX 现货（yfinance ^VIX）
        "futures": [float],              # M1-M8 期货（vixcentral）
        "m1": float, "m2": float,        # 前两月期货
        "structure": "contango"|"backwardation"|"flat",
        "contango_slope_pct": float,     # (M4-M1)/M1 * 100，正=contango 负=backwardation
        "m1_m2_spread": float,           # M2 - M1（正=contango，负=backwardation）
        "signal": str,                   # 可操作信号摘要
        "trading_implication": str,      # 对月期权的含义
        "timestamp": str,
        "source": str,
      }
    """
    with _lock:
        # ── 读取缓存 ─────────────────────────────────────────────────────
        if not force_refresh and _CACHE_PATH.exists():
            try:
                cached = json.loads(_CACHE_PATH.read_text())
                ts = datetime.fromisoformat(cached.get("timestamp", "2000-01-01"))
                if (datetime.now() - ts).total_seconds() < _CACHE_TTL:
                    return cached
            except Exception:
                pass

        result = _fetch_fresh()

        # 写缓存
        try:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_PATH.write_text(json.dumps(result, ensure_ascii=False))
        except Exception:
            pass

        return result


def _fetch_fresh() -> Dict:
    spot_vix = _get_spot_vix()
    futures = _get_vx_futures()

    result: Dict = {
        "spot_vix": spot_vix,
        "futures": futures,
        "m1": futures[0] if len(futures) > 0 else None,
        "m2": futures[1] if len(futures) > 1 else None,
        "structure": "unknown",
        "contango_slope_pct": None,
        "m1_m2_spread": None,
        "signal": "",
        "trading_implication": "",
        "timestamp": datetime.now().isoformat(),
        "source": "vixcentral+yfinance",
    }

    if len(futures) >= 2:
        m1, m2 = futures[0], futures[1]
        spread = m2 - m1
        result["m1_m2_spread"] = round(spread, 3)

        # 期限结构判断
        if spread > 0.5:
            result["structure"] = "contango"
        elif spread < -0.5:
            result["structure"] = "backwardation"
        else:
            result["structure"] = "flat"

    if len(futures) >= 4:
        m1, m4 = futures[0], futures[3]
        if m1 > 0:
            slope = (m4 - m1) / m1 * 100
            result["contango_slope_pct"] = round(slope, 2)

    # 生成可操作信号
    structure = result["structure"]
    vix = spot_vix or (futures[0] if futures else 0)
    slope = result.get("contango_slope_pct") or 0

    if structure == "contango":
        if slope > 8:
            result["signal"] = f"强 Contango (slope={slope:+.1f}%) — 卖 Vol 环境"
            result["trading_implication"] = (
                "月期权：CSP / Covered Call / Iron Condor 胜率高。"
                "做空波动率策略占优，避免 Debit Spread 买方。"
            )
        else:
            result["signal"] = f"弱 Contango (slope={slope:+.1f}%) — 平静市场"
            result["trading_implication"] = "温和卖方环境，可做轻度 CSP/CC，止损需控制。"
    elif structure == "backwardation":
        spread_val = abs(result.get("m1_m2_spread") or 0)
        result["signal"] = f"Backwardation (M1-M2={spread_val:.2f}) — 市场恐慌"
        result["trading_implication"] = (
            "月期权：买方有利 (Debit Spread / Long Put)。"
            "IV 高企时可考虑财报后 IV Crush 卖方策略。"
        )
    else:
        result["signal"] = "Flat — 期限结构无明显方向"
        result["trading_implication"] = "等待期限结构明确后再部署方向性期权。"

    # VIX 绝对水平修正
    if vix and vix > 30:
        result["signal"] += f" | VIX={vix:.1f} 极度恐慌区"
        result["trading_implication"] += " VIX>30 是经典 IV Crush 财报后卖方时机。"
    elif vix and vix < 15:
        result["signal"] += f" | VIX={vix:.1f} 极度平静"
        result["trading_implication"] += " VIX<15 期权极度便宜，Long Vol 性价比高。"

    return result


def _get_spot_vix() -> Optional[float]:
    """从 yfinance 获取 ^VIX 现货"""
    try:
        import yfinance as yf
        hist = yf.Ticker("^VIX").history(period="2d")
        if not hist.empty:
            return round(float(hist["Close"].iloc[-1]), 2)
    except Exception as e:
        _log.debug("VIX spot error: %s", e)
    return None


def _get_vx_futures() -> List[float]:
    """从 vixcentral.com 抓取 VX M1-M8 期货收盘价"""
    try:
        req = urllib.request.Request(
            "http://vixcentral.com",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        )
        html = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", errors="ignore")

        # previous_close_var = [M1, M2, M3, M4, M5, M6, M7, M8]
        match = re.search(r"previous_close_var\s*=\s*\[([\d.,\s]+)\]", html)
        if match:
            values = [float(v.strip()) for v in match.group(1).split(",") if v.strip()]
            return values
    except Exception as e:
        _log.debug("vixcentral fetch error: %s", e)
    return []


def format_vix_term_summary(data: Dict) -> str:
    """返回注入 LLM prompt 的简洁文字摘要"""
    if data.get("structure") == "unknown":
        return ""

    futures = data.get("futures", [])
    futures_str = " → ".join(f"{v:.2f}" for v in futures[:6]) if futures else "N/A"

    lines = [
        "【VIX 波动率期限结构】",
        f"VIX现货: {data.get('spot_vix', 'N/A')} | M1: {data.get('m1', 'N/A')} | M2: {data.get('m2', 'N/A')}",
        f"期限结构: {futures_str}",
        f"判断: {data.get('signal', '')}",
        f"月期权含义: {data.get('trading_implication', '')}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    data = get_vix_term_structure(force_refresh=True)
    print(format_vix_term_summary(data))
    import json as _j
    print("\nRaw:", _j.dumps(data, ensure_ascii=False, indent=2))
