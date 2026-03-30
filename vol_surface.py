#!/usr/bin/env python3
"""
🐝 Alpha Hive · 波动率曲面拟合模块
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

使用 SABR 模型（Hagan 2002 近似）拟合隐含波动率微笑，替代简单的 IV 期限结构。
支持异常检测、风险反转、蝴蝶价差计算。

用法:
    from vol_surface import VolSurface, SABRCalibrator

    # 构建波动率曲面
    surface = VolSurface("NVDA")
    surface.build_surface(options_chain_data)

    # 获取某个行权价和到期日的隐含波动率
    iv = surface.get_iv(strike=120.0, expiration_days=30)

    # 计算 25delta 风险反转
    rr = surface.calculate_risk_reversal_25d(expiration_days=30)

    # 检测曲面异常
    anomalies = surface.detect_surface_anomalies()
"""

import math
import warnings
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field
from collections import defaultdict

from hive_logger import get_logger

_log = get_logger("vol_surface")

# ── 尝试导入 scipy，如果失败则使用纯 Python 优化器 ──
try:
    from scipy.optimize import minimize as scipy_minimize
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False
    _log.warning("scipy 不可用，将使用纯 Python Nelder-Mead 优化器")


# ──────────────────────────────────────────────────────────────────────────────
# SABR 模型实现（Hagan 2002 近似）
# ──────────────────────────────────────────────────────────────────────────────

def sabr_implied_vol(
    F: float,
    K: float,
    T: float,
    alpha: float,
    beta: float,
    rho: float,
    nu: float
) -> float:
    """
    计算 SABR 模型隐含波动率（Hagan 2002 近似）。

    参数:
        F: 远期价格
        K: 行权价
        T: 到期时间（年）
        alpha: SABR 参数（ATM 波动率）
        beta: SABR 参数（价格过程的次数，0≤beta≤1）
        rho: SABR 参数（相关系数，-1≤rho≤1）
        nu: SABR 参数（波动率的波动率）

    返回:
        隐含波动率 (float)
    """
    # 边界情况：时间趋于 0
    if T <= 0:
        return alpha if K == F else 0.0

    # 避免除以 0
    if F <= 0 or K <= 0 or alpha <= 0:
        return 0.0

    # ATM 情况特殊处理
    if abs(K - F) < 1e-10 * F:
        # ATM implied vol = alpha / (F^(1-beta))
        if beta < 1.0:
            return alpha / (F ** (1.0 - beta))
        return alpha

    # 避免波动率平方为负
    if nu < 0 or rho < -1.0 or rho > 1.0 or beta < 0 or beta > 1.0:
        return 0.0

    try:
        # z 参数 — Hagan 2002: z = (nu / alpha) * (F*K)^((1-beta)/2) * ln(F/K)
        fk_mid = (F * K) ** ((1.0 - beta) / 2.0)
        if fk_mid <= 0 or alpha <= 0:
            return alpha

        z = (nu / alpha) * fk_mid * math.log(F / K)

        # χ(z) 函数（约化 z）
        disc = 1.0 - 2.0 * rho * z + z * z
        if disc < 0:
            disc = 0.0  # 浮点精度守卫
        sqrt_disc = math.sqrt(disc)
        denom_chi = 1.0 - rho
        if abs(denom_chi) < 1e-12:
            denom_chi = 1e-12  # 避免 rho≈1 时除零
        arg = (sqrt_disc + z - rho) / denom_chi
        if arg <= 0:
            return alpha  # 降级
        chi = math.log(arg)

        # 避免 chi 为 0
        if abs(chi) < 1e-12:
            chi = 1e-12

        # B(z, chi) 项
        numerator = z
        denominator = chi
        B = numerator / denominator

        # Hagan 公式中 B*z/chi 项已在上面计算，D 不单独出现
        # 这里保留结构注释以便后续扩展

        # σ_SABR = (alpha / (F^(1-beta)) * B) * D * (1 + terms...)
        # 近似忽略高阶项
        base_vol = alpha / (F ** (1.0 - beta)) if beta < 1.0 else alpha

        # 修正项（Hagan 2002 公式）
        fk = F * K
        if fk > 0:
            fk_sqrt = math.sqrt(fk)
            term1 = (1.0 - beta) ** 2 / 24.0 * (alpha / fk_sqrt) ** 2
            term2 = rho * beta * nu * alpha / (4.0 * fk_sqrt)
            term3 = (2.0 - 3.0 * rho * rho) / 24.0 * nu * nu

            sigma = base_vol * B * (1.0 + T * (term1 + term2 + term3))
        else:
            sigma = base_vol * B

        # 確保波動率为正且合理
        if sigma < 0 or sigma > 10.0:  # 10 = 1000% IV 上限
            return max(0.01, min(10.0, sigma))

        return sigma

    except (ValueError, ZeroDivisionError, OverflowError):
        return alpha  # 降级到 ATM 波动率


