"""
Alpha Hive Greeks Engine — Black-Scholes Greeks Calculator
====================================================

Comprehensive Greeks calculation system for options analysis, including:
- Standard Greeks: Delta, Gamma, Theta, Vega, Rho
- Second-order Greeks: Vanna, Charm, Volga
- Portfolio aggregation and pin-risk detection

Used by: advanced_analyzer.py, options_analyzer.py, chart_engine.py

Dependencies: math, typing (no external packages)
Author: Alpha Hive Team
Version: 0.10.0
"""

import math
from typing import Dict, List, Tuple, Optional, Any
from datetime import datetime

# Try to import logger, fall back to print if not available
try:
    from hive_logger import _log
except ImportError:
    def _log(level: str, msg: str, **kwargs):
        print(f"[{level}] {msg}")


# ============================================================================
# HELPER FUNCTIONS — Normal Distribution
# ============================================================================

def _norm_pdf(x: float) -> float:
    """
    Standard normal probability density function.

    标准正态分布概率密度函数。

    Args:
        x: Input value

    Returns:
        PDF value
    """
    return math.exp(-x * x / 2.0) / math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    """
    Standard normal cumulative distribution function using error function.

    使用误差函数的标准正态分布累积分布函数。

    Args:
        x: Input value

    Returns:
        CDF value (probability)
    """
    # Handle extreme values
    if x > 6.0:
        return 1.0
    if x < -6.0:
        return 0.0

    # Use error function approximation
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """
    Inverse normal CDF (percent point function) — approximation.

    正态分布分位数函数近似。

    Args:
        p: Probability (0 < p < 1)

    Returns:
        Quantile value
    """
    if p <= 0.0 or p >= 1.0:
        return 0.0

    # Abramowitz & Stegun approximation
    if p < 0.5:
        t = math.sqrt(-2.0 * math.log(p))
        return -(t - (2.515517 + 0.802853 * t + 0.010328 * t * t) /
                     (1.0 + 1.432788 * t + 0.189269 * t * t + 0.001308 * t * t * t))
    else:
        t = math.sqrt(-2.0 * math.log(1.0 - p))
        return (t - (2.515517 + 0.802853 * t + 0.010328 * t * t) /
                (1.0 + 1.432788 * t + 0.189269 * t * t + 0.001308 * t * t * t))


# ============================================================================
# BLACK-SCHOLES PRICING
# ============================================================================

def bs_price(S: float, K: float, T: float, r: float, sigma: float,
             option_type: str = 'call') -> float:
    """
    Black-Scholes theoretical option price.

    Black-Scholes 理论期权价格。

    Args:
        S: Current stock price / 当前股价
        K: Strike price / 行权价
        T: Time to expiration in years / 到期时间（年）
        r: Risk-free rate / 无风险利率
        sigma: Volatility (annualized) / 波动率（年化）
        option_type: 'call' or 'put'

    Returns:
        Theoretical option price

    Edge cases:
        - T <= 0: intrinsic value only
        - sigma <= 0: intrinsic value only
        - S <= 0: returns 0
    """
    if S <= 0 or K <= 0:
        return 0.0

    # Intrinsic value for expired or zero-vol options
    if T <= 0:
        if option_type == 'call':
            return max(S - K, 0.0)
        else:
            return max(K - S, 0.0)

    if sigma <= 0:
        if option_type == 'call':
            return max(S - K * math.exp(-r * T), 0.0)
        else:
            return max(K * math.exp(-r * T) - S, 0.0)

    # d1, d2 calculation
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if option_type == 'call':
        price = S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:  # put
        price = K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

    return max(price, 0.0)


# ============================================================================
# GREEKS CALCULATION
# ============================================================================

