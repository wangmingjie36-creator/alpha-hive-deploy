"""
🐝 Alpha Hive - 高级分析系统
支持行业对标、历史回溯、概率计算、止损止盈建议
"""

import logging as _logging
import json
import math
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple
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

    #: 前瞻估计量的最小样本。远低于分票口径的 MIN_SAMPLE——池化是全书汇总，
    #: 几百条起步，这个闸只防「库刚建起来」的极早期。
    MIN_POOLED_SAMPLE = 100

    @staticmethod
    def _wilson_ci(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
        """比例的 Wilson 95% 区间（百分数）。

        不用正态近似 `p ± z·√(p(1−p)/n)`：它在 p 靠近 0/1 时会给出超出 [0,1]
        的界，而「命中率区间上界 103%」这种数印出去就是错的。Wilson 不会越界。
        """
        if n <= 0:
            raise ValueError("n must be positive")
        p = k / n
        d = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / d
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
        return (round(max(0.0, centre - half) * 100, 1),
                round(min(1.0, centre + half) * 100, 1))

    def pooled_hit_rate(self) -> Dict:
        """全书方向性预测的**池化**命中率——前瞻估计量（v0.45.138）。

        为什么前瞻用池化、而不是同一份数据里的分票分方向频率：
        v0.45.134 的记分卡按时点隔离实测（n=629，2026-03-23→08-26），
        在真正用上分票频率的 147 条上 **分票 Brier 0.2898 vs 池化 0.2611**，
        配对差 +0.0287 ± 0.0135（**t = +2.12，显著**）；经验贝叶斯收缩 α 从 0
        扫到 ∞，Brier **单调递减到完全收缩、没有内部最优** ⇒ 分票那一层的
        信息量为零，是在拟合噪声。

        ⚠️ 这不代表分票频率是假的——它作为「这只票这个方向历史上赢过几成」的
        **描述**完全成立（见 `calculate_expected_returns` 的 `hit_rate_pct`）。
        两者的区别正是「描述过去」与「预测下一笔」，本函数只管后者。

        口径与 `calculate_expected_returns` 逐字一致：命中 = 方向调整后收益 > 0
        （恰好为 0 计入分母算未命中），只算 bullish / bearish。

        生产运行时不需要时点隔离：库里 `close_t7` 非空的行全部已实现，
        不含未来信息。embargo 只在 `probability_scorecard --walk-forward`
        的回测里才需要。
        """
        rows = self._rows()
        out: Dict = {"basis": "pooled_directional", "db_status": self.db_status}
        dirn = [r for r in rows if r["direction"] in ("bullish", "bearish")]
        n = len(dirn)
        out["sample_size"] = n
        if n < self.MIN_POOLED_SAMPLE:
            out["hit_rate_pct"] = None
            out["ci95"] = None
            out["note"] = (f"池化样本 {n} 条，低于 {self.MIN_POOLED_SAMPLE}"
                           f"（db_status={self.db_status}）")
            _log.debug("[HistoricalAnalyzer] 前瞻估计量不可得：%s", out["note"])
            return out
        k = sum(1 for r in dirn
                if (r["return_7d_pct"] if r["direction"] == "bullish"
                    else -r["return_7d_pct"]) > 0)
        out["hit_rate_pct"] = round(k / n * 100.0, 1)
        out["ci95"] = list(self._wilson_ci(k, n))
        out["date_range"] = [min(r["date"] for r in dirn), max(r["date"] for r in dirn)]
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

    def calculate_stop_loss_positions(
        self, current_price: float, risk_tolerance_pct: float = 5.0
    ) -> Dict:
        """计算止损位置"""
        return {
            "conservative": round(current_price * (1 - 0.02), 2),  # -2%
            "moderate": round(current_price * (1 - risk_tolerance_pct / 100), 2),
            "aggressive": round(current_price * (1 - 0.08), 2),  # -8%
        }

    #: 三档止盈 = 历史 T+7 收益分布的三个分位点（v0.45.134）。
    #: `reason` 一律写「观测到的频率」，不写交易建议——这张表是历史频次的陈述，
    #: 不是「该在哪减仓」的意见。
    TP_LEVELS = (
        ("level_1", 0.33, "历史 T+7 中位：同类持仓约一半走到过这里"),
        ("level_2", 0.33, "历史 T+7 四分位：约四次里一次走到过"),
        ("level_3", 0.34, "历史 T+7 十分位：约十次里一次走到过"),
    )

    #: 盈利符号：空头的价格下跌是盈利
    _DIRECTION_SIGN = {"bullish": 1.0, "bearish": -1.0}

    def calculate_take_profit_levels(
        self, current_price: float, level_gains: Sequence[float], direction: str
    ) -> Dict:
        """计算止盈位置（分批了结）。

        `level_gains`：三档的**原始价格变动**（%），按 `direction` 折算后盈利递增，
        来自 `expected_7d` 的分位数（多头 P50/P75/P90、空头 P50/P25/P10，
        见 `_TP_QUANTILES_BY_DIRECTION`）。**没有默认值**——拿不到分布时调用方
        必须整节渲染「不可得」，不许挑一个兜底数。

        产出两个数，别混用：
          · `gain_pct`  = 相对现价的**价格变动**，与 `price` 严格一致（可为负）
          · `profit_pct`= 按持仓方向折算的**盈利**，空头是 `-gain_pct`
        空头的 `gain_pct` 为负正是它在赚钱。渲染时标「涨幅」必须用 `gain_pct`、
        标「盈利」必须用 `profit_pct`，混了就是 v0.45.134 修的那个口径错位。

        v0.45.134 之前这里吃的是 `_estimate_expected_gain(ticker, crowding)`：
        NVDA 15 / VKTX 25 / 其他 12 的写死基数 + 拥挤度四档调整。而拥挤度入参
        在当前流水线里恒为 0（`realtime_metrics` 不含 `crowding_input`），
        于是 803 份生产报告里 **76% 的目标涨幅恒等于 20.0%** —— 而同期
        pheromone.db 实测同标的同方向 T+7 中位收益只有 **0.01%**，
        11 个够样本的组合里 5 个为负。

        ⚠️ 分位数为负是**真实结论**（「历史上一半的同类持仓 T+7 是亏的」），
        照常渲染、不过滤。跳过它等于把「测出来是负的」渲染成「没测过」，
        与 v0.45.114 同一形状。

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
        gains = list(level_gains)
        if len(gains) != len(self.TP_LEVELS):
            raise ValueError(
                f"level_gains 需要 {len(self.TP_LEVELS)} 个分位点，实得 {len(gains)}"
            )

        if direction not in self._DIRECTION_SIGN:
            raise ValueError(f"direction 必须是 bullish / bearish，实得 {direction!r}")
        sign = self._DIRECTION_SIGN[direction]

        out: Dict = {}
        for (key, sell_ratio, reason), target in zip(self.TP_LEVELS, gains):
            price = round(current_price * (1 + target / 100), 2)
            # 标签由 round(…, 2) 之后的成交价反算，与印出去的价严格一致
            gain_pct = round((price / current_price - 1) * 100, 1)
            out[key] = {
                "price": price,
                "gain_pct": gain_pct,
                "profit_pct": round(sign * gain_pct, 1),
                "sell_ratio": sell_ratio,
                "reason": reason,
                "direction": direction,
            }
        return out

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
        #
        # v0.45.134 删除：`crowding_pct` 及其两条来源。
        #   · `realtime_metrics["crowding_input"]` 在**当前流水线里从不存在**——
        #     两个入口（alpha_hive_daily_report._analyze_ticker_safe、
        #     generate_ml_report.__main__）构造的 dict 只有 ticker 与
        #     sources.yahoo_finance；唯一生产该键的 data_fetcher.collect_all_metrics()
        #     全仓没有生产调用点。于是 crowding_score 恒为 0。
        #   · 那条「> 1000 → NVDA 63.5 / VKTX 44.1 / 其他 63.8」的魔数分支因此
        #     从不触发；否则走的是「把消息条数直接当百分比」——两条路都不是拥挤度。
        #   · 恒为 0 落进两个消费者的最看多档（胜率 +8pp、目标涨幅 +8pp）：
        #     **一条数据都没拿到被系统当成了利好**，与 crowding_detector.py
        #     v0.45.50 修掉的是同一形状，那次漏了这个孪生兄弟。
        # 真实拥挤度另有其人：ScoutBeeNova 走 CrowdingDetector 算出的
        # `swarm_results.agent_details.ScoutBeeNova.details.crowding_score`
        # （TMUS 2026-09-04 实测 18.07）。本函数在蜂群结果注入之前运行、拿不到它，
        # 故此处不重建一个假的。
        current_price = realtime_metrics.get("sources", {}).get("yahoo_finance", {}).get(
            "current_price", 0
        )

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

        # 3. 历史命中率与止损止盈（v0.45.134：两者都改读 expected_returns，
        #    不可得时字段为 None —— 调用方必须按「不可得」渲染，见下方字段契约）
        _pooled = self.history.pooled_hit_rate()
        if current_price > 0:
            analysis["probability_analysis"] = {
                # ⚠️ 字段名从 win_probability_pct 改成 hit_rate_pct 是有意的：
                #    它是样本内历史频率，不是前瞻概率。旧名字会让读的人以为
                #    系统在预测赢面，而那个数六个月没动过。
                # ── 描述量：这只票这个方向**过去**赢过几成 ──────────────
                "hit_rate_pct": self._history_hit_rate(ticker, expected_returns),
                "basis": expected_returns.get("basis"),
                "sample_size": expected_returns.get("sample_size"),
                "return_basis": HistoricalAnalyzer.RETURN_BASIS,
                # ── 前瞻量：**下一笔**赢面多大（v0.45.138）────────────────
                # 全书池化，故各标的相同。这不是偷懒——记分卡实测分票频率
                # 作为预测显著更差（t=+2.12），且收缩曲线单调到底。
                "forward_estimate_pct": self._forward_estimate(ticker, _pooled),
                "forward_ci95": _pooled.get("ci95"),
                "forward_sample_size": _pooled.get("sample_size"),
                "forward_basis": _pooled.get("basis"),
                "forward_is_ticker_specific": False,
                "risk_reward_ratio": self._calculate_risk_reward_ratio(
                    ticker, expected_returns
                ),
            }

            _tp = self._take_profit_gains(ticker, expected_returns, direction)
            _tp_gains, _tp_keys = _tp if _tp is not None else (None, None)
            analysis["position_management"] = {
                "stop_loss": self.probability.calculate_stop_loss_positions(
                    current_price
                ),
                # 拿不到分布就整节不可得。不挑兜底值——0.0 在本量表上恰是
                # 「一分不赚」这个明确结论，而我们要表达的是「不知道」。
                "take_profit": (
                    self.probability.calculate_take_profit_levels(
                        current_price, _tp_gains, direction)
                    if _tp_gains is not None else None
                ),
                "take_profit_quantiles": _tp_keys,
                "optimal_holding_time": self.probability.calculate_optimal_holding_time(
                    analysis["historical_analysis"]["similar_opportunities"]
                ),
            }

        # 4. 投资建议
        analysis["recommendation"] = self._generate_recommendation(ticker, analysis)

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

    #: 止盈三档取 `expected_7d` 的哪三个分位——**按方向取，不是固定三个键**。
    #:
    #: `expected_7d` 的分位数是**原始收益**（close_t7/price−1），没有按方向调整。
    #: 对多头，收益越大越赚，阶梯是 P50 → P75 → P90；对空头，价格跌得越多越赚，
    #: 阶梯必须反过来走 P50 → P25 → P10。两者共同的性质是「**盈利递增**」。
    #: 中性方向没有「止盈」可言（没有持仓方向就没有盈利方向），返回 None。
    _TP_QUANTILES_BY_DIRECTION = {
        "bullish": ("median", "p75", "p90"),
        "bearish": ("median", "p25", "p10"),
    }

    def _take_profit_gains(
        self, ticker: str, expected_returns: Dict, direction: Optional[str]
    ) -> Optional[Tuple[List[float], List[str]]]:
        """止盈三档的目标涨幅；**分布不可得时返回 None**，不以常数冒充。

        v0.45.134：来源改为 `HistoricalAnalyzer` 在同标的（同方向优先）真实
        T+7 样本上算出的 P50 / P75 / P90。旧实现 `_estimate_expected_gain`
        是「NVDA 15 / VKTX 25 / 其他 12 的写死基数 + 拥挤度四档调整」，而
        拥挤度入参恒为 0 ⇒ 803 份生产报告里 76% 的目标涨幅恒等于 20.0%。

        守卫与 `_calculate_risk_reward_ratio` 同源、逐字同型：
        `bool` 是 `int` 的子类，必须显式排除（v0.45.121 的教训——同类守卫在
        本仓 5 处全写了 `not isinstance(x, bool)`）。
        """
        keys = self._TP_QUANTILES_BY_DIRECTION.get(direction or "")
        if keys is None:
            _log.debug("[%s] 止盈不可得：方向为 %r —— 没有持仓方向就没有盈利方向",
                       ticker, direction)
            return None
        e7 = (expected_returns or {}).get("expected_7d") or {}
        vals: List[float] = []
        for key in keys:
            v = e7.get(key)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
                _log.debug("[%s] 止盈目标不可得：expected_7d.%s = %r（basis=%s, n=%s）",
                           ticker, key, v, (expected_returns or {}).get("basis"),
                           (expected_returns or {}).get("sample_size"))
                return None
            vals.append(float(v))
        # 按方向折算成盈利后必须单调不减；不满足说明上游分位数算坏了，
        # 宁可整节不可得，也不印一张「越靠后越不赚」的倒序阶梯
        sign = 1.0 if direction == "bullish" else -1.0
        profits = [sign * v for v in vals]
        if not all(a <= b for a, b in zip(profits, profits[1:])):
            _log.warning("[%s] 止盈目标不可得：%s 方向折算盈利后非单调 %s（原始 %s）"
                         " —— 上游 expected_7d 有问题", ticker, direction, profits, vals)
            return None
        return [round(v, 2) for v in vals], list(keys)

    def _forward_estimate(self, ticker: str, pooled: Dict) -> Optional[float]:
        """前瞻命中率估计（%）；**不可得返回 None**，不以常数冒充。

        与 `_history_hit_rate` 的分工是本版的核心（v0.45.138）：
          · `_history_hit_rate` 回答「**这只票这个方向过去**赢过几成」——描述
          · `_forward_estimate` 回答「**下一笔**赢面多大」——预测

        两者用同一批样本、却必须是两个数：v0.45.134 的记分卡按时点隔离实测，
        分票频率作为**预测**显著劣于池化（配对 t=+2.12），收缩曲线单调到底。
        把描述量直接当预测量用，正是那次红的原因。
        """
        if not isinstance(pooled, dict):
            return None
        v = pooled.get("hit_rate_pct")
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            _log.debug("[%s] 前瞻估计不可得（n=%s）：%s", ticker,
                       (pooled or {}).get("sample_size"), (pooled or {}).get("note"))
            return None
        return float(v)

    def _history_hit_rate(self, ticker: str, expected_returns: Dict) -> Optional[float]:
        """同标的同方向历史 T+7 命中率（%）；**不可得返回 None**。

        ⚠️ 这是**样本内的历史频率**，不是校准过的前瞻概率——本项目实测
        `final_score` 的 Spearman IC = +0.042（n=683，95%CI 半宽 ±0.075，不显著），
        五个维度无一显著。所以它只能回答「这只票这个方向过去赢过几成」，
        不能回答「这次赢面多大」。字段名与渲染文案必须体现这个区别。

        v0.45.134 之前这里是 `calculate_win_probability`：base 0.55 + 拥挤度
        常数 + 催化剂常数。生产 803 份报告里 **81% 恒等于 65.0**，34 只标的
        中 31 只六个月一动不动；而真实命中率实测跨度 37.0%~81.8%，
        11 个够样本的组合里 4 个低于 50%（VKTX 看多印 70~73%、实测 37.0%）。
        """
        if not isinstance(expected_returns, dict):
            return None
        hr = expected_returns.get("hit_rate_pct")
        if not isinstance(hr, (int, float)) or isinstance(hr, bool) or not math.isfinite(hr):
            _log.debug("[%s] 历史命中率不可得（basis=%s, n=%s），不以常数冒充",
                       ticker, (expected_returns or {}).get("basis"),
                       (expected_returns or {}).get("sample_size"))
            return None
        return float(hr)

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

    def _generate_recommendation(self, ticker: str, analysis: Dict) -> Dict:
        """投资「建议」——v0.45.139 起**不再产出评级词**，只汇总三个可核对的数。

        为什么撤掉评级（2026-09-06 实测，n=438 份能对上 T+7 结果的生产报告）：
          · 第 1 章「建议」：BUY 命中 54.5% vs HOLD 56.9%，z = −0.55 —— 不区分结果
          · 本函数旧评级：STRONG BUY 命中 38.3% [25.8, 52.6] vs BUY 59.0% —— **是反的**。
            STRONG BUY 就是 VKTX / NVDA 那几只写死高常数的票，而 VKTX 真实命中率 37%
          · ML 概率五等分命中率 59.8 / 64.4 / 51.7 / 54.0 / 54.4，非单调；
            Spearman +0.026 ± 0.099，不显著
          · 前瞻量自 v0.45.138 起全书池化、各标的相同 ⇒ 概率闸在结构上不可能区分标的
        没有任何一个概率输入分得开标的。零区分度的评级带着警示发出去，只会让读者
        学会忽略警示；对一个不随标的变化的输入重定阈值毫无意义。
        三道闸（STRONG BUY 70/2.0、BUY 60/1.5、HOLD 50）连同来历记在 CHANGELOG v0.45.139。

        保留的三个数都能被 `probability_scorecard.py` 核对：前瞻量（池化 + Wilson 区间）、
        描述量（本标的本方向历史频率）、rr（本标的）。等 `ic_rerun_readiness.py` 的
        25 周闸开、真有区分信号时再引回评级——那时它有证据。
        字段形状保持（`rating` / `action` 键仍在、值为 None），下游读者不会崩。
        """
        _pa = analysis.get("probability_analysis") or {}
        fwd = _pa.get("forward_estimate_pct")
        hr = _pa.get("hit_rate_pct")
        rr = _pa.get("risk_reward_ratio")

        def _num(v) -> bool:
            return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

        _ci = _pa.get("forward_ci95")
        _ci_txt = (f"，95% 区间 [{_ci[0]}, {_ci[1]}]"
                   if isinstance(_ci, (list, tuple)) and len(_ci) == 2 else "")
        parts = [
            (f"前瞻命中率 {fwd:.1f}%（全书池化 n={_pa.get('forward_sample_size')}{_ci_txt}；各标的相同）"
             if _num(fwd) else
             f"前瞻命中率不可得（池化样本 n={_pa.get('forward_sample_size')}）"),
            (f"本标的本方向历史命中率 {hr:.1f}%（n={_pa.get('sample_size')}, basis={_pa.get('basis')}）"
             if _num(hr) else
             f"本标的本方向历史命中率不可得（n={_pa.get('sample_size')}）"),
            # v0.45.50：rr 为 None 时印「未知」，不印 "None:1" 也不编一个数
            (f"风险收益比 {rr}:1（本标的）" if _num(rr)
             else "风险收益比未知（样本里没有亏损单，多半是样本太少）"),
        ]
        return {
            "rating": None,
            "action": None,
            "confidence": f"{fwd:.1f}%" if _num(fwd) else None,
            "probability_is_ticker_specific": False,
            "rating_retired": "v0.45.139",
            "rating_retired_reason": (
                "无任何概率输入能区分标的：BUY 命中 54.5% vs HOLD 56.9%（z=−0.55），"
                "旧 STRONG BUY 38.3% vs BUY 59.0% 反向"
            ),
            "rationale": "；".join(parts),
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

            # 历史命中率（v0.45.134：不是「赚钱概率」——那是前瞻断言）
            pa = analysis.get("probability_analysis", {})
            _hr = pa.get("hit_rate_pct")
            print(f"\n🎲 同方向历史 T+7 命中率："
                  + (f"{_hr:.1f}%（n={pa.get('sample_size')}, basis={pa.get('basis')}）"
                     if isinstance(_hr, (int, float)) else "不可得（无同方向可比样本）"))
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

                tp = pm.get("take_profit")
                if tp:
                    print(f"   止盈方案（历史 T+7 分位，分批了结）：")
                    for level, data in list(tp.items())[:3]:
                        print(
                            f"     • {level.upper()}：${data['price']} ({data['gain_pct']:+}%)，"
                            f"卖 {int(data['sell_ratio']*100)}% — {data['reason']}"
                        )
                else:
                    print(f"   止盈方案：不可得（无历史 T+7 分布）")

                oht = pm.get("optimal_holding_time", {})
                print(
                    f"   最优持仓：{oht.get('recommended_holding_days')} 天"
                )

            # 建议
            rec = analysis.get("recommendation", {})
            print(f"\n✅ 投资建议：")
            print(
                f"   评级：已于 {rec.get('rating_retired')} 撤销（{rec.get('rating_retired_reason')}）"
            )
            print(f"   理由：{rec.get('rationale')}")

    print("\n" + "=" * 60)
    print("✅ 高级分析完成！")
    print("=" * 60)
