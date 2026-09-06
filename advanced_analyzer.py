"""
🐝 Alpha Hive - 高级分析系统
支持行业对标、历史回溯、概率计算、止损止盈建议
"""

import logging as _logging
import json
import math
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import sqlite3
import statistics

_log = _logging.getLogger("alpha_hive.advanced_analyzer")

try:
    from resilience import NETWORK_ERRORS
except ImportError:
    NETWORK_ERRORS = (ConnectionError, TimeoutError, OSError, ValueError, KeyError)

# 动态导入期权分析模块
try:
    from options_analyzer import OptionsAgent
    OPTIONS_AGENT_AVAILABLE = True
except ImportError:
    OPTIONS_AGENT_AVAILABLE = False
    OptionsAgent = None


# ─────────────────────────────────────────────────────────────────
#  Dealer GEX Analyzer
#  yfinance 不返回 Greeks，用 Black-Scholes 自行计算每个 strike 的
#  gamma，再聚合出 Notional GEX profile（单位：百万美元 gamma exposure）
# ─────────────────────────────────────────────────────────────────

def _norm_pdf(x: float) -> float:
    """标准正态分布概率密度函数"""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    """标准正态累积分布函数（Abramowitz & Stegun 近似，误差 < 7.5e-8）"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """
    Black-Scholes gamma（call 和 put 的 gamma 相同）

    Args:
        S: 标的当前价格
        K: 行权价
        T: 到期年化时间（DTE / 365）
        r: 无风险利率（年化，e.g. 0.045）
        sigma: 隐含波动率（年化，e.g. 0.35 = 35%）

    Returns:
        gamma（每 $1 股价变动对应 delta 的变化量）
        返回 0.0 表示参数无效
    """
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        return _norm_pdf(d1) / (S * sigma * math.sqrt(T))
    except (ValueError, ZeroDivisionError):
        return 0.0


class DealerGEXAnalyzer:
    """
    做市商 Gamma Exposure（GEX）分析器

    做市商假设：
      - 做市商持有 call 的对冲头寸 → long gamma（正 GEX）
      - 做市商持有 put 的对冲头寸 → short gamma（负 GEX）
      - 净 GEX = call_gex - put_gex

    正 GEX：做市商需要"顺势对冲"（rally→卖，跌→买）→ 压制波动
    负 GEX：做市商需要"顺方向追"（rally→买，跌→卖）→ 放大波动

    GEX flip point：净 GEX = 0 的价格，波动从压制转放大的临界点
    """

    RISK_FREE_RATE = 0.045  # 美国10年期国债参考利率

    def __init__(self):
        try:
            from options_analyzer import OptionsDataFetcher
            self._fetcher = OptionsDataFetcher()
        except ImportError:
            self._fetcher = None

    # ── 核心计算 ──────────────────────────────────────────────────

    def _enrich_with_bs_gamma(
        self, contracts: List[Dict], S: float, option_type: str
    ) -> List[Dict]:
        """
        为每份合约注入 BS gamma（yfinance 不返回 gamma，需自行计算）
        原始 gamma 字段非零时保留，等于 0 则用 BS 覆盖。
        """
        enriched = []
        for c in contracts:
            raw_gamma = c.get("gamma", 0.0) or 0.0
            if raw_gamma != 0.0:
                enriched.append(c)
                continue

            K     = float(c.get("strike", 0) or 0)
            # ── v0.45.50：`or 30` 把 0DTE 变成了 30DTE ──
            # 下一行的 `max(dte, 0.5)` 才是真正的超短期守卫（注释也这么写）。
            # 但 `or 30` 在它之前生效：到期日当天 dte=0 → `or 30` → 30，
            # 于是 T 从 0.5/365=0.00137 变成 30/365=0.08219 —— **差 60 倍**，
            # BS gamma 被严重低估，恰好发生在 gamma 最大、pinning 最强的那一天。
            # 守卫写对了，被上游默认值架空（同 risk_engine:834 的 σ 守卫）。
            # 现在只把「键缺失/不可解析」当未知给 30，真实的 0 原样传给守卫。
            _dte_raw = c.get("dte")
            try:
                dte = float(_dte_raw) if _dte_raw is not None else 30.0
            except (TypeError, ValueError):
                dte = 30.0
            sigma = float(c.get("impliedVolatility", 0) or 0)
            T     = max(dte, 0.5) / 365.0  # 最小 0.5 天，避免超短期 gamma 爆炸

            if K <= 0 or sigma < 0.01:
                enriched.append(c)
                continue

            gamma = bs_gamma(S, K, T, self.RISK_FREE_RATE, sigma)
            enriched.append({**c, "gamma": gamma, "gamma_source": "bs"})
        return enriched

    def _notional_gex_per_strike(
        self, contracts: List[Dict], S: float, sign: float
    ) -> Dict[float, float]:
        """
        按行权价聚合 Notional GEX（百万美元）

        sign = +1 for calls（做市商 long gamma），-1 for puts（做市商 short gamma）

        GEX(K) = sign × price × 100 × gamma × OI
        """
        gex_by_strike: Dict[float, float] = {}
        for c in contracts:
            K      = float(c.get("strike", 0) or 0)
            gamma  = float(c.get("gamma", 0) or 0)
            oi     = float(c.get("openInterest", 0) or 0)
            if K <= 0 or gamma == 0 or oi == 0:
                continue
            notional = sign * S * 100 * gamma * oi / 1e6  # 百万美元
            gex_by_strike[K] = gex_by_strike.get(K, 0.0) + notional
        return gex_by_strike

    def _find_gex_flip(
        self, gex_profile: List[Dict], S: float
    ) -> Optional[float]:
        """
        寻找 GEX flip point：净 GEX 从正变负（或负变正）的最近行权价
        返回距离当前价最近的翻转行权价，无则返回 None
        """
        if len(gex_profile) < 2:
            return None

        # 取行权价升序
        sorted_profile = sorted(gex_profile, key=lambda x: x["strike"])
        prev_gex = sorted_profile[0]["net_gex"]
        flip_strikes = []
        for item in sorted_profile[1:]:
            curr_gex = item["net_gex"]
            if prev_gex * curr_gex < 0:  # 符号变化
                flip_strikes.append(item["strike"])
            prev_gex = curr_gex

        if not flip_strikes:
            return None
        # 返回距当前价最近的 flip
        return min(flip_strikes, key=lambda k: abs(k - S))

    def _calculate_flip_acceleration(
        self, gex_profile: List[Dict], S: float, gex_flip: Optional[float]
    ) -> Dict:
        """
        计算 GEX Flip 加速度 — 股价接近 flip point 时 GEX 变化速率

        dGEX/dPrice 斜率越陡，穿越 flip 后波动放大越快
        """
        if gex_flip is None or len(gex_profile) < 3:
            return {"acceleration": 0.0, "urgency": "low"}

        sorted_profile = sorted(gex_profile, key=lambda x: x["strike"])

        # 找 flip 附近的两个 strike
        for i in range(len(sorted_profile) - 1):
            k1 = sorted_profile[i]["strike"]
            k2 = sorted_profile[i + 1]["strike"]
            if k1 <= gex_flip <= k2:
                gex1 = sorted_profile[i]["net_gex"]
                gex2 = sorted_profile[i + 1]["net_gex"]
                dk = k2 - k1
                if dk > 0:
                    slope = (gex2 - gex1) / dk  # dGEX/dPrice
                    distance_pct = abs(S - gex_flip) / S * 100

                    if abs(slope) > 0.1 and distance_pct < 2:
                        urgency = "critical"
                    elif abs(slope) > 0.05 and distance_pct < 5:
                        urgency = "high"
                    elif distance_pct < 10:
                        urgency = "medium"
                    else:
                        urgency = "low"

                    return {
                        "acceleration": round(slope, 6),
                        "distance_to_flip_pct": round(distance_pct, 2),
                        "urgency": urgency,
                    }
                break

        return {"acceleration": 0.0, "urgency": "low"}

    def _vanna_stress_test(
        self, calls: List[Dict], puts: List[Dict], S: float,
        vol_shock: float = 0.05, total_gex: float = 0.0
    ) -> Dict:
        """
        Vanna 压力测试：当 IV 突变 vol_shock 时，GEX 如何变化

        Vanna = dDelta/dVol = dVega/dSpot
        在 vol 飙升时，Vanna 可以翻转 GEX 的符号

        Args:
            vol_shock: IV 变动幅度（默认 +5%）
        """
        def _bs_vanna(S, K, T, r, sigma):
            """Vanna = -e^(-d1²/2) * d2 / (S * sigma * sqrt(T) * sqrt(2π))"""
            if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
                return 0.0
            try:
                d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
                d2 = d1 - sigma * math.sqrt(T)
                return -math.exp(-0.5 * d1 ** 2) * d2 / (S * sigma * math.sqrt(T) * math.sqrt(2 * math.pi))
            except (ValueError, ZeroDivisionError):
                return 0.0

        r = self.RISK_FREE_RATE
        total_vanna_impact = 0.0
        strike_impacts = []

        for contracts, sign in [(calls, +1.0), (puts, -1.0)]:
            for c in contracts:
                K = float(c.get("strike", 0) or 0)
                # v0.45.53：与 L118 同修 —— `or 30` 把真实 0DTE 改写成 30 天，
                # 架空下一行的 `max(dte, 0.5)` 守卫（T 差 60 倍）。
                # 这是第三处，前两处修于 v0.45.50 时漏了本处，由 Phase 2 护栏抓到。
                _dte_v = c.get("dte")
                try:
                    dte = float(_dte_v) if _dte_v is not None else 30.0
                except (TypeError, ValueError):
                    dte = 30.0
                sigma = float(c.get("impliedVolatility", 0) or 0)
                oi = float(c.get("openInterest", 0) or 0)
                T = max(dte, 0.5) / 365.0

                if K <= 0 or sigma < 0.01 or oi == 0:
                    continue

                vanna = _bs_vanna(S, K, T, r, sigma)
                # Vanna impact on GEX when vol changes by vol_shock
                impact = sign * S * 100 * vanna * oi * vol_shock / 1e6
                total_vanna_impact += impact

                if abs(impact) > 0.01:
                    strike_impacts.append({"strike": K, "vanna_gex_delta": round(impact, 4)})

        # 排序找最受影响的行权价
        strike_impacts.sort(key=lambda x: abs(x["vanna_gex_delta"]), reverse=True)

        return {
            "vol_shock": vol_shock,
            "total_vanna_gex_shift": round(total_vanna_impact, 4),
            "can_flip_gex": abs(total_vanna_impact) > abs(total_gex) * 0.5 if total_gex != 0 else False,
            "top_affected_strikes": strike_impacts[:5],
            "interpretation": (
                f"IV+{vol_shock*100:.0f}%时GEX将变动{total_vanna_impact:+.2f}M$ — "
                + ("可能翻转GEX符号⚠️" if abs(total_vanna_impact) > 1.0 else "影响有限")
            ),
        }

    # ── 公开接口 ──────────────────────────────────────────────────

    def analyze(self, ticker: str, stock_price: float) -> Dict:
        """
        返回完整 Dealer GEX 分析结果：
          - total_gex: 全市场净 GEX（百万美元）
          - gex_profile: 每个行权价的 call/put/net GEX 列表
          - gex_flip: GEX flip point（价格稳定→放大的临界行权价）
          - largest_call_wall: call GEX 最大的行权价（阻力）
          - largest_put_wall: put GEX 绝对值最大的行权价（支撑）
          - regime: "positive_gex"（压制波动）| "negative_gex"（放大波动）
        """
        if self._fetcher is None:
            return {"error": "options_analyzer 未安装", "total_gex": 0.0}

        try:
            chain = self._fetcher.fetch_options_chain(ticker)
        except Exception as e:
            _log.warning("DealerGEX fetch_options_chain failed for %s: %s", ticker, e)
            return {"error": str(e), "total_gex": 0.0}

        calls_raw = chain.get("calls", [])
        puts_raw  = chain.get("puts",  [])

        if not calls_raw and not puts_raw:
            return {"error": "期权链为空", "total_gex": 0.0}

        # P0-1 (v0.38.0): 样本链不算 Dealer GEX——假链算出的 GEX/flip 全是噪声
        if chain.get("source") == "sample":
            _log.warning("[%s] 期权链为样本数据，跳过 Dealer GEX 计算", ticker)
            return {"error": "期权链为样本数据（真实链获取失败）", "total_gex": 0.0}

        S = stock_price
        if S <= 0:
            return {"error": "无效股价", "total_gex": 0.0}

        # 注入 BS gamma
        calls = self._enrich_with_bs_gamma(calls_raw, S, "call")
        puts  = self._enrich_with_bs_gamma(puts_raw,  S, "put")

        # 按 strike 聚合 GEX
        call_gex = self._notional_gex_per_strike(calls, S, sign=+1.0)
        put_gex  = self._notional_gex_per_strike(puts,  S, sign=-1.0)

        # 合并所有行权价
        all_strikes = sorted(set(call_gex.keys()) | set(put_gex.keys()))
        profile = []
        for K in all_strikes:
            cg = call_gex.get(K, 0.0)
            pg = put_gex.get(K, 0.0)
            profile.append({
                "strike":   K,
                "call_gex": round(cg, 4),
                "put_gex":  round(pg, 4),
                "net_gex":  round(cg + pg, 4),
            })

        total_gex = round(sum(p["net_gex"] for p in profile), 4)
        total_oi = sum(float(c.get("openInterest", 0) or 0) for c in calls_raw + puts_raw)

        # GEX walls（最大吸引力行权价）
        call_walls = sorted(profile, key=lambda x: x["call_gex"], reverse=True)
        put_walls  = sorted(profile, key=lambda x: x["put_gex"])  # put_gex 是负数
        largest_call_wall = call_walls[0]["strike"] if call_walls else None
        largest_put_wall  = put_walls[0]["strike"]  if put_walls  else None

        # GEX flip point
        gex_flip = self._find_gex_flip(profile, S)

        # Flip 加速度
        flip_accel = self._calculate_flip_acceleration(profile, S, gex_flip)

        # Vanna 压力测试（传入 total_gex 用于判断是否可翻转）
        vanna_stress = self._vanna_stress_test(calls, puts, S, total_gex=total_gex)

        # 机制判断
        regime = "positive_gex" if total_gex >= 0 else "negative_gex"

        # 仅保留 ±20% 行权价区间的 profile（避免极端 OTM 噪音）
        lo, hi = S * 0.80, S * 1.20
        profile_near = [p for p in profile if lo <= p["strike"] <= hi]

        return {
            "ticker":            ticker,
            "stock_price":       S,
            "total_gex":         total_gex,
            "gex_normalized_pct": round(total_gex / (S * total_oi / 1e6) * 100, 4) if total_oi > 0 else 0.0,
            "regime":            regime,
            "gex_flip":          gex_flip,
            "largest_call_wall": largest_call_wall,
            "largest_put_wall":  largest_put_wall,
            "gex_profile":       profile_near,
            "call_strikes":      len(call_gex),
            "put_strikes":       len(put_gex),
            "gamma_source":      "bs_computed",
            "flip_acceleration": flip_accel,
            "vanna_stress":      vanna_stress,
        }


class IndustryComparator:
    """行业对标分析"""

    def __init__(self):
        # 行业内的竞争对手定义
        self.industries = {
            "GPU": {
                "leader": "NVDA",
                "competitors": ["AMD", "INTC", "QCOM"],
                "metrics": ["market_cap", "pe_ratio", "dividend_yield", "revenue_growth"],
            },
            "Biotech": {
                "leader": "VKTX",
                "competitors": ["AMGN", "BIIB", "REGN"],
                "metrics": ["pipeline_stage", "cash_burn", "trial_success_rate"],
            },
            "EV": {
                "leader": "TSLA",
                "competitors": ["NIO", "XPev", "LI"],
                "metrics": ["production_capacity", "battery_tech", "margin"],
            },
        }

    def get_industry(self, ticker: str) -> Optional[str]:
        """获取标的所在行业"""
        for industry, data in self.industries.items():
            if ticker in [data["leader"]] + data["competitors"]:
                return industry
        return None

    def compare_with_peers(
        self, ticker: str, current_metrics: Dict
    ) -> Dict:
        """与同行竞争对手对标"""
        industry = self.get_industry(ticker)
        if not industry:
            return {}

        industry_data = self.industries[industry]
        leader = industry_data["leader"]
        competitors = industry_data["competitors"]

        comparison = {
            "industry": industry,
            "leader": leader,
            "position": "Leader" if ticker == leader else "Challenger",
            "competitors": competitors,
            "comparative_strength": self._calculate_strength(ticker, current_metrics),
            "competitive_advantages": self._analyze_advantages(ticker, industry),
            "competitive_threats": self._analyze_threats(ticker, industry),
        }

        return comparison

    def _calculate_strength(self, ticker: str, metrics: Dict) -> float:
        """计算竞争力评分（0-100）"""
        # 简化版本 - 可扩展为更复杂的评分
        if ticker == "NVDA":
            return 92  # CUDA 生态、市场地位
        elif ticker == "AMD":
            return 78  # 技术进步、成本竞争
        elif ticker == "INTC":
            return 65  # 传统优势削弱
        elif ticker == "VKTX":
            return 72  # GLP-1 领先
        elif ticker == "TSLA":
            return 88  # 成本、规模、技术
        elif ticker == "NIO":
            return 60  # 资金压力、销量差距
        return 50

    def _analyze_advantages(self, ticker: str, industry: str) -> List[str]:
        """分析竞争优势"""
        advantages = {
            "NVDA": [
                "CUDA 生态护城河（开发者黏性强）",
                "AI 芯片市场份额领先（90%+）",
                "研发投入最大（年 60 亿美元）",
                "品牌溢价（高端产品定价权强）",
            ],
            "AMD": [
                "成本竞争力（制造工艺成熟）",
                "性价比（相同性能便宜 15-20%）",
                "技术追赶速度快",
                "企业客户关系深厚",
            ],
            "TSLA": [
                "规模效应（全球产能最大）",
                "垂直整合（电池、芯片自研）",
                "品牌价值（消费者忠诚度高）",
                "数据积累（自驾数据优势）",
            ],
            "VKTX": [
                "GLP-1 领先地位（临床试验进展快）",
                "专利保护（独家技术）",
                "医生认可度高",
            ],
        }
        return advantages.get(ticker, [])

    def _analyze_threats(self, ticker: str, industry: str) -> List[str]:
        """分析竞争威胁"""
        threats = {
            "NVDA": [
                "AMD/INTC 追赶（工艺差距缩小）",
                "监管风险（中国禁令）",
                "开源替代方案（RISC-V）",
                "客户自研芯片（Meta、Google）",
            ],
            "AMD": [
                "NVDA 市场地位难撼动",
                "高端市场占有率低",
                "技术代差追赶困难",
            ],
            "TSLA": [
                "传统车厂电动化追赶（BYD、VW）",
                "新兴 EV 创企竞争（NIO、小鹏）",
                "利润率压力（降价竞争）",
                "供应链风险（芯片短缺）",
            ],
            "VKTX": [
                "大药厂进入（诺华、礼来）",
                "仿制药威胁（专利过期后）",
                "临床失败风险（Phase 3 风险大）",
            ],
        }
        return threats.get(ticker, [])


def _percentile(sorted_vals: List[float], p: float) -> float:
    """线性插值分位数（与 numpy 默认 `linear` 一致）；入参必须已升序。"""
    n = len(sorted_vals)
    if n == 0:
        raise ValueError("empty sample")
    k = (n - 1) * p / 100.0
    f = math.floor(k)
    c = min(f + 1, n - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


class HistoricalAnalyzer:
    """历史回溯：读 pheromone.db 里蜂群自己的预测与 T+7 真实结果（v0.45.132）。

    v0.45.132 之前这里是 **6 条手写记录**（NVDA 3 / VKTX 2 / TSLA 1，全是 2023 年
    的财报、全是 beat，2026-02-24 落笔后从未增补），按「拥挤度 ±10」匹配——
    对 30 只标的里 27 只**结构上不可能命中**；能命中的两只也只是一个常数
    （旧 generate_comprehensive_analysis 的「消息数 > 1000 → 63.5」）去撞两年半前
    的三条记录。同期 pheromone.db 已攒下 900+ 条核对过 T+7 收盘的真实预测。

    口径：
      · 收益 = close_t7 / price_at_predict − 1（干净收盘口径，与 v0.45.87
        feedback_loop.clean_t7 同源）。**不用 return_t7**——它对 SL/TP 方向单是
        钳位离场收益、对中性单是原始收益，混在一起没有意义。
      · 条件：同标的 + 同方向（蜂群当日 direction）。同方向样本 < MIN_SAMPLE 时
        退回同标的**不分方向**，结果里 basis 标明；仍不足则只返回样本数与 note，
        **不给任何分位数**——调用方必须按「不可得」渲染。
      · 只读连接；库不存在 / 读失败 → db_status 标出，结果同「样本不足」。
        看报告的人能从 db_status 分清「没库」与「没样本」。
    """

    MIN_SAMPLE = 20   # P10/P90 在 20 个样本上分别落在第 2/19 个点；再少分位数就是单点噪声
    RETURN_BASIS = "close_t7 / price_at_predict − 1"

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            from feedback_loop import PHEROMONE_DB_PATH   # 路径唯一真相在 feedback_loop
            db_path = PHEROMONE_DB_PATH
        self.db_path = Path(db_path)
        self._rows_cache: Optional[List[Dict]] = None
        self.db_status: Optional[str] = None
        self.skipped_rows = 0

    # ── 读库 ────────────────────────────────────────────────────────────
    def _load(self) -> Tuple[List[Dict], str]:
        try:
            exists = self.db_path.exists()
        except OSError as e:
            _log.warning("[HistoricalAnalyzer] 检查 %s 失败: %s", self.db_path, e)
            return [], "error"
        if not exists:
            _log.warning("[HistoricalAnalyzer] %s 不存在，历史回溯不可得", self.db_path)
            return [], "missing"
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT date, ticker, direction, final_score, price_at_predict, close_t7 "
                    "FROM predictions WHERE close_t7 IS NOT NULL AND price_at_predict > 0"
                ).fetchall()
            finally:
                con.close()
        except (sqlite3.Error, OSError) as e:
            _log.warning("[HistoricalAnalyzer] 读取 %s 失败: %s", self.db_path, e)
            return [], "error"

        out: List[Dict] = []
        skipped = 0
        for date, ticker, direction, score, p0, c7 in rows:
            try:
                p0 = float(p0)
                c7 = float(c7)
            except (TypeError, ValueError):
                skipped += 1
                continue
            if not (math.isfinite(p0) and math.isfinite(c7)) or p0 <= 0:
                skipped += 1
                continue
            out.append({
                "date": str(date),
                "ticker": str(ticker),
                "direction": str(direction or "").lower(),
                "final_score": score,
                "return_7d_pct": round((c7 / p0 - 1.0) * 100.0, 3),
            })
        self.skipped_rows = skipped
        if skipped:
            # 「这个失败，下游怎么知道」——坏行不能静默消失
            _log.warning("[HistoricalAnalyzer] %d 行 price_at_predict/close_t7 非法，已剔除", skipped)
        return out, ("ok" if out else "empty")

    def _rows(self) -> List[Dict]:
        if self._rows_cache is None:
            self._rows_cache, self.db_status = self._load()
        return self._rows_cache

    # ── 查询 ────────────────────────────────────────────────────────────
    def find_similar_opportunities(
        self, ticker: str, direction: Optional[str] = None
    ) -> List[Dict]:
        """同标的（可选同方向）的历史预测及其 T+7 真实收益，最近的在前。"""
        d = (direction or "").lower() or None
        similar = []
        for r in self._rows():
            if r["ticker"] != ticker:
                continue
            if d is not None and r["direction"] != d:
                continue
            ret = r["return_7d_pct"]
            if r["direction"] in ("bullish", "bearish"):
                hit = (ret > 0) if r["direction"] == "bullish" else (ret < 0)
                result = "hit" if hit else "miss"
            else:
                result = "—"
            similar.append({
                "date": r["date"],
                "event": f"蜂群 {r['direction'] or '—'} · 综合分 {r['final_score']}",
                "direction": r["direction"],
                "final_score": r["final_score"],
                "gain_7d_pct": ret,
                "result": result,
            })
        similar.sort(key=lambda x: x["date"], reverse=True)
        return similar

    def calculate_expected_returns(
        self, ticker: str, direction: Optional[str] = None
    ) -> Dict:
        """T+7 真实收益分布；样本不足时**不返回 expected_7d**（调用方据此判不可得）。"""
        same = self.find_similar_opportunities(ticker, direction) if direction else []
        anyd = self.find_similar_opportunities(ticker)
        base = {
            "source": "pheromone.db",
            "db_status": self.db_status,
            "return_basis": self.RETURN_BASIS,
            "min_sample": self.MIN_SAMPLE,
            "direction": (direction or "").lower() or None,
            "same_direction_n": len(same),
            "any_direction_n": len(anyd),
        }
        if len(same) >= self.MIN_SAMPLE:
            rows, basis = same, "same_direction"
        elif len(anyd) >= self.MIN_SAMPLE:
            rows, basis = anyd, "any_direction"
        else:
            return {
                **base,
                "sample_size": len(anyd),
                "basis": None,
                "note": (f"样本不足：{ticker} 同方向 {len(same)} / 不分方向 {len(anyd)}，"
                         f"低于 {self.MIN_SAMPLE}"),
            }

        rets = sorted(r["gain_7d_pct"] for r in rows)
        dates = [r["date"] for r in rows]
        q = lambda p: round(_percentile(rets, p), 2)  # noqa: E731
        out = {
            **base,
            "sample_size": len(rets),
            "basis": basis,
            "date_range": [min(dates), max(dates)],
            "expected_7d": {
                "mean": round(statistics.mean(rets), 2),
                "median": q(50),
                "min": round(rets[0], 2),
                "max": round(rets[-1], 2),
                "p10": q(10), "p25": q(25), "p75": q(75), "p90": q(90),
                "std": round(statistics.pstdev(rets), 2) if len(rets) > 1 else 0.0,
            },
        }
        # 方向口径的胜率与风险收益比：只在「同方向 + 有方向」时有定义
        d = base["direction"]
        if basis == "same_direction" and d in ("bullish", "bearish"):
            adj = [r if d == "bullish" else -r for r in rets]
            wins = [a for a in adj if a > 0]
            losses = [a for a in adj if a < 0]
            out["hit_rate_pct"] = round(len(wins) / len(adj) * 100.0, 1)
            out["risk_reward"] = {
                "avg_gain_pct": round(statistics.mean(wins), 2) if wins else None,
                "avg_loss_pct": round(statistics.mean(losses), 2) if losses else None,
                "ratio": (round(statistics.mean(wins) / abs(statistics.mean(losses)), 2)
                          if wins and losses else None),
            }
        return out

    def get_similar_opportunities_summary(
        self, ticker: str, direction: Optional[str] = None
    ) -> List[Dict]:
        """最近 3 次同标的（同方向）预测及其 T+7 结果。"""
        return self.find_similar_opportunities(ticker, direction)[:3]


class ProbabilityCalculator:
    """概率和止损止盈计算"""

    def __init__(self):
        pass

    def calculate_win_probability(
        self, ticker: str, crowding_score: float, catalyst_quality: str
    ) -> float:
        """计算赚钱概率"""
        # 基于历史数据的概率计算
        base_prob = 0.55  # 基础 55% 赚钱概率

        # 拥挤度调整
        if crowding_score < 30:
            crowding_adj = 0.08  # +8%
        elif crowding_score < 50:
            crowding_adj = 0.05  # +5%
        elif crowding_score < 70:
            crowding_adj = 0.02  # +2%
        else:
            crowding_adj = -0.05  # -5%

        # 催化剂质量调整
        catalyst_adj = {
            "A+": 0.10,  # +10%
            "A": 0.08,  # +8%
            "B+": 0.05,  # +5%
            "B": 0.02,  # +2%
            "C": -0.02,  # -2%
        }.get(catalyst_quality, 0)

        win_prob = min(max(base_prob + crowding_adj + catalyst_adj, 0.3), 0.85)

        return round(win_prob * 100, 1)

    def calculate_stop_loss_positions(
        self, current_price: float, risk_tolerance_pct: float = 5.0
    ) -> Dict:
        """计算止损位置"""
        return {
            "conservative": round(current_price * (1 - 0.02), 2),  # -2%
            "moderate": round(current_price * (1 - risk_tolerance_pct / 100), 2),
            "aggressive": round(current_price * (1 - 0.08), 2),  # -8%
        }

    def calculate_take_profit_levels(
        self, current_price: float, expected_gain_pct: float
    ) -> Dict:
        """计算止盈位置（分批了结）：三档价格 = 目标涨幅的 30% / 60% / 100% 处。

        ⚠️ `gain_pct` 一律**由该档成交价反算**，不写常数（v0.45.134）。

        旧实现 level_1 / level_2 把 `gain_pct` 写死成 30 / 60，而价格算的是目标涨幅
        的 0.3 / 0.6 倍——「占目标的比例」与「相对现价的涨幅」是两个口径，但渲染时
        进的是同一列（`generate_ml_report` 的「涨幅」列 `+{gain_pct:.0f}%`）。
        本机 1045 份已发布 ML 报告里 **998 份**因此印出「价格递增、标签 30→60→20」
        的自相矛盾表（TMUS 2026-09-04 现价 $181.52：level_1 $192.41 标 +30%，
        实为 +6.0%；标签误差中位 24pp）。

        反算而非按公式重算一遍，是为了让标签与 **round(…, 2) 之后**的价格严格一致：
        低价股（AMC ≈ $2.6）上四舍五入本身就有 0.2% 量级的相对误差。
        不变式固化在 `tests/test_take_profit_labels.py`，与 `expected_gain_pct`
        的来源无关——Step 2 换数据源后仍然有效。
        """
        def _level(fraction: float) -> "tuple[float, float]":
            price = round(current_price * (1 + expected_gain_pct * fraction / 100), 2)
            return price, round((price / current_price - 1) * 100, 1)

        level_1, gain_1 = _level(0.3)
        level_2, gain_2 = _level(0.6)
        level_3, gain_3 = _level(1.0)

        return {
            "level_1": {
                "price": level_1,
                "gain_pct": gain_1,
                "sell_ratio": 0.33,  # 卖出 1/3
                "reason": "锁定初步收益",
            },
            "level_2": {
                "price": level_2,
                "gain_pct": gain_2,
                "sell_ratio": 0.33,  # 再卖出 1/3
                "reason": "追踪止损，保护利润",
            },
            "level_3": {
                "price": level_3,
                "gain_pct": gain_3,
                "sell_ratio": 0.34,  # 卖出剩余
                "reason": "达到目标收益，全部清仓",
            },
        }

    def calculate_optimal_holding_time(
        self, similar_opportunities: List[Dict]
    ) -> Dict:
        """计算最优持仓时间

        需要同一样本在 3 / 7 / 30 天三个持仓期的收益才能比较。v0.45.132 起历史样本
        来自 pheromone.db，只有 T+7 一个持仓期的干净结果——比较无从做起，诚实返回
        「不可得」，不拿 7 天当「最优」冒充测算。
        """
        if not similar_opportunities:
            return {
                "note": "数据不足",
            }
        if any(("gain_3d_pct" not in o or "gain_30d_pct" not in o) for o in similar_opportunities):
            return {
                "note": "历史样本只有 T+7 结果，无法比较持仓期（v0.45.132）",
            }

        # 分析历史数据中的最优持仓时间
        times = []
        gains = []

        for opp in similar_opportunities:
            # 7 天收益通常最优
            if opp["gain_7d_pct"] > opp["gain_3d_pct"]:
                times.append(7)
                gains.append(opp["gain_7d_pct"])
            elif opp["gain_30d_pct"] > opp["gain_7d_pct"]:
                times.append(30)
                gains.append(opp["gain_30d_pct"])
            else:
                times.append(3)
                gains.append(opp["gain_3d_pct"])

        avg_time = round(statistics.mean(times), 0) if times else 7

        return {
            "recommended_holding_days": int(avg_time),
            "note": f"基于 {len(similar_opportunities)} 次类似机会的平均持仓时间",
            "holding_time_range": {
                "minimum": 3,
                "optimal": int(avg_time),
                "maximum": 30,
            },
            "expected_gain_at_optimal_time": round(statistics.mean(gains), 2),
        }


class AdvancedAnalyzer:
    """综合高级分析系统"""

    def __init__(self):
        self.comparator = IndustryComparator()
        self.history = HistoricalAnalyzer()
        self.probability = ProbabilityCalculator()
        self.dealer_gex = DealerGEXAnalyzer()

    def generate_comprehensive_analysis(
        self, ticker: str, realtime_metrics: Dict, direction: Optional[str] = None
    ) -> Dict:
        """生成综合高级分析报告

        direction：蜂群当日方向（bullish / bearish / neutral），v0.45.132 起用于
        历史回溯的「同标的 + 同方向」条件；不传则历史回溯只按标的不分方向。
        """

        # 提取关键数据
        crowding_score = realtime_metrics.get("crowding_input", {}).get(
            "social_messages_per_day", 0
        )
        current_price = realtime_metrics.get("sources", {}).get("yahoo_finance", {}).get(
            "current_price", 0
        )

        # 如果 crowding_score 是消息数量，需要转换
        if crowding_score > 1000:
            # 这是消息数量，需要估算拥挤度评分
            if ticker == "NVDA":
                crowding_pct = 63.5
            elif ticker == "VKTX":
                crowding_pct = 44.1
            else:
                crowding_pct = 63.8
        else:
            crowding_pct = crowding_score

        analysis = {
            "ticker": ticker,
            "timestamp": datetime.now().isoformat(),
            "overview": self._generate_overview(ticker),
        }

        # 1. 行业对标分析
        analysis["industry_comparison"] = self.comparator.compare_with_peers(
            ticker, realtime_metrics
        )

        # 2. 历史回溯分析（v0.45.132：读 pheromone.db 真实 T+7，同标的 + 同方向；
        #    此前的拥挤度匹配连同 6 条手写记录一起删除）
        expected_returns = self.history.calculate_expected_returns(ticker, direction)
        analysis["historical_analysis"] = {
            "similar_opportunities": self.history.get_similar_opportunities_summary(
                ticker, direction
            ),
            "expected_returns": expected_returns,
        }

        # 3. 概率和止损止盈
        if current_price > 0:
            analysis["probability_analysis"] = {
                "win_probability_pct": self.probability.calculate_win_probability(
                    ticker, crowding_pct, self._estimate_catalyst_quality(ticker)
                ),
                "risk_reward_ratio": self._calculate_risk_reward_ratio(
                    ticker, expected_returns
                ),
            }

            analysis["position_management"] = {
                "stop_loss": self.probability.calculate_stop_loss_positions(
                    current_price
                ),
                "take_profit": self.probability.calculate_take_profit_levels(
                    current_price, self._estimate_expected_gain(ticker, crowding_pct)
                ),
                "optimal_holding_time": self.probability.calculate_optimal_holding_time(
                    analysis["historical_analysis"]["similar_opportunities"]
                ),
            }

        # 4. 投资建议
        analysis["recommendation"] = self._generate_recommendation(
            ticker, analysis, crowding_pct, current_price
        )

        # 5. 期权分析（OptionsAgent）
        if OPTIONS_AGENT_AVAILABLE and OptionsAgent is not None:
            try:
                options_agent = OptionsAgent()
                analysis["options_analysis"] = options_agent.analyze(
                    ticker, stock_price=current_price if current_price > 0 else None
                )
            except NETWORK_ERRORS as e:
                _log.error("期权分析异常: %s", e, exc_info=True)
                analysis["options_analysis"] = None
        else:
            analysis["options_analysis"] = None

        # 6. Dealer GEX 分析（BS gamma 计算，真实期权链）
        if current_price > 0:
            try:
                gex_result = self.dealer_gex.analyze(ticker, current_price)
                analysis["dealer_gex"] = gex_result
                _log.info(
                    "%s Dealer GEX: %.2f M$ | regime=%s | flip=$%.1f",
                    ticker,
                    gex_result.get("total_gex", 0),
                    gex_result.get("regime", "?"),
                    gex_result.get("gex_flip") or 0,
                )
            except Exception as e:
                _log.warning("Dealer GEX 分析失败 %s: %s", ticker, e)
                analysis["dealer_gex"] = {"error": str(e), "total_gex": 0.0}
        else:
            analysis["dealer_gex"] = None

        return analysis

    def _generate_overview(self, ticker: str) -> str:
        """生成概述"""
        overviews = {
            "NVDA": "AI 芯片龙头，CUDA 生态护城河深厚，但估值已高，监管风险需关注",
            "VKTX": "GLP-1 领先者，临床进展良好，但财务压力大，失败风险需防范",
            "TSLA": "EV 成本领导者，产能扩张快，但竞争加剧，利润率承压",
            "AMD": "GPU 追赶者，性价比优势，但高端市场份额仍低",
        }
        return overviews.get(ticker, "标的基本面分析")

    def _estimate_catalyst_quality(self, ticker: str) -> str:
        """估算催化剂质量"""
        return {
            "NVDA": "A",  # 财报催化强
            "VKTX": "A+",  # 试验结果催化非常强
            "TSLA": "B+",  # 交付指引中等强
        }.get(ticker, "B")

    def _estimate_expected_gain(self, ticker: str, crowding: float) -> float:
        """估算预期涨幅"""
        base_gains = {
            "NVDA": 15.0,  # 基础 15% 涨幅预期
            "VKTX": 25.0,  # 更高波动性
            "TSLA": 12.0,
        }

        base = base_gains.get(ticker, 12.0)

        # 拥挤度调整
        if crowding < 40:
            return base + 8  # 低拥挤 +8%
        elif crowding < 60:
            return base + 3  # 中等拥挤 +3%
        elif crowding < 75:
            return base - 2  # 高拥挤 -2%
        else:
            return base - 5  # 极度拥挤 -5%

    def _calculate_risk_reward_ratio(self, ticker: str, expected_returns: Dict):
        """风险收益比；**无历史可比时返回 None**。

        v0.45.50：旧实现无历史返回 **2.0**，而 `_generate_recommendation` 的
        STRONG BUY 闸正是 `prob >= 70 and rr >= 2.0` —— 「一次历史比对都没做成」
        恰好卡在阈值上通过，于是缺数据不但不降级，反而让评级门槛自动满足。

        v0.45.132：来源改为 HistoricalAnalyzer 在同标的 + 同方向真实 T+7 样本上
        算出的 `risk_reward.ratio`（方向口径的平均盈利 / 平均亏损）。退回不分方向
        或没有方向时该项不存在 → None；样本里没有亏损单也是 None（那是样本太少的
        症状，不是「风险收益比 ∞」）。
        """
        if not isinstance(expected_returns, dict):
            return None
        rr = (expected_returns.get("risk_reward") or {}).get("ratio")
        if not isinstance(rr, (int, float)) or isinstance(rr, bool) or not math.isfinite(rr):
            _log.debug("[%s] 风险收益比不可得（basis=%s, n=%s），不以常数冒充",
                       ticker, (expected_returns or {}).get("basis"),
                       (expected_returns or {}).get("sample_size"))
            return None
        return rr

    def _generate_recommendation(
        self, ticker: str, analysis: Dict, crowding: float, price: float
    ) -> Dict:
        """生成投资建议"""
        prob = analysis.get("probability_analysis", {}).get("win_probability_pct", 50)
        rr = analysis.get("probability_analysis", {}).get("risk_reward_ratio")
        # v0.45.50：rr 不可得时**不许升级评级**。
        # 旧默认值 1.5 恰好是 BUY 闸的阈值（`prob >= 60 and rr >= 1.5`），
        # 与上面 _calculate_risk_reward_ratio 的 2.0 一样卡在门槛上。
        # 两个闸都要求 rr 达标，所以「不知道」必须是不达标，而不是刚好达标。
        _rr_known = isinstance(rr, (int, float)) and not isinstance(rr, bool)
        if not _rr_known:
            _log.debug("风险收益比不可得，评级不因缺数据而升级")

        # 评估建议
        if prob >= 70 and _rr_known and rr >= 2.0:
            rating = "STRONG BUY"
            action = "积极布局"
        elif prob >= 60 and _rr_known and rr >= 1.5:
            rating = "BUY"
            action = "分批建仓"
        elif prob >= 50:
            rating = "HOLD"
            action = "观察等待"
        else:
            rating = "AVOID"
            action = "回避或减仓"

        return {
            "rating": rating,
            "action": action,
            "confidence": f"{prob:.1f}%",
            # v0.45.50：rr 为 None 时印「未知」，不印 "None:1" 也不编一个数
            "rationale": (f"赚钱概率 {prob:.1f}%，风险收益比 {rr}:1" if _rr_known
                          else f"赚钱概率 {prob:.1f}%，风险收益比未知（无历史可比样本）"),
        }


# ==================== 脚本示例 ====================
if __name__ == "__main__":
    import json

    analyzer = AdvancedAnalyzer()

    # 加载实时数据
    with open("realtime_metrics.json") as f:
        metrics = json.load(f)

    # 生成分析报告
    print("🎯 Alpha Hive 高级分析系统")
    print("=" * 60)

    for ticker in ["NVDA", "VKTX", "TSLA"]:
        if ticker in metrics:
            analysis = analyzer.generate_comprehensive_analysis(ticker, metrics[ticker])

            print(f"\n\n{'=' * 60}")
            print(f"📊 {ticker} 高级分析报告")
            print(f"{'=' * 60}")

            # 概述
            print(f"\n📌 概述：{analysis['overview']}")

            # 行业对标
            if analysis.get("industry_comparison"):
                ic = analysis["industry_comparison"]
                print(f"\n🏆 行业对标：{ic.get('position')} in {ic.get('industry')}")
                print(f"   竞争力评分：{ic.get('comparative_strength')}/100")
                print(f"   主要优势：")
                for adv in ic.get("competitive_advantages", [])[:2]:
                    print(f"     • {adv}")

            # 历史对标
            ha = analysis.get("historical_analysis", {})
            similar = ha.get("similar_opportunities", [])
            if similar:
                print(f"\n📈 历史类似机会（最近 3 次）：")
                for i, opp in enumerate(similar[:3], 1):
                    print(f"   {i}. {opp['date']} - {opp['event']}")
                    print(
                        f"      3 日收益：{opp['gain_3d_pct']}% | 7 日收益：{opp['gain_7d_pct']}% | 30 日收益：{opp['gain_30d_pct']}%"
                    )

            # 收益预期（v0.45.132：只有 T+7 真实分布，没有 3 天 / 30 天）
            er = ha.get("expected_returns", {})
            e7 = er.get("expected_7d")
            if e7:
                print(f"\n💰 T+7 收益分布（{er.get('basis')}，n={er['sample_size']}）：")
                print(f"   均值 {e7['mean']}% | 中位 {e7['median']}% | "
                      f"P10 {e7['p10']}% | P90 {e7['p90']}%")
            else:
                print(f"\n💰 T+7 收益分布不可得：{er.get('note') or er.get('db_status')}")

            # 概率
            pa = analysis.get("probability_analysis", {})
            print(f"\n🎲 赚钱概率：{pa.get('win_probability_pct', '?')}%")
            _rr_p = pa.get("risk_reward_ratio")
            print(f"   风险收益比：{_rr_p}:1" if isinstance(_rr_p, (int, float))
                  else "   风险收益比：未知（无历史可比样本）")

            # 位置管理
            pm = analysis.get("position_management", {})
            if pm:
                print(f"\n🛑 位置管理：")
                sl = pm.get("stop_loss", {})
                print(f"   止损位置：$" + ", $".join(
                    [f"{sl.get('conservative')}", f"{sl.get('moderate')}", f"{sl.get('aggressive')}"]
                ))

                tp = pm.get("take_profit", {})
                print(f"   止盈方案（分批了结）：")
                for level, data in list(tp.items())[:3]:
                    print(
                        f"     • {level.upper()}：${data['price']} (+{data['gain_pct']}%)，卖 {int(data['sell_ratio']*100)}%"
                    )

                oht = pm.get("optimal_holding_time", {})
                print(
                    f"   最优持仓：{oht.get('recommended_holding_days')} 天"
                )

            # 建议
            rec = analysis.get("recommendation", {})
            print(f"\n✅ 投资建议：")
            print(
                f"   评级：{rec.get('rating')} | 行动：{rec.get('action')}"
            )
            print(f"   理由：{rec.get('rationale')}")

    print("\n" + "=" * 60)
    print("✅ 高级分析完成！")
    print("=" * 60)