def calculate_single(S: float, K: float, T: float, r: float, sigma: float,
                     option_type: str = 'call',
                     spot_shock: float = 0.01,
                     vol_shock: float = 0.01) -> Dict[str, float]:
    """
    Calculate all Greeks for a single option contract.

    计算单个期权合约的所有希腊字母。

    Args:
        S: Current stock price / 当前股价
        K: Strike price / 行权价
        T: Time to expiration in years / 到期时间（年）
        r: Risk-free rate / 无风险利率
        sigma: Volatility (annualized) / 波动率（年化）
        option_type: 'call' or 'put'
        spot_shock: Spot shock for numerical derivatives / 价格冲击（数值偏导）
        vol_shock: Vol shock for numerical derivatives / 波动率冲击

    Returns:
        Dict with Greeks: delta, gamma, theta, vega, rho, vanna, charm, volga

    Note:
        - Theta is expressed as daily decay (divide by 365)
        - Vega is per 1% IV move (0.01 = 1%)
        - Charm is daily delta change (per day)
        - All values reference call option conventions
    """
    result = {
        'S': S,
        'K': K,
        'T': T,
        'r': r,
        'sigma': sigma,
        'option_type': option_type,
    }

    # Handle edge cases
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        result.update({
            'delta': 0.0,
            'gamma': 0.0,
            'theta': 0.0,
            'vega': 0.0,
            'rho': 0.0,
            'vanna': 0.0,
            'charm': 0.0,
            'volga': 0.0,
            'price': 0.0,
        })
        return result

    # Base price and d1, d2
    price = bs_price(S, K, T, r, sigma, option_type)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    sqrt_T = math.sqrt(T)
    pdf_d1 = _norm_pdf(d1)
    cdf_d1 = _norm_cdf(d1)
    cdf_d2 = _norm_cdf(d2)
    cdf_neg_d1 = _norm_cdf(-d1)
    cdf_neg_d2 = _norm_cdf(-d2)

    # ========== Delta ==========
    if option_type == 'call':
        delta = cdf_d1
    else:
        delta = cdf_d1 - 1.0

    # ========== Gamma ==========
    # Same for call and put
    gamma = pdf_d1 / (S * sigma * sqrt_T)

    # ========== Theta (daily decay) ==========
    if option_type == 'call':
        theta_annual = (-S * pdf_d1 * sigma / (2.0 * sqrt_T)
                        - r * K * math.exp(-r * T) * cdf_d2)
    else:
        theta_annual = (-S * pdf_d1 * sigma / (2.0 * sqrt_T)
                        + r * K * math.exp(-r * T) * cdf_neg_d2)

    theta_daily = theta_annual / 365.0

    # ========== Vega (per 1% vol move) ==========
    # Vega per 0.01 (1%) vol change — same for call and put
    vega = S * pdf_d1 * sqrt_T / 100.0

    # ========== Rho (per 1% rate change) ==========
    if option_type == 'call':
        rho = K * T * math.exp(-r * T) * cdf_d2 / 100.0
    else:
        rho = -K * T * math.exp(-r * T) * cdf_neg_d2 / 100.0

    # ========== Vanna (dDelta/dVol) ==========
    # Sensitivity of delta to vol changes
    # Vanna = -pdf(d1) * d2 / sigma
    vanna = -pdf_d1 * d2 / sigma

    # ========== Charm (dDelta/dTime) — daily ==========
    # Rate at which delta changes per day
    # Charm = -d1 * pdf(d1) / (2*T) + r * delta (for call)
    if option_type == 'call':
        charm_annual = (-d1 * pdf_d1 / (2.0 * T) - r * cdf_d1)
    else:
        charm_annual = (-d1 * pdf_d1 / (2.0 * T) + r * cdf_neg_d1)

    charm_daily = charm_annual / 365.0

    # ========== Volga (dVega/dVol) ==========
    # Sensitivity of vega to vol changes
    # Volga = vega * d1 * d2 / sigma
    volga = (S * pdf_d1 * sqrt_T * d1 * d2) / (sigma * 100.0)

    result.update({
        'price': price,
        'delta': delta,
        'gamma': gamma,
        'theta': theta_daily,  # Daily decay
        'vega': vega,  # Per 1% vol move
        'rho': rho,  # Per 1% rate change
        'vanna': vanna,
        'charm': charm_daily,  # Daily
        'volga': volga,
    })

    return result