# ──────────────────────────────────────────────────────────────────────────────
# 纯 Python Nelder-Mead 优化器（scipy 不可用时的降级）
# ──────────────────────────────────────────────────────────────────────────────

def _nelder_mead_minimize(
    func,
    x0: List[float],
    maxiter: int = 500,
    tol: float = 1e-6
) -> Dict[str, Any]:
    """
    简单 Nelder-Mead 单纯形优化器（纯 Python 实现）。

    参数:
        func: 目标函数 f(x) → float
        x0: 初始点
        maxiter: 最大迭代次数
        tol: 收敛容差

    返回:
        {x: 最优点, fun: 最小值, nit: 迭代次数, success: bool}
    """
    n = len(x0)
    simplex = []

    # 初始化单纯形（n+1 个顶点）
    simplex.append(list(x0))
    for i in range(n):
        vertex = list(x0)
        vertex[i] += max(0.05, abs(x0[i]) * 0.1)  # 步长
        simplex.append(vertex)

    # 计算初始函数值
    values = [func(v) for v in simplex]

    for iteration in range(maxiter):
        # 按函数值排序
        sorted_indices = sorted(range(len(values)), key=lambda i: values[i])
        simplex = [simplex[i] for i in sorted_indices]
        values = [values[i] for i in sorted_indices]

        # 检查收敛
        if max(values) - min(values) < tol:
            break

        # 计算重心（排除最差点）
        centroid = [sum(simplex[i][j] for i in range(n)) / n for j in range(n)]

        # 反射
        reflected = [2 * centroid[j] - simplex[-1][j] for j in range(n)]
        reflected_value = func(reflected)

        if reflected_value < values[0]:
            # 扩展
            expanded = [centroid[j] + 2 * (reflected[j] - centroid[j]) for j in range(n)]
            expanded_value = func(expanded)
            if expanded_value < reflected_value:
                simplex[-1] = expanded
                values[-1] = expanded_value
            else:
                simplex[-1] = reflected
                values[-1] = reflected_value
        elif reflected_value < values[-2]:
            # 接受反射
            simplex[-1] = reflected
            values[-1] = reflected_value
        else:
            # 收缩
            if reflected_value < values[-1]:
                contracted = [centroid[j] + 0.5 * (reflected[j] - centroid[j]) for j in range(n)]
                contracted_value = func(contracted)
                if contracted_value < reflected_value:
                    simplex[-1] = contracted
                    values[-1] = contracted_value
                else:
                    simplex[-1] = reflected
                    values[-1] = reflected_value
            else:
                contracted = [centroid[j] + 0.5 * (simplex[-1][j] - centroid[j]) for j in range(n)]
                contracted_value = func(contracted)
                if contracted_value < values[-1]:
                    simplex[-1] = contracted
                    values[-1] = contracted_value
                else:
                    # 收缩整个单纯形
                    for i in range(1, len(simplex)):
                        for j in range(n):
                            simplex[i][j] = 0.5 * (simplex[0][j] + simplex[i][j])
                        values[i] = func(simplex[i])

    best_idx = values.index(min(values))
    return {
        "x": simplex[best_idx],
        "fun": values[best_idx],
        "nit": iteration + 1,
        "success": max(values) - min(values) < tol
    }