# ============================================================================
# IMPLIED VOLATILITY SOLVER
# ============================================================================

def implied_vol_newton(market_price: float, S: float, K: float, T: float,
                       r: float, option_type: str = 'call',
                       max_iterations: int = 100,
                       tolerance: float = 1e-6) -> Optional[float]:
    """
    Calculate implied volatility using Newton-Raphson method.

    使用 Newton-Raphson 方法计算隐含波动率。

    Args:
        market_price: Observed market option price / 市场价格
        S: Current stock price / 当前股价
        K: Strike price / 行权价
        T: Time to expiration in years / 到期时间（年）
        r: Risk-free rate / 无风险利率
        option_type: 'call' or 'put'
        max_iterations: Max iterations before stopping
        tolerance: Convergence tolerance

    Returns:
        Implied volatility (0.0-10.0 range), or None if no convergence

    Note:
        - Returns None if no solution found
        - Uses vega as derivative
        - Starting guess is sqrt(2π/T) * (market_price / S)
    """
    if S <= 0 or K <= 0 or T <= 0 or market_price <= 0:
        return None

    # Intrinsic value check
    if option_type == 'call':
        intrinsic = max(S - K, 0.0)
    else:
        intrinsic = max(K - S, 0.0)

    if market_price < intrinsic * 0.99:
        return None

    # Initial guess
    sigma = math.sqrt(2.0 * math.pi / T) * (market_price / S)
    sigma = max(0.01, min(sigma, 3.0))  # Clamp to reasonable range

    for iteration in range(max_iterations):
        # Calculate price and vega at current sigma
        greeks = calculate_single(S, K, T, r, sigma, option_type)
        price = greeks['price']
        vega = greeks['vega']

        # Newton-Raphson update
        diff = price - market_price

        if abs(diff) < tolerance:
            return sigma

        if vega < 1e-8:
            return None

        sigma = sigma - diff / vega
        sigma = max(0.001, min(sigma, 5.0))  # Keep in bounds

    return sigma if abs(bs_price(S, K, T, r, sigma, option_type) - market_price) < tolerance * 100 else None


# ============================================================================
# GREEKS ENGINE CLASS
# ============================================================================