# ──────────────────────────────────────────────────────────────────────────────
# SABR 校准器
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SABRParams:
    """SABR 模型参数容器"""
    alpha: float
    beta: float
    rho: float
    nu: float
    fit_error: float
    n_strikes: int
    timestamp: str = ""

    def to_dict(self) -> Dict[str, float]:
        """转换为字典"""
        return {
            "alpha": self.alpha,
            "beta": self.beta,
            "rho": self.rho,
            "nu": self.nu,
            "fit_error": self.fit_error,
            "n_strikes": self.n_strikes
        }


class SABRCalibrator:
    """
    SABR 模型校准器。

    使用 scipy 或纯 Python 优化器拟合市场隐含波动率曲线。
    """

    def __init__(self, beta: float = 0.5):
        """
        初始化校准器。

        参数:
            beta: SABR beta 参数（通常对股票固定为 0.5）
        """
        self.beta = max(0.0, min(1.0, beta))  # 约束在 [0, 1]
        _log.debug(f"SABRCalibrator 初始化，beta={self.beta}")

    def calibrate(
        self,
        market_strikes: List[float],
        market_ivs: List[float],
        F: float,
        T: float
    ) -> SABRParams:
        """
        校准 SABR 参数以匹配市场 IV 曲线。

        参数:
            market_strikes: 行权价列表
            market_ivs: 对应的隐含波动率列表
            F: 远期价格
            T: 到期时间（年）

        返回:
            SABRParams 对象
        """
        if not market_strikes or not market_ivs:
            _log.warning("校准数据为空")
            return SABRParams(
                alpha=0.2, beta=self.beta, rho=0.0, nu=0.5,
                fit_error=float('inf'), n_strikes=0
            )

        if len(market_strikes) != len(market_ivs):
            raise ValueError("行权价和 IV 长度不匹配")

        # ATM IV 作为初始 alpha
        atm_iv = self._interpolate_iv(market_strikes, market_ivs, F)
        initial_alpha = max(0.01, atm_iv)

        # 初始点：alpha, rho, nu（beta 固定）
        x0 = [initial_alpha, 0.0, 0.3]

        def objective(x):
            """目标函数：计算模型 IV 与市场 IV 的 MSE"""
            alpha, rho, nu = x

            # 约束参数范围
            alpha = max(0.001, alpha)
            rho = max(-0.99, min(0.99, rho))
            nu = max(0.001, nu)

            squared_errors = []
            for strike, market_iv in zip(market_strikes, market_ivs):
                try:
                    model_iv = sabr_implied_vol(F, strike, T, alpha, self.beta, rho, nu)
                    error = (model_iv - market_iv) ** 2
                    squared_errors.append(error)
                except Exception as e:
                    _log.debug(f"校准计算异常：{e}")
                    squared_errors.append(1e6)

            return sum(squared_errors) / len(squared_errors) if squared_errors else 1e6

        # 使用 scipy 或纯 Python 优化
        try:
            if HAS_SCIPY:
                result = scipy_minimize(
                    objective,
                    x0,
                    method='Nelder-Mead',
                    options={'maxiter': 500, 'xatol': 1e-6, 'fatol': 1e-8}
                )
                optimal_x = result.x
                fit_error = result.fun
            else:
                result = _nelder_mead_minimize(objective, x0, maxiter=500, tol=1e-6)
                optimal_x = result["x"]
                fit_error = result["fun"]

            alpha, rho, nu = optimal_x
            alpha = max(0.001, alpha)
            rho = max(-0.99, min(0.99, rho))
            nu = max(0.001, nu)

            params = SABRParams(
                alpha=alpha,
                beta=self.beta,
                rho=rho,
                nu=nu,
                fit_error=fit_error,
                n_strikes=len(market_strikes)
            )

            _log.debug(f"校准完成：fit_error={fit_error:.6f}，参数={params.to_dict()}")
            return params

        except Exception as e:
            _log.warning(f"校准失败：{e}，使用默认参数")
            return SABRParams(
                alpha=initial_alpha, beta=self.beta, rho=0.0, nu=0.3,
                fit_error=1e6, n_strikes=len(market_strikes)
            )

    def generate_smile(
        self,
        F: float,
        T: float,
        params: SABRParams,
        strike_range: float = 0.3,
        n_points: int = 50
    ) -> List[Tuple[float, float]]:
        """
        根据校准参数生成 IV 微笑曲线。

        参数:
            F: 远期价格
            T: 到期时间（年）
            params: SABR 参数
            strike_range: 相对于 F 的范围（0.3 = ±30%）
            n_points: 采样点数

        返回:
            [(strike, iv), ...] 列表
        """
        strikes = [F * (1.0 - strike_range + 2 * strike_range * i / (n_points - 1))
                   for i in range(n_points)]

        smile = []
        for strike in strikes:
            try:
                iv = sabr_implied_vol(F, strike, T, params.alpha, params.beta,
                                      params.rho, params.nu)
                smile.append((strike, iv))
            except Exception as e:
                _log.debug(f"生成微笑失败：strike={strike}, {e}")

        return smile

    def detect_skew_anomaly(
        self,
        params: SABRParams,
        historical_params: Optional[List[SABRParams]] = None
    ) -> Dict[str, Any]:
        """
        检测 SABR 参数是否有异常偏离（与历史对比）。

        参数:
            params: 当前 SABR 参数
            historical_params: 历史参数列表（可选）

        返回:
            {
                'skew_z_score': float,
                'vol_of_vol_z_score': float,
                'is_anomalous': bool,
                'interpretation': str
            }
        """
        if not historical_params or len(historical_params) < 2:
            return {
                'skew_z_score': 0.0,
                'vol_of_vol_z_score': 0.0,
                'is_anomalous': False,
                'interpretation': "历史数据不足"
            }

        try:
            import statistics as stats

            # 计算历史 rho（偏度）统计
            historical_rho = [p.rho for p in historical_params]
            mean_rho = stats.mean(historical_rho)
            stdev_rho = stats.stdev(historical_rho) if len(historical_rho) > 1 else 0.1

            # 计算历史 nu（波动率的波动率）统计
            historical_nu = [p.nu for p in historical_params]
            mean_nu = stats.mean(historical_nu)
            stdev_nu = stats.stdev(historical_nu) if len(historical_nu) > 1 else 0.1

            # Z 分数
            skew_z = (params.rho - mean_rho) / max(stdev_rho, 0.01)
            vol_of_vol_z = (params.nu - mean_nu) / max(stdev_nu, 0.01)

            is_anomalous = abs(skew_z) > 2.0 or abs(vol_of_vol_z) > 2.0

            interpretation_parts = []
            if abs(skew_z) > 2.0:
                direction = "更负" if params.rho < mean_rho else "更正"
                interpretation_parts.append(f"偏度异常：{direction}（Z={skew_z:.2f}）")
            if abs(vol_of_vol_z) > 2.0:
                direction = "更高" if params.nu > mean_nu else "更低"
                interpretation_parts.append(f"波动率波动性{direction}（Z={vol_of_vol_z:.2f}）")

            interpretation = " | ".join(interpretation_parts) if interpretation_parts else "正常"

            return {
                'skew_z_score': skew_z,
                'vol_of_vol_z_score': vol_of_vol_z,
                'is_anomalous': is_anomalous,
                'interpretation': interpretation
            }

        except Exception as e:
            _log.warning(f"异常检测失败：{e}")
            return {
                'skew_z_score': 0.0,
                'vol_of_vol_z_score': 0.0,
                'is_anomalous': False,
                'interpretation': f"检测异常：{e}"
            }

    @staticmethod
    def _interpolate_iv(strikes: List[float], ivs: List[float], target: float) -> float:
        """线性插值获取目标行权价的 IV"""
        if not strikes or not ivs:
            return 0.2

        # 找到最接近的两个点
        sorted_pairs = sorted(zip(strikes, ivs))
        strikes_sorted = [s for s, _ in sorted_pairs]
        ivs_sorted = [iv for _, iv in sorted_pairs]

        if target <= strikes_sorted[0]:
            return ivs_sorted[0]
        if target >= strikes_sorted[-1]:
            return ivs_sorted[-1]

        for i in range(len(strikes_sorted) - 1):
            if strikes_sorted[i] <= target <= strikes_sorted[i + 1]:
                # 线性插值
                w = (target - strikes_sorted[i]) / (strikes_sorted[i + 1] - strikes_sorted[i])
                return ivs_sorted[i] * (1 - w) + ivs_sorted[i + 1] * w

        return ivs_sorted[0]