class GreeksEngine:
    """
    Comprehensive Greeks calculation engine for options analysis.

    期权分析的完整希腊字母计算引擎。

    Methods:
        - calculate_single: Single contract Greeks
        - calculate_chain: Enriched option chain
        - calculate_portfolio_greeks: Net portfolio Greeks
        - find_max_gamma_strike: Identify pin risk level
        - find_max_vanna_strikes: IV spike sensitivity
        - calculate_charm_decay_map: Theta map across chain
    """

    def __init__(self, log_enabled: bool = True):
        """
        Initialize Greeks engine.

        Args:
            log_enabled: Enable logging output
        """
        self.log_enabled = log_enabled

    def _log(self, msg: str, level: str = 'INFO'):
        """Internal logging helper."""
        if self.log_enabled:
            _log(level, msg)

    def calculate_single(self, S: float, K: float, T: float, r: float,
                         sigma: float, option_type: str = 'call') -> Dict[str, float]:
        """
        Calculate all Greeks for a single option.

        计算单个期权的所有希腊字母。

        Args:
            S, K, T, r, sigma: Black-Scholes parameters
            option_type: 'call' or 'put'

        Returns:
            Dictionary with all Greeks
        """
        return calculate_single(S, K, T, r, sigma, option_type)

    def calculate_chain(self, ticker: str, stock_price: float,
                        chain_data: List[Dict[str, Any]], r: float = 0.04,
                        dte_key: str = 'dte', strike_key: str = 'strike',
                        price_key: str = 'price', iv_key: str = 'impliedVolatility') -> List[Dict[str, Any]]:
        """
        Enrich option chain with Greeks for all strikes.

        使用希腊字母丰富期权链数据。

        Args:
            ticker: Ticker symbol (for logging)
            stock_price: Current stock price
            chain_data: List of option contracts
            r: Risk-free rate
            dte_key: Key for days to expiration in chain_data
            strike_key: Key for strike price
            price_key: Key for option price
            iv_key: Key for implied volatility

        Returns:
            Enhanced chain with 'greeks' dict per contract

        Example:
            chain = [{'strike': 150, 'dte': 7, 'price': 2.5, 'impliedVolatility': 0.25, ...}, ...]
            enriched = engine.calculate_chain('NVDA', 160.0, chain)
        """
        enriched = []

        for contract in chain_data:
            try:
                K = contract.get(strike_key, 0.0)
                dte = contract.get(dte_key, 0.0)
                mid_price = contract.get(price_key, 0.0)
                iv = contract.get(iv_key, 0.25)
                option_type = contract.get('type', 'call').lower()

                # Convert DTE to years
                T = dte / 365.0 if dte > 0 else 0.0

                # Calculate greeks
                greeks = self.calculate_single(stock_price, K, T, r, iv, option_type)

                # Add to contract
                contract_copy = contract.copy()
                contract_copy['greeks'] = greeks
                enriched.append(contract_copy)

            except Exception as e:
                self._log(f"Error calculating Greeks for {ticker} strike {contract.get(strike_key)}: {e}", 'WARN')
                contract['greeks'] = {}
                enriched.append(contract)

        return enriched

    def calculate_portfolio_greeks(self, positions: List[Dict[str, Any]]) -> Dict[str, float]:
        """
        Aggregate Greeks across a portfolio of positions.

        聚合投资组合的希腊字母。

        Args:
            positions: List of dicts with keys:
                - 'quantity': int (positive = long, negative = short)
                - 'greeks': dict with greek values

        Returns:
            Portfolio-level Greeks (summed across positions)

        Example:
            portfolio = [
                {'quantity': 10, 'greeks': {'delta': 0.7, 'gamma': 0.02, ...}},
                {'quantity': -5, 'greeks': {'delta': 0.3, 'gamma': 0.01, ...}},
            ]
            net = engine.calculate_portfolio_greeks(portfolio)
        """
        portfolio_greeks = {
            'delta': 0.0,
            'gamma': 0.0,
            'theta': 0.0,
            'vega': 0.0,
            'rho': 0.0,
            'vanna': 0.0,
            'charm': 0.0,
            'volga': 0.0,
        }

        for position in positions:
            qty = position.get('quantity', 0)
            greeks = position.get('greeks', {})

            for greek_name in portfolio_greeks.keys():
                portfolio_greeks[greek_name] += qty * greeks.get(greek_name, 0.0)

        return portfolio_greeks

    def find_max_gamma_strike(self, chain_data: List[Dict[str, Any]],
                              stock_price: float,
                              strike_key: str = 'strike',
                              greeks_key: str = 'greeks') -> Tuple[Optional[float], float]:
        """
        Find the strike with maximum gamma (pin risk level).

        查找最大伽玛值的行权价（Pin Risk 水位）。

        Args:
            chain_data: Enriched chain with Greeks
            stock_price: Current stock price
            strike_key: Key for strike price
            greeks_key: Key for greeks dict

        Returns:
            Tuple (strike_with_max_gamma, max_gamma_value)

        Note:
            - Max gamma typically near ATM strike
            - Indicates highest convexity/acceleration risk
            - Used to identify option expiration pin risk
        """
        max_gamma = -1.0
        max_gamma_strike = None

        for contract in chain_data:
            greeks = contract.get(greeks_key, {})
            gamma = greeks.get('gamma', 0.0)

            if gamma > max_gamma:
                max_gamma = gamma
                max_gamma_strike = contract.get(strike_key)

        return max_gamma_strike, max_gamma

    def find_max_vanna_strikes(self, chain_data: List[Dict[str, Any]],
                               stock_price: float, vol_move: float = 0.05,
                               n_strikes: int = 3, greeks_key: str = 'greeks',
                               strike_key: str = 'strike') -> List[Tuple[float, float, float]]:
        """
        Find strikes most sensitive to IV changes (vanna sensitivity).

        查找对 IV 变化最敏感的行权价（Vanna 敏感性）。

        Args:
            chain_data: Enriched chain with Greeks
            stock_price: Current stock price (for context)
            vol_move: Expected IV move for calculation (e.g., 0.05 = 5%)
            n_strikes: Number of top vanna strikes to return
            greeks_key: Key for greeks dict
            strike_key: Key for strike price

        Returns:
            List of tuples: (strike, vanna, delta_move)
            - delta_move = vanna * vol_move (predicted delta change per IV move)

        Note:
            - High vanna means delta is very sensitive to IV spikes
            - Critical for volatility-driven trading
            - Usually found OTM (wings have high vanna)
        """
        vanna_data = []

        for contract in chain_data:
            greeks = contract.get(greeks_key, {})
            vanna = abs(greeks.get('vanna', 0.0))
            strike = contract.get(strike_key)
            delta_move = greeks.get('vanna', 0.0) * vol_move

            vanna_data.append((strike, vanna, delta_move))

        # Sort by vanna (descending)
        vanna_data.sort(key=lambda x: x[1], reverse=True)

        return vanna_data[:n_strikes]

    def calculate_charm_decay_map(self, chain_data: List[Dict[str, Any]],
                                   stock_price: float, days_ahead: int = 1,
                                   strike_key: str = 'strike',
                                   greeks_key: str = 'greeks') -> Dict[float, float]:
        """
        Map daily delta change (charm) across strike range.

        映射行权价范围的每日德尔塔变化（Charm）。

        Args:
            chain_data: Enriched chain with Greeks
            stock_price: Current stock price (for context)
            days_ahead: Number of days for projection (typically 1)
            strike_key: Key for strike price
            greeks_key: Key for greeks dict

        Returns:
            Dict mapping strike -> charm_daily (next-day delta change)

        Example:
            charm_map = engine.calculate_charm_decay_map(enriched_chain, 160.0)
            # {150.0: -0.002, 155.0: -0.003, 160.0: -0.005, ...}
            # Negative = decay, delta gets smaller each day (theta bleed)
        """
        charm_map = {}

        for contract in chain_data:
            greeks = contract.get(greeks_key, {})
            charm = greeks.get('charm', 0.0)
            strike = contract.get(strike_key)

            charm_map[strike] = charm * days_ahead

        return charm_map

    def calculate_pin_risk_score(self, chain_data: List[Dict[str, Any]],
                                 stock_price: float,
                                 oi_key: str = 'openInterest',
                                 greeks_key: str = 'greeks',
                                 strike_key: str = 'strike') -> Tuple[float, float]:
        """
        Calculate expiration pin risk score (gamma concentration).

        计算到期日 Pin Risk 评分（伽玛集中度）。

        Args:
            chain_data: Enriched chain with Greeks
            stock_price: Current stock price
            oi_key: Key for open interest
            greeks_key: Key for greeks dict
            strike_key: Key for strike price

        Returns:
            Tuple (pin_risk_score: 0-100, concentration_strike)
            - 0-20: Low concentration, smooth surface
            - 20-50: Moderate concentration
            - 50-80: High pin risk
            - 80-100: Extreme concentration, expiration may pin to this level
        """
        # Find ATM strike
        atm_strike = min(chain_data, key=lambda x: abs(x.get(strike_key, 0) - stock_price),
                         default={}).get(strike_key, stock_price)

        # Calculate total gamma and concentration within ±2 strikes
        strike_list = sorted([c.get(strike_key, 0) for c in chain_data if c.get(strike_key)])
        if not strike_list:
            return 0.0, stock_price

        strike_width = strike_list[1] - strike_list[0] if len(strike_list) > 1 else 1.0
        concentration_range = atm_strike - strike_width, atm_strike + strike_width

        total_gamma = 0.0
        concentrated_gamma = 0.0
        max_oi = 0.0
        max_oi_strike = atm_strike

        for contract in chain_data:
            greeks = contract.get(greeks_key, {})
            gamma = greeks.get('gamma', 0.0)
            oi = contract.get(oi_key, 0)
            strike = contract.get(strike_key, 0)

            weighted_gamma = gamma * max(oi, 1)
            total_gamma += weighted_gamma

            if concentration_range[0] <= strike <= concentration_range[1]:
                concentrated_gamma += weighted_gamma

            if oi > max_oi:
                max_oi = oi
                max_oi_strike = strike

        # Calculate concentration ratio
        if total_gamma > 0:
            concentration_ratio = concentrated_gamma / total_gamma
            pin_score = concentration_ratio * 100.0
            pin_score = min(100.0, max(0.0, pin_score))
        else:
            pin_score = 0.0

        return pin_score, max_oi_strike


# ============================================================================
# FORMATTING HELPERS
# ============================================================================

def format_greeks_for_report(greeks_result: Dict[str, float], contract_id: str = "",
                             price: float = 0.0, oi: int = 0) -> str:
    """
    Format Greeks calculation into HTML card for CH4 report.

    将希腊字母计算格式化为 CH4 报告的 HTML 卡片。

    Args:
        greeks_result: Dict from calculate_single()
        contract_id: Optional contract identifier
        price: Current price
        oi: Open interest

    Returns:
        HTML string for embedding in report

    Note:
        - Color-coded for quick visual scanning
        - Includes tooltips for each Greek
        - Responsive mobile design
    """
    S = greeks_result.get('S', 0)
    K = greeks_result.get('K', 0)
    option_type = greeks_result.get('option_type', 'call')

    delta = greeks_result.get('delta', 0)
    gamma = greeks_result.get('gamma', 0)
    theta = greeks_result.get('theta', 0)
    vega = greeks_result.get('vega', 0)
    rho = greeks_result.get('rho', 0)
    vanna = greeks_result.get('vanna', 0)
    charm = greeks_result.get('charm', 0)
    volga = greeks_result.get('volga', 0)

    # Color mapping
    def delta_color(d):
        if d > 0.7:
            return '#27ae60'  # Green — deep ITM
        elif d > 0.3:
            return '#f39c12'  # Orange — ITM
        elif d > 0.0:
            return '#e74c3c'  # Red — OTM
        else:
            return '#95a5a6'  # Gray — puts

    def gamma_color(g):
        if g > 0.01:
            return '#e74c3c'  # Red — high gamma
        else:
            return '#3498db'  # Blue — low gamma

    html = f"""
    <div style="background: #f8f9fa; border: 1px solid #dee2e6; border-radius: 8px; padding: 12px; margin: 8px 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto;">
        <div style="font-weight: 600; font-size: 13px; color: #2c3e50; margin-bottom: 8px;">
            {option_type.upper()} | S=${S:.2f} | K=${K:.2f} {f"| {contract_id}" if contract_id else ""}
        </div>
        <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; font-size: 12px;">
            <div style="text-align: center; padding: 6px; background: white; border-radius: 4px;">
                <div style="color: {delta_color(delta)}; font-weight: 600; font-size: 14px;">{delta:.3f}</div>
                <div style="color: #7f8c8d; font-size: 11px;">Δ Delta</div>
            </div>
            <div style="text-align: center; padding: 6px; background: white; border-radius: 4px;">
                <div style="color: {gamma_color(gamma)}; font-weight: 600; font-size: 14px;">{gamma:.5f}</div>
                <div style="color: #7f8c8d; font-size: 11px;">Γ Gamma</div>
            </div>
            <div style="text-align: center; padding: 6px; background: white; border-radius: 4px;">
                <div style="color: {'#27ae60' if theta < 0 else '#e74c3c'}; font-weight: 600; font-size: 14px;">{theta:.4f}</div>
                <div style="color: #7f8c8d; font-size: 11px;">Θ Theta/day</div>
            </div>
            <div style="text-align: center; padding: 6px; background: white; border-radius: 4px;">
                <div style="color: #3498db; font-weight: 600; font-size: 14px;">{vega:.3f}</div>
                <div style="color: #7f8c8d; font-size: 11px;">ν Vega/1%</div>
            </div>
            <div style="text-align: center; padding: 6px; background: white; border-radius: 4px;">
                <div style="color: #9b59b6; font-weight: 600; font-size: 14px;">{rho:.3f}</div>
                <div style="color: #7f8c8d; font-size: 11px;">ρ Rho/1%</div>
            </div>
            <div style="text-align: center; padding: 6px; background: white; border-radius: 4px;">
                <div style="color: #e67e22; font-weight: 600; font-size: 14px;">{vanna:.4f}</div>
                <div style="color: #7f8c8d; font-size: 11px;">Vanna</div>
            </div>
            <div style="text-align: center; padding: 6px; background: white; border-radius: 4px;">
                <div style="color: {'#27ae60' if charm > 0 else '#e74c3c'}; font-weight: 600; font-size: 14px;">{charm:.4f}</div>
                <div style="color: #7f8c8d; font-size: 11px;">Charm/day</div>
            </div>
            <div style="text-align: center; padding: 6px; background: white; border-radius: 4px;">
                <div style="color: #16a085; font-weight: 600; font-size: 14px;">{volga:.4f}</div>
                <div style="color: #7f8c8d; font-size: 11px;">Volga</div>
            </div>
        </div>
    </div>
    """
    return html