# ──────────────────────────────────────────────────────────────────────────────
# 波动率曲面管理
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ExpirySlice:
    """某个到期日的 SABR 切片"""
    expiration_days: int
    forward_price: float
    params: SABRParams
    smile: List[Tuple[float, float]] = field(default_factory=list)
    market_data: Dict[str, Any] = field(default_factory=dict)


class VolSurface:
    """
    波动率曲面管理器。

    为每个到期日维护一个 SABR 校准的微笑曲线，并提供
    插值、风险反转、蝴蝶等衍生指标。
    """

    def __init__(self, ticker: str):
        """
        初始化波动率曲面。

        参数:
            ticker: 股票代码
        """
        self.ticker = ticker
        self.calibrator = SABRCalibrator(beta=0.5)  # 对股票通常使用 beta=0.5
        self.slices: Dict[int, ExpirySlice] = {}
        self.historical_params: Dict[int, List[SABRParams]] = defaultdict(list)
        _log.debug(f"VolSurface 初始化：{ticker}")

    def build_surface(self, options_chain_data: List[Dict[str, Any]]) -> None:
        """
        从期权链数据构建波动率曲面。

        期望的数据格式:
            [{
                'strike': float,
                'expiration_days': int,
                'bid_iv': float,
                'ask_iv': float,
                'forward_price': float,
                ...
            }, ...]
        """
        if not options_chain_data:
            _log.warning("期权链数据为空")
            return

        # 按到期日分组
        by_expiry = defaultdict(list)
        for row in options_chain_data:
            exp_days = row.get('expiration_days', 0)
            if exp_days > 0:
                by_expiry[exp_days].append(row)

        # 对每个到期日进行 SABR 校准
        for exp_days in sorted(by_expiry.keys()):
            data = by_expiry[exp_days]

            # 提取市场数据
            strikes = []
            ivs = []
            for row in data:
                strike = row.get('strike')
                bid_iv = row.get('bid_iv', 0)
                ask_iv = row.get('ask_iv', 0)

                if strike and bid_iv and ask_iv:
                    strikes.append(strike)
                    # 使用 mid IV
                    ivs.append((bid_iv + ask_iv) / 2.0)

            if not strikes or not ivs:
                _log.debug(f"到期日 {exp_days}：无有效数据")
                continue

            # 远期价格（通常在数据中）
            forward = data[0].get('forward_price') or strikes[len(strikes) // 2]
            T = exp_days / 365.0

            # 校准 SABR
            params = self.calibrator.calibrate(strikes, ivs, forward, T)

            # 生成微笑曲线
            smile = self.calibrator.generate_smile(forward, T, params)

            # 保存
            slice_obj = ExpirySlice(
                expiration_days=exp_days,
                forward_price=forward,
                params=params,
                smile=smile,
                market_data={
                    'strikes': strikes,
                    'ivs': ivs,
                    'n_quotes': len(strikes)
                }
            )
            self.slices[exp_days] = slice_obj

            # 追加到历史记录
            self.historical_params[exp_days].append(params)

            _log.debug(
                f"{self.ticker} 到期日 {exp_days}d：校准完成，"
                f"params={params.to_dict()}"
            )

    def get_iv(self, strike: float, expiration_days: int) -> float:
        """
        获取某个行权价和到期日的隐含波动率（通过插值）。

        参数:
            strike: 行权价
            expiration_days: 到期天数

        返回:
            隐含波动率 (float)
        """
        # 精确匹配
        if expiration_days in self.slices:
            slice_obj = self.slices[expiration_days]
            # 使用 SABR 生成的微笑
            for s, iv in slice_obj.smile:
                if abs(s - strike) < 0.01 * strike:  # 1% 容差
                    return iv

            # 从 SABR 参数直接计算
            p = slice_obj.params
            T = expiration_days / 365.0
            return sabr_implied_vol(
                slice_obj.forward_price,
                strike,
                T,
                p.alpha,
                p.beta,
                p.rho,
                p.nu
            )

        # 跨到期日线性插值
        sorted_exp = sorted(self.slices.keys())
        if not sorted_exp:
            return 0.2  # 默认 IV

        if expiration_days <= sorted_exp[0]:
            return self.get_iv(strike, sorted_exp[0])
        if expiration_days >= sorted_exp[-1]:
            return self.get_iv(strike, sorted_exp[-1])

        # 找邻近的两个到期日
        for i in range(len(sorted_exp) - 1):
            if sorted_exp[i] <= expiration_days <= sorted_exp[i + 1]:
                exp1, exp2 = sorted_exp[i], sorted_exp[i + 1]
                iv1 = self.get_iv(strike, exp1)
                iv2 = self.get_iv(strike, exp2)
                w = (expiration_days - exp1) / (exp2 - exp1)
                return iv1 * (1 - w) + iv2 * w

        return 0.2

    def calculate_risk_reversal_25d(self, expiration_days: int) -> float:
        """
        计算 25-delta 风险反转（call IV - put IV）。

        参数:
            expiration_days: 到期天数

        返回:
            风险反转价差 (float)
        """
        if expiration_days not in self.slices:
            return 0.0

        slice_obj = self.slices[expiration_days]
        F = slice_obj.forward_price
        p = slice_obj.params
        T = expiration_days / 365.0

        try:
            # 25-delta call strike ≈ F * exp(0.25 * σ * √T)
            # 25-delta put strike ≈ F * exp(-0.25 * σ * √T)
            atm_iv = sabr_implied_vol(F, F, T, p.alpha, p.beta, p.rho, p.nu)
            shift = 0.25 * atm_iv * math.sqrt(T)

            call_strike = F * math.exp(shift)
            put_strike = F * math.exp(-shift)

            call_iv = sabr_implied_vol(F, call_strike, T, p.alpha, p.beta, p.rho, p.nu)
            put_iv = sabr_implied_vol(F, put_strike, T, p.alpha, p.beta, p.rho, p.nu)

            rr = call_iv - put_iv
            return rr

        except Exception as e:
            _log.warning(f"计算风险反转失败：{e}")
            return 0.0

    def calculate_butterfly_25d(self, expiration_days: int) -> float:
        """
        计算 25-delta 蝴蝶价差（ATM IV - (25d call IV + 25d put IV)/2）。

        参数:
            expiration_days: 到期天数

        返回:
            蝴蝶价差 (float)
        """
        if expiration_days not in self.slices:
            return 0.0

        slice_obj = self.slices[expiration_days]
        F = slice_obj.forward_price
        p = slice_obj.params
        T = expiration_days / 365.0

        try:
            atm_iv = sabr_implied_vol(F, F, T, p.alpha, p.beta, p.rho, p.nu)
            shift = 0.25 * atm_iv * math.sqrt(T)

            call_strike = F * math.exp(shift)
            put_strike = F * math.exp(-shift)

            call_iv = sabr_implied_vol(F, call_strike, T, p.alpha, p.beta, p.rho, p.nu)
            put_iv = sabr_implied_vol(F, put_strike, T, p.alpha, p.beta, p.rho, p.nu)

            butterfly = atm_iv - (call_iv + put_iv) / 2.0
            return butterfly

        except Exception as e:
            _log.warning(f"计算蝴蝶价差失败：{e}")
            return 0.0

    def detect_surface_anomalies(self) -> List[Dict[str, Any]]:
        """
        检测曲面异常（异常偏度、期限结构倒挂等）。

        返回:
            [{
                'type': str,  # 'skew_anomaly', 'term_inversion', ...
                'expiration_days': int,
                'severity': str,  # 'info', 'warning', 'critical'
                'description': str
            }, ...]
        """
        anomalies = []

        # 检测每个到期日的参数异常
        for exp_days in sorted(self.slices.keys()):
            slice_obj = self.slices[exp_days]
            params = slice_obj.params

            # 检查拟合质量
            if params.fit_error > 0.1:  # 10% MSE
                anomalies.append({
                    'type': 'poor_fit',
                    'expiration_days': exp_days,
                    'severity': 'warning',
                    'description': f"SABR 拟合质量差（fit_error={params.fit_error:.4f}）"
                })

            # 检查与历史的偏离
            if len(self.historical_params[exp_days]) > 1:
                hist = self.historical_params[exp_days]
                anomaly_check = self.calibrator.detect_skew_anomaly(params, hist)
                if anomaly_check['is_anomalous']:
                    anomalies.append({
                        'type': 'skew_anomaly',
                        'expiration_days': exp_days,
                        'severity': 'info',
                        'description': anomaly_check['interpretation']
                    })

        # 检查期限结构（相邻到期日的 rho 变化）
        sorted_exps = sorted(self.slices.keys())
        for i in range(len(sorted_exps) - 1):
            exp1, exp2 = sorted_exps[i], sorted_exps[i + 1]
            rho1 = self.slices[exp1].params.rho
            rho2 = self.slices[exp2].params.rho

            # 期限结构倒挂：rho 反向变化
            if (rho1 < 0 < rho2) or (rho2 < 0 < rho1):
                anomalies.append({
                    'type': 'term_inversion',
                    'expiration_days': exp2,
                    'severity': 'info',
                    'description': (
                        f"期限结构异常：{exp1}d rho={rho1:.3f} "
                        f"→ {exp2}d rho={rho2:.3f}"
                    )
                })

        return anomalies


# ──────────────────────────────────────────────────────────────────────────────
# 报告生成助手
# ──────────────────────────────────────────────────────────────────────────────

def format_surface_for_report(surface: VolSurface) -> str:
    """
    格式化波动率曲面为 HTML 卡片（供 CH4 使用）。

    参数:
        surface: VolSurface 实例

    返回:
        HTML 字符串
    """
    html_parts = [
        "<div style='border: 1px solid #ccc; padding: 12px; border-radius: 8px; "
        "margin: 8px 0; background: #f9f9f9;'>",
        "<h4 style='margin: 0 0 12px 0;'>波动率曲面</h4>"
    ]

    if not surface.slices:
        html_parts.append("<p>无波动率曲面数据</p>")
    else:
        html_parts.append("<table style='width: 100%; border-collapse: collapse;'>")
        html_parts.append(
            "<tr style='background: #f0f0f0;'>"
            "<th style='border: 1px solid #ddd; padding: 6px;'>到期日</th>"
            "<th style='border: 1px solid #ddd; padding: 6px;'>Alpha</th>"
            "<th style='border: 1px solid #ddd; padding: 6px;'>Rho</th>"
            "<th style='border: 1px solid #ddd; padding: 6px;'>Nu</th>"
            "<th style='border: 1px solid #ddd; padding: 6px;'>拟合误差</th>"
            "</tr>"
        )

        for exp_days in sorted(surface.slices.keys()):
            slice_obj = surface.slices[exp_days]
            p = slice_obj.params
            html_parts.append(
                f"<tr>"
                f"<td style='border: 1px solid #ddd; padding: 6px;'>{exp_days}d</td>"
                f"<td style='border: 1px solid #ddd; padding: 6px;'>{p.alpha:.4f}</td>"
                f"<td style='border: 1px solid #ddd; padding: 6px;'>{p.rho:.4f}</td>"
                f"<td style='border: 1px solid #ddd; padding: 6px;'>{p.nu:.4f}</td>"
                f"<td style='border: 1px solid #ddd; padding: 6px;'>{p.fit_error:.6f}</td>"
                f"</tr>"
            )

        html_parts.append("</table>")

    # 异常
    anomalies = surface.detect_surface_anomalies()
    if anomalies:
        html_parts.append("<h5 style='margin: 12px 0 6px 0;'>异常检测</h5>")
        for anom in anomalies:
            severity_color = {
                'info': '#0066cc',
                'warning': '#ff9900',
                'critical': '#cc0000'
            }.get(anom['severity'], '#666')

            html_parts.append(
                f"<p style='margin: 4px 0; color: {severity_color};'>"
                f"<strong>[{anom['type'].upper()}]</strong> "
                f"{anom['description']}</p>"
            )

    html_parts.append("</div>")
    return "\n".join(html_parts)


def format_skew_alert(anomaly: Dict[str, Any]) -> str:
    """
    格式化异常为警告文本。

    参数:
        anomaly: 异常字典（来自 detect_surface_anomalies()）

    返回:
        文本字符串
    """
    type_text = {
        'skew_anomaly': '偏度异常',
        'poor_fit': '拟合质量',
        'term_inversion': '期限倒挂'
    }.get(anomaly['type'], anomaly['type'])

    return (
        f"[{type_text}] {anomaly['description']} "
        f"({anomaly['severity'].upper()}, 到期日: {anomaly['expiration_days']}d)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 演示
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import random

    print("=" * 80)
    print("🐝 Alpha Hive · 波动率曲面演示")
    print("=" * 80)

    # 模拟期权链数据
    spot = 150.0
    forward = spot * 1.001

    # 30 天到期
    mock_options_30d = []
    strikes_30d = [spot * (1 + i * 0.02 - 0.20) for i in range(21)]  # -20% 到 +20%

    for k in strikes_30d:
        # 模拟微笑曲线
        moneyness = k / forward
        iv_atm = 0.25
        skew = 0.02 * (1.0 - moneyness)  # 负偏度

        iv_mid = iv_atm + skew + random.gauss(0, 0.01)
        iv_mid = max(0.05, min(0.80, iv_mid))

        mock_options_30d.append({
            'strike': k,
            'expiration_days': 30,
            'bid_iv': iv_mid * 0.98,
            'ask_iv': iv_mid * 1.02,
            'forward_price': forward
        })

    # 60 天到期
    mock_options_60d = []
    strikes_60d = [spot * (1 + i * 0.02 - 0.20) for i in range(21)]

    for k in strikes_60d:
        moneyness = k / forward
        iv_atm = 0.22
        skew = 0.015 * (1.0 - moneyness)

        iv_mid = iv_atm + skew + random.gauss(0, 0.01)
        iv_mid = max(0.05, min(0.80, iv_mid))

        mock_options_60d.append({
            'strike': k,
            'expiration_days': 60,
            'bid_iv': iv_mid * 0.98,
            'ask_iv': iv_mid * 1.02,
            'forward_price': forward
        })

    mock_data = mock_options_30d + mock_options_60d

    # 构建曲面
    surf = VolSurface("MOCK")
    surf.build_surface(mock_data)

    print("\n📊 波动率曲面构建完成")
    print(f"   包含到期日：{sorted(surf.slices.keys())}")

    # 查询 IV
    print("\n📈 样本查询：")
    print(f"   30d ATM IV: {surf.get_iv(forward, 30):.4f}")
    print(f"   30d +10% IV: {surf.get_iv(forward * 1.1, 30):.4f}")
    print(f"   30d -10% IV: {surf.get_iv(forward * 0.9, 30):.4f}")

    # 风险反转
    print("\n🔄 风险反转 & 蝴蝶：")
    rr30 = surf.calculate_risk_reversal_25d(30)
    bf30 = surf.calculate_butterfly_25d(30)
    print(f"   30d RR(25d): {rr30:.4f}")
    print(f"   30d BF(25d): {bf30:.4f}")

    # 异常检测
    anomalies = surf.detect_surface_anomalies()
    if anomalies:
        print("\n⚠️  异常检测：")
        for anom in anomalies:
            print(f"   - {format_skew_alert(anom)}")
    else:
        print("\n✓ 无异常检测")

    # 报告格式
    print("\n📄 报告格式（HTML）：")
    html = format_surface_for_report(surf)
    print(html[:200] + "...")

    print("\n" + "=" * 80)