def format_portfolio_summary(portfolio_greeks: Dict[str, float], positions_count: int = 0) -> str:
    """
    Format portfolio Greeks summary as compact text.

    将投资组合希腊字母摘要格式化为紧凑文本。

    Args:
        portfolio_greeks: Dict from calculate_portfolio_greeks()
        positions_count: Number of positions in portfolio

    Returns:
        Formatted text summary

    Example:
        Portfolio Summary | 5 positions
        Δ: +0.850 | Γ: +0.0150 | Θ: -0.0230/day | ν: +1.20 | ρ: +0.30
        Vanna: +0.05 | Charm: -0.015/day | Volga: +0.08
    """
    delta = portfolio_greeks.get('delta', 0)
    gamma = portfolio_greeks.get('gamma', 0)
    theta = portfolio_greeks.get('theta', 0)
    vega = portfolio_greeks.get('vega', 0)
    rho = portfolio_greeks.get('rho', 0)
    vanna = portfolio_greeks.get('vanna', 0)
    charm = portfolio_greeks.get('charm', 0)
    volga = portfolio_greeks.get('volga', 0)

    positions_str = f" | {positions_count} positions" if positions_count > 0 else ""

    line1 = f"Portfolio Summary{positions_str}\n"
    line2 = f"Δ: {delta:+.3f} | Γ: {gamma:+.4f} | Θ: {theta:+.4f}/day | ν: {vega:+.2f} | ρ: {rho:+.2f}\n"
    line3 = f"Vanna: {vanna:+.4f} | Charm: {charm:+.4f}/day | Volga: {volga:+.4f}"

    return line1 + line2 + line3


# ============================================================================
# DEMO / TEST SECTION
# ============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("Alpha Hive Greeks Engine — Demo")
    print("=" * 70)

    # Demo 1: Single contract Greeks
    print("\n[Demo 1] Single Contract Greeks")
    print("-" * 70)

    S, K, T, r, sigma = 160.0, 155.0, 7/365, 0.04, 0.35
    greeks = calculate_single(S, K, T, r, sigma, 'call')

    print(f"Stock Price: ${S}")
    print(f"Strike: ${K}, Call, DTE: 7, Vol: {sigma*100:.1f}%")
    print(f"Theoretical Price: ${greeks['price']:.2f}")
    print(f"Δ: {greeks['delta']:.3f}")
    print(f"Γ: {greeks['gamma']:.5f}")
    print(f"Θ: {greeks['theta']:.4f} (daily decay)")
    print(f"ν: {greeks['vega']:.3f} (per 1% vol)")
    print(f"ρ: {greeks['rho']:.3f} (per 1% rate)")
    print(f"Vanna: {greeks['vanna']:.4f}")
    print(f"Charm: {greeks['charm']:.4f} (daily delta change)")
    print(f"Volga: {greeks['volga']:.4f}")

    # Demo 2: Implied Volatility
    print("\n[Demo 2] Implied Volatility Solver")
    print("-" * 70)

    market_price = 4.50
    implied_vol = implied_vol_newton(market_price, S, K, T, r, 'call')
    if implied_vol:
        print(f"Market Price: ${market_price:.2f}")
        print(f"Implied Vol: {implied_vol*100:.2f}%")
        print(f"Verification (price at IV): ${bs_price(S, K, T, r, implied_vol, 'call'):.2f}")
    else:
        print("Could not solve for implied volatility")

    # Demo 3: Greeks Engine
    print("\n[Demo 3] Greeks Engine — Chain Enrichment")
    print("-" * 70)

    engine = GreeksEngine(log_enabled=True)

    # Create mock option chain
    chain = [
        {'type': 'call', 'strike': 150.0, 'dte': 7, 'price': 10.5, 'impliedVolatility': 0.35},
        {'type': 'call', 'strike': 155.0, 'dte': 7, 'price': 6.2, 'impliedVolatility': 0.35},
        {'type': 'call', 'strike': 160.0, 'dte': 7, 'price': 3.1, 'impliedVolatility': 0.35},
        {'type': 'call', 'strike': 165.0, 'dte': 7, 'price': 1.4, 'impliedVolatility': 0.35},
        {'type': 'call', 'strike': 170.0, 'dte': 7, 'price': 0.6, 'impliedVolatility': 0.35},
    ]

    enriched_chain = engine.calculate_chain('NVDA', 160.0, chain)

    print(f"Enriched {len(enriched_chain)} call contracts with Greeks")
    for contract in enriched_chain:
        g = contract['greeks']
        print(f"  K={contract['strike']}: Δ={g['delta']:.3f}, Γ={g['gamma']:.5f}, Θ={g['theta']:.4f}, ν={g['vega']:.2f}")

    # Demo 4: Pin Risk Detection
    print("\n[Demo 4] Pin Risk Detection")
    print("-" * 70)

    max_gamma_strike, max_gamma = engine.find_max_gamma_strike(enriched_chain, 160.0)
    print(f"Max Gamma Strike: ${max_gamma_strike:.2f} (Γ={max_gamma:.5f})")

    vanna_strikes = engine.find_max_vanna_strikes(enriched_chain, 160.0, vol_move=0.05, n_strikes=3)
    print(f"Top 3 Vanna Sensitive Strikes (for 5% IV move):")
    for strike, vanna, delta_move in vanna_strikes:
        print(f"  K=${strike:.2f}: Vanna={vanna:.4f}, ΔDelta={delta_move:+.4f}")

    # Demo 5: Portfolio Greeks
    print("\n[Demo 5] Portfolio Greeks Aggregation")
    print("-" * 70)

    positions = [
        {'quantity': 10, 'greeks': enriched_chain[2]['greeks']},   # Long 10 ATM calls
        {'quantity': -5, 'greeks': enriched_chain[1]['greeks']},    # Short 5 ITM calls
        {'quantity': 20, 'greeks': enriched_chain[4]['greeks']},    # Long 20 OTM calls
    ]

    portfolio_greeks = engine.calculate_portfolio_greeks(positions)
    summary = format_portfolio_summary(portfolio_greeks, positions_count=len(positions))
    print(summary)

    # Demo 6: HTML Card
    print("\n[Demo 6] HTML Card Output")
    print("-" * 70)

    html_card = format_greeks_for_report(greeks, contract_id="NVDA 160C 7DTE", price=3.1, oi=50000)
    print(html_card[:200] + "... (truncated)")

    print("\n" + "=" * 70)
    print("Demo Complete")
    print("=" * 70)
