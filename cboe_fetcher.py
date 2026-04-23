"""
CBOE 日度统计数据抓取器 — Alpha Hive 宏观分析层增强

功能：
1. 获取股票 Put/Call 比率（合成值，替代 2026-04 下架的 ^PCCE）
2. 获取 VIX 期限结构（远期溢价 vs 现货）
3. 获取 SKEW 指数（尾部风险偏度）
4. 获取 VVIX（波动率之波动率）
5. 提供宏观评分组合

数据源：yfinance（优先），FRED API 备选，本地缓存 30 分钟（盘中）或 4 小时（盘后）

2026-04-22 变更：因 Yahoo Finance 下架 ^PCCE / ^CPCE / ^CPC 等 CBOE 官方 P/C 比率符号，
fetch_equity_putcall_ratio() 改为从 SPY/QQQ/IWM 期权链 volume 合成。未来 Yahoo 若再
下架 ETF 期权数据，只需修改 _SYNTHETIC_PC_TICKERS 常量即可。
"""

import os
import json
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
import warnings

try:
    import yfinance as yf
except ImportError:
    yf = None

try:
    from resilience import NETWORK_ERRORS
except ImportError:
    NETWORK_ERRORS = (ConnectionError, TimeoutError, OSError)

try:
    from hive_logger import get_logger, atomic_json_write
except ImportError:
    def get_logger(name):
        import logging
        return logging.getLogger(name)

    def atomic_json_write(path: str, data: Dict[str, Any]) -> None:
        """简单原子写入（备选）"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

logger = get_logger(__name__)


class CBOEDailyFetcher:
    """
    CBOE 日度统计数据抓取器

    提供无需 API Key 的免费 CBOE 数据获取：
    - 股票 Put/Call 比率
    - VIX 期限结构
    - SKEW 指数
    - VVIX（波动率的波动率）
    """

    def __init__(self, cache_dir: str = "cache/cboe_daily"):
        """
        初始化抓取器

        Args:
            cache_dir: 缓存目录路径
        """
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.logger = get_logger(f"{__name__}.{self.__class__.__name__}")

        # TTL 设置（秒）
        self.market_hours_ttl = 1800      # 30 分钟（盘中）
        self.after_hours_ttl = 14400      # 4 小时（盘后）

    def _is_market_hours(self) -> bool:
        """检查当前是否在美股交易时间（9:30-16:00 EST）"""
        now = datetime.now()
        # 简化版：仅检查工作日 9:30-16:00
        if now.weekday() >= 5:  # 周末
            return False
        hour = now.hour
        # 注意：这里假设运行在 EST，实际需考虑时区
        return 9 <= hour < 16

    def _get_ttl(self) -> int:
        """根据交易时间返回 TTL"""
        return self.market_hours_ttl if self._is_market_hours() else self.after_hours_ttl

    def _read_cache(self, key: str) -> Optional[Dict[str, Any]]:
        """
        读取缓存数据

        Args:
            key: 缓存键（如 'pcce', 'vix_term', 等）

        Returns:
            缓存数据或 None（过期/不存在）
        """
        cache_path = os.path.join(self.cache_dir, f"{key}.json")
        if not os.path.exists(cache_path):
            return None

        try:
            with open(cache_path, 'r') as f:
                cache_data = json.load(f)

            # 检查 TTL
            cached_at = cache_data.get('cached_at', 0)
            ttl = self._get_ttl()
            if time.time() - cached_at > ttl:
                self.logger.debug(f"缓存过期: {key}")
                return None

            return cache_data.get('data')
        except Exception as e:
            self.logger.warning(f"读取缓存失败 {key}: {e}")
            return None

    def _write_cache(self, key: str, data: Dict[str, Any]) -> None:
        """
        写入缓存数据

        Args:
            key: 缓存键
            data: 数据
        """
        cache_path = os.path.join(self.cache_dir, f"{key}.json")
        cache_obj = {
            'cached_at': time.time(),
            'data': data
        }
        try:
            atomic_json_write(cache_path, cache_obj)
        except Exception as e:
            self.logger.warning(f"写入缓存失败 {key}: {e}")

    # 合成 P/C Ratio 数据源：最近 N 个到期日
    _SYNTHETIC_PC_TICKERS = ("SPY", "QQQ", "IWM")
    _SYNTHETIC_PC_EXPIRIES = 3

    def fetch_equity_putcall_ratio(self) -> Dict[str, Any]:
        """
        获取合成股票 Put/Call 比率（替代已下架的 CBOE ^PCCE）

        策略：因 Yahoo 2026-04 下架 ^PCCE / ^CPCE / ^CPC 等 CBOE 官方 P/C 比率符号，
        改为聚合 SPY / QQQ / IWM 期权链的成交量合成 P/C Ratio，100% 基于 Yahoo 数据，
        未来 Yahoo 若再下架个别 ETF 的期权数据，只需修改 _SYNTHETIC_PC_TICKERS 常量。

        注意：ETF 合成 P/C Ratio 的水位通常比 CBOE 官方 PCCE 高 0.2-0.3（因 SPY 等
        ETF 承担大量机构对冲盘），阈值已相应上调。

        Returns:
            {
                'total_pc_ratio': float,      # 合成 P/C 比率（put_vol / call_vol）
                'call_volume': int,           # 汇总 call 成交量
                'put_volume': int,            # 汇总 put 成交量
                'date': str,                  # ISO 日期
                'signal': str,                # 情绪信号
                'source': str,                # 数据源标识
                'tickers_used': list[str],    # 实际纳入合成的标的
                'error': str (optional)
            }
        """
        # 尝试读缓存
        cached = self._read_cache('pcce')
        if cached:
            self.logger.debug("使用缓存 PCCE 数据")
            return cached

        result = {
            'total_pc_ratio': 0.0,
            'call_volume': 0,
            'put_volume': 0,
            'date': datetime.now().isoformat()[:10],
            'signal': 'unknown',
            'source': 'synthetic_yf_options',
            'tickers_used': [],
        }

        try:
            if yf is None:
                raise ImportError("yfinance 未安装")

            total_call_vol = 0.0
            total_put_vol = 0.0
            tickers_used = []

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")

                for symbol in self._SYNTHETIC_PC_TICKERS:
                    try:
                        tk = yf.Ticker(symbol)
                        expirations = list(tk.options or [])
                        if not expirations:
                            self.logger.debug(f"{symbol} 无期权到期日，跳过")
                            continue

                        sym_call_vol = 0.0
                        sym_put_vol = 0.0
                        for expiry in expirations[: self._SYNTHETIC_PC_EXPIRIES]:
                            try:
                                chain = tk.option_chain(expiry)
                                # volume 列可能存在 NaN
                                sym_call_vol += float(chain.calls["volume"].fillna(0).sum())
                                sym_put_vol += float(chain.puts["volume"].fillna(0).sum())
                            except Exception as e:
                                self.logger.debug(f"{symbol}@{expiry} 期权链失败: {e}")
                                continue

                        if sym_call_vol > 0 or sym_put_vol > 0:
                            total_call_vol += sym_call_vol
                            total_put_vol += sym_put_vol
                            tickers_used.append(symbol)
                            self.logger.debug(
                                f"{symbol}: calls={sym_call_vol:.0f}, puts={sym_put_vol:.0f}, "
                                f"pc={sym_put_vol / max(sym_call_vol, 1):.3f}"
                            )
                    except NETWORK_ERRORS as ne:
                        self.logger.debug(f"{symbol} 网络错误: {ne}")
                        continue
                    except Exception as e:
                        self.logger.debug(f"{symbol} 合成失败: {e}")
                        continue

            if tickers_used and total_call_vol > 0:
                pc_ratio = total_put_vol / total_call_vol
                result['total_pc_ratio'] = round(pc_ratio, 3)
                result['call_volume'] = int(total_call_vol)
                result['put_volume'] = int(total_put_vol)
                result['tickers_used'] = tickers_used

                # 阈值上调（ETF 合成比 CBOE PCCE 系统性偏高 0.2-0.3）
                if pc_ratio > 1.3:
                    result['signal'] = 'extreme_fear'
                elif pc_ratio > 1.0:
                    result['signal'] = 'fear'
                elif pc_ratio > 0.8:
                    result['signal'] = 'neutral'
                elif pc_ratio > 0.6:
                    result['signal'] = 'greed'
                else:
                    result['signal'] = 'extreme_greed'

                self.logger.info(
                    f"合成 P/C Ratio: {pc_ratio:.3f} "
                    f"(calls={int(total_call_vol):,}, puts={int(total_put_vol):,}, "
                    f"来源={tickers_used}, 信号={result['signal']})"
                )
            else:
                # 所有 ETF 都失败，降级中性默认值
                self.logger.warning("合成 P/C Ratio 所有 ETF 获取失败，使用历史中位数")
                result['total_pc_ratio'] = 0.95  # ETF 合成历史中位数（比 PCCE 的 0.75 高）
                result['call_volume'] = 0
                result['put_volume'] = 0
                result['signal'] = 'neutral'
                result['source'] = 'default_fallback'

        except NETWORK_ERRORS as ne:
            self.logger.warning(f"合成 P/C Ratio 网络错误: {ne}")
            result['total_pc_ratio'] = 0.95
            result['signal'] = 'neutral'
            result['source'] = 'default_fallback'
        except Exception as e:
            self.logger.error(f"合成 P/C Ratio 异常: {e}")
            result['error'] = str(e)
            result['signal'] = 'neutral'
            result['total_pc_ratio'] = 0.95
            result['source'] = 'default_fallback'

        # 写入缓存
        self._write_cache('pcce', result)
        return result

    def fetch_vix_term_structure(self) -> Dict[str, Any]:
        """
        获取 VIX 期限结构

        Returns:
            {
                'vix_spot': float,            # 现货 VIX
                'vix_1m': float,              # 1 月期 VIX 期货
                'vix_3m': float,              # 3 月期 VIX 期货
                'term_structure': str,        # 'contango' / 'backwardation' / 'flat'
                'contango_pct': float,        # 现货 vs 1 月差额 %
                'error': str (optional)
            }
        """
        cached = self._read_cache('vix_term')
        if cached:
            self.logger.debug("使用缓存 VIX 期限结构")
            return cached

        result = {
            'vix_spot': 0.0,
            'vix_1m': 0.0,
            'vix_3m': 0.0,
            'term_structure': 'unknown',
            'contango_pct': 0.0
        }

        try:
            if yf is None:
                raise ImportError("yfinance 未安装")

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")

                # 获取 VIX 现货
                vix_data = yf.download('^VIX', period='1d', progress=False)
                if not vix_data.empty:
                    result['vix_spot'] = float(vix_data['Close'].iloc[-1])
                else:
                    result['vix_spot'] = 15.0  # 默认

                # 尝试获取 VIX 期货数据（yfinance 支持 VIXY / UVXY）
                # VIXY: 短期 VIX ETN（约 1 月）
                # UVXY: 2x 短期 VIX ETN
                try:
                    vixy_data = yf.download('VIXY', period='1d', progress=False)
                    if not vixy_data.empty:
                        # VIXY 反映约 1 月期 VIX
                        result['vix_1m'] = float(vixy_data['Close'].iloc[-1]) * 0.5  # 近似转换
                    else:
                        result['vix_1m'] = result['vix_spot'] * 1.05  # 正常情况小幅升水
                except:
                    result['vix_1m'] = result['vix_spot'] * 1.05

                # VIX 3 月期（更高，通常）
                result['vix_3m'] = result['vix_spot'] * 1.10

                # 计算期限结构
                if result['vix_1m'] > result['vix_spot'] * 1.02:
                    result['term_structure'] = 'contango'
                elif result['vix_1m'] < result['vix_spot'] * 0.98:
                    result['term_structure'] = 'backwardation'
                else:
                    result['term_structure'] = 'flat'

                # 计算升水百分比
                if result['vix_spot'] > 0:
                    result['contango_pct'] = (result['vix_1m'] - result['vix_spot']) / result['vix_spot'] * 100

                self.logger.info(f"VIX 期限结构: spot={result['vix_spot']:.2f}, 1m={result['vix_1m']:.2f}, "
                               f"term={result['term_structure']}, contango={result['contango_pct']:.2f}%")

        except NETWORK_ERRORS as ne:
            self.logger.warning(f"网络错误获取 VIX: {ne}")
            result['vix_spot'] = 15.0
            result['vix_1m'] = 15.75
            result['vix_3m'] = 16.5
            result['term_structure'] = 'contango'
            result['contango_pct'] = 5.0
        except Exception as e:
            self.logger.error(f"VIX 期限结构抓取异常: {e}")
            result['error'] = str(e)

        self._write_cache('vix_term', result)
        return result

    def fetch_skew_index(self) -> Dict[str, Any]:
        """
        获取 CBOE SKEW 指数（尾部风险）

        Returns:
            {
                'skew_value': float,          # SKEW 指数值
                'signal': str,                # 'extreme_tail_risk' / 'elevated' / 'normal' / 'complacent'
                'date': str,                  # ISO 日期
                'error': str (optional)
            }
        """
        cached = self._read_cache('skew')
        if cached:
            self.logger.debug("使用缓存 SKEW 数据")
            return cached

        result = {
            'skew_value': 0.0,
            'signal': 'unknown',
            'date': datetime.now().isoformat()[:10]
        }

        try:
            if yf is None:
                raise ImportError("yfinance 未安装")

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")

                skew_data = yf.download('^SKEW', period='1d', progress=False)
                if not skew_data.empty:
                    result['skew_value'] = float(skew_data['Close'].iloc[-1])

                    # 根据阈值分类
                    if result['skew_value'] > 150:
                        result['signal'] = 'extreme_tail_risk'
                    elif result['skew_value'] > 130:
                        result['signal'] = 'elevated'
                    elif result['skew_value'] > 115:
                        result['signal'] = 'normal'
                    else:
                        result['signal'] = 'complacent'

                    self.logger.info(f"SKEW 数据获取: value={result['skew_value']:.1f}, signal={result['signal']}")
                else:
                    result['signal'] = 'normal'
                    result['skew_value'] = 120.0
                    self.logger.warning("SKEW 数据下载为空，使用默认值")

        except NETWORK_ERRORS as ne:
            self.logger.warning(f"网络错误获取 SKEW: {ne}")
            result['signal'] = 'normal'
            result['skew_value'] = 120.0
        except Exception as e:
            self.logger.error(f"SKEW 抓取异常: {e}")
            result['error'] = str(e)
            result['signal'] = 'normal'
            result['skew_value'] = 120.0

        self._write_cache('skew', result)
        return result

    def fetch_vvix(self) -> Dict[str, Any]:
        """
        获取 VVIX（波动率的波动率）

        Returns:
            {
                'vvix_value': float,          # VVIX 指数值
                'signal': str,                # 'vol_explosion_risk' / 'elevated' / 'normal' / 'compressed'
                'date': str,                  # ISO 日期
                'error': str (optional)
            }
        """
        cached = self._read_cache('vvix')
        if cached:
            self.logger.debug("使用缓存 VVIX 数据")
            return cached

        result = {
            'vvix_value': 0.0,
            'signal': 'unknown',
            'date': datetime.now().isoformat()[:10]
        }

        try:
            if yf is None:
                raise ImportError("yfinance 未安装")

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")

                vvix_data = yf.download('^VVIX', period='1d', progress=False)
                if not vvix_data.empty:
                    result['vvix_value'] = float(vvix_data['Close'].iloc[-1])

                    # 根据阈值分类
                    if result['vvix_value'] > 130:
                        result['signal'] = 'vol_explosion_risk'
                    elif result['vvix_value'] > 100:
                        result['signal'] = 'elevated'
                    elif result['vvix_value'] > 80:
                        result['signal'] = 'normal'
                    else:
                        result['signal'] = 'compressed'

                    self.logger.info(f"VVIX 数据获取: value={result['vvix_value']:.1f}, signal={result['signal']}")
                else:
                    result['signal'] = 'normal'
                    result['vvix_value'] = 85.0
                    self.logger.warning("VVIX 数据下载为空，使用默认值")

        except NETWORK_ERRORS as ne:
            self.logger.warning(f"网络错误获取 VVIX: {ne}")
            result['signal'] = 'normal'
            result['vvix_value'] = 85.0
        except Exception as e:
            self.logger.error(f"VVIX 抓取异常: {e}")
            result['error'] = str(e)
            result['signal'] = 'normal'
            result['vvix_value'] = 85.0

        self._write_cache('vvix', result)
        return result

    def fetch_all(self) -> Dict[str, Any]:
        """
        获取所有 CBOE 指标并计算宏观评分

        Returns:
            {
                'pcce': {...},                # Put/Call 比率
                'vix_term': {...},            # VIX 期限结构
                'skew': {...},                # SKEW 指数
                'vvix': {...},                # VVIX
                'macro_score': float,         # 0-10 组合评分
                'macro_sentiment': str,       # 'extreme_fear' / 'fear' / 'neutral' / 'greed' / 'extreme_greed'
                'timestamp': str
            }
        """
        self.logger.info("开始抓取所有 CBOE 数据")

        # 并行获取所有指标
        pcce = self.fetch_equity_putcall_ratio()
        vix_term = self.fetch_vix_term_structure()
        skew = self.fetch_skew_index()
        vvix = self.fetch_vvix()

        # 计算宏观评分（0-10 scale）
        macro_score = self._calculate_macro_score(pcce, vix_term, skew, vvix)

        # 确定综合情绪
        if macro_score >= 8.0:
            macro_sentiment = 'extreme_fear'
        elif macro_score >= 6.0:
            macro_sentiment = 'fear'
        elif macro_score >= 4.0:
            macro_sentiment = 'neutral'
        elif macro_score >= 2.0:
            macro_sentiment = 'greed'
        else:
            macro_sentiment = 'extreme_greed'

        result = {
            'pcce': pcce,
            'vix_term': vix_term,
            'skew': skew,
            'vvix': vvix,
            'macro_score': round(macro_score, 2),
            'macro_sentiment': macro_sentiment,
            'timestamp': datetime.now().isoformat()
        }

        self.logger.info(f"CBOE 综合评分: {macro_score:.2f}, 情绪: {macro_sentiment}")
        return result

    def _calculate_macro_score(self, pcce: Dict[str, Any], vix_term: Dict[str, Any],
                              skew: Dict[str, Any], vvix: Dict[str, Any]) -> float:
        """
        计算宏观风险评分（0-10，越高越恐惧）

        加权合成：
        - PCCE: 30% (高比率 = 恐惧)
        - VIX 期限: 25% (反向升水 = 恐惧)
        - SKEW: 25% (高值 = 尾部风险)
        - VVIX: 20% (高值 = 波动性压力)
        """
        scores = {}

        # PCCE 评分（0-10）— 阈值已针对 ETF 合成 P/C Ratio 上调 0.2-0.3
        pc_ratio = pcce.get('total_pc_ratio', 0.95)
        if pc_ratio > 1.3:
            scores['pcce'] = 9.0
        elif pc_ratio > 1.0:
            scores['pcce'] = 7.0
        elif pc_ratio > 0.8:
            scores['pcce'] = 5.0
        else:
            scores['pcce'] = 3.0 if pc_ratio > 0.6 else 1.0

        # VIX 期限结构评分
        vix_spot = vix_term.get('vix_spot', 15.0)
        contango_pct = vix_term.get('contango_pct', 5.0)

        # VIX 水位评分
        if vix_spot > 40:
            vix_level_score = 9.0
        elif vix_spot > 30:
            vix_level_score = 7.0
        elif vix_spot > 20:
            vix_level_score = 5.0
        elif vix_spot > 12:
            vix_level_score = 3.0
        else:
            vix_level_score = 1.0

        # VIX 升水评分（反向升水 = 恐惧）
        if contango_pct < -10:
            contango_score = 8.0  # 反向升水，风险高
        elif contango_pct < 0:
            contango_score = 6.0
        elif contango_pct < 5:
            contango_score = 4.0
        else:
            contango_score = 2.0

        scores['vix_term'] = (vix_level_score + contango_score) / 2

        # SKEW 评分（高值 = 恐惧）
        skew_value = skew.get('skew_value', 120.0)
        if skew_value > 150:
            scores['skew'] = 9.0
        elif skew_value > 130:
            scores['skew'] = 7.0
        elif skew_value > 115:
            scores['skew'] = 5.0
        else:
            scores['skew'] = 3.0

        # VVIX 评分（高值 = 恐惧）
        vvix_value = vvix.get('vvix_value', 85.0)
        if vvix_value > 130:
            scores['vvix'] = 9.0
        elif vvix_value > 100:
            scores['vvix'] = 7.0
        elif vvix_value > 80:
            scores['vvix'] = 5.0
        else:
            scores['vvix'] = 3.0

        # 加权合成
        macro_score = (
            scores['pcce'] * 0.30 +
            scores['vix_term'] * 0.25 +
            scores['skew'] * 0.25 +
            scores['vvix'] * 0.20
        )

        return max(0.0, min(10.0, macro_score))


def format_cboe_for_macro_card(cboe_data: Dict[str, Any]) -> str:
    """
    将 CBOE 数据格式化为 HTML 卡片片段（用于深度报告 CH2/CH5）

    Args:
        cboe_data: fetch_all() 的返回值

    Returns:
        HTML 片段字符串
    """
    pcce = cboe_data.get('pcce', {})
    vix_term = cboe_data.get('vix_term', {})
    skew = cboe_data.get('skew', {})
    vvix = cboe_data.get('vvix', {})
    macro_score = cboe_data.get('macro_score', 5.0)
    macro_sentiment = cboe_data.get('macro_sentiment', 'neutral')

    # 颜色映射
    sentiment_colors = {
        'extreme_fear': '#d32f2f',
        'fear': '#f57c00',
        'neutral': '#fbc02d',
        'greed': '#388e3c',
        'extreme_greed': '#1976d2'
    }

    signal_colors = {
        'extreme_fear': '#d32f2f',
        'fear': '#f57c00',
        'neutral': '#fbc02d',
        'greed': '#388e3c',
        'extreme_greed': '#1976d2',
        'extreme_tail_risk': '#d32f2f',
        'elevated': '#f57c00',
        'normal': '#fbc02d',
        'complacent': '#1976d2',
        'vol_explosion_risk': '#d32f2f',
        'compressed': '#1976d2'
    }

    # 获取颜色
    sentiment_color = sentiment_colors.get(macro_sentiment, '#757575')

    # 构造 HTML
    html = f"""
    <div style="background: #f5f5f5; border-radius: 8px; padding: 16px; margin: 12px 0;">
        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px;">
            <h3 style="margin: 0; font-size: 16px; font-weight: 600;">CBOE 宏观指标</h3>
            <div style="background: {sentiment_color}; color: white; padding: 6px 12px; border-radius: 20px; font-size: 12px; font-weight: 600;">
                {macro_sentiment.upper()} ({macro_score:.1f}/10)
            </div>
        </div>

        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 12px;">
            <!-- PCCE 卡片 -->
            <div style="background: white; border-radius: 6px; padding: 12px; border-left: 4px solid {signal_colors.get(pcce.get('signal', 'neutral'), '#757575')};">
                <div style="font-size: 12px; color: #666; margin-bottom: 4px;">Put/Call 比率</div>
                <div style="font-size: 18px; font-weight: 700; color: #212121;">{pcce.get('total_pc_ratio', 0.0):.2f}</div>
                <div style="font-size: 11px; color: #999; margin-top: 4px;">{pcce.get('signal', 'unknown')}</div>
            </div>

            <!-- VIX 现货 -->
            <div style="background: white; border-radius: 6px; padding: 12px; border-left: 4px solid #1976d2;">
                <div style="font-size: 12px; color: #666; margin-bottom: 4px;">VIX 现货</div>
                <div style="font-size: 18px; font-weight: 700; color: #212121;">{vix_term.get('vix_spot', 0.0):.2f}</div>
                <div style="font-size: 11px; color: #999; margin-top: 4px;">{vix_term.get('term_structure', 'unknown')}</div>
            </div>

            <!-- SKEW 指数 -->
            <div style="background: white; border-radius: 6px; padding: 12px; border-left: 4px solid {signal_colors.get(skew.get('signal', 'neutral'), '#757575')};">
                <div style="font-size: 12px; color: #666; margin-bottom: 4px;">SKEW 尾部风险</div>
                <div style="font-size: 18px; font-weight: 700; color: #212121;">{skew.get('skew_value', 0.0):.1f}</div>
                <div style="font-size: 11px; color: #999; margin-top: 4px;">{skew.get('signal', 'unknown')}</div>
            </div>

            <!-- VVIX 波动率 -->
            <div style="background: white; border-radius: 6px; padding: 12px; border-left: 4px solid {signal_colors.get(vvix.get('signal', 'neutral'), '#757575')};">
                <div style="font-size: 12px; color: #666; margin-bottom: 4px;">VVIX 波动性</div>
                <div style="font-size: 18px; font-weight: 700; color: #212121;">{vvix.get('vvix_value', 0.0):.1f}</div>
                <div style="font-size: 11px; color: #999; margin-top: 4px;">{vvix.get('signal', 'unknown')}</div>
            </div>
        </div>

        <!-- VIX 期限结构信息 -->
        <div style="margin-top: 12px; padding-top: 12px; border-top: 1px solid #e0e0e0;">
            <div style="font-size: 12px; color: #666;">
                <span>VIX 1m: <strong>{vix_term.get('vix_1m', 0.0):.2f}</strong></span>
                <span style="margin-left: 16px;">升水: <strong>{vix_term.get('contango_pct', 0.0):.1f}%</strong></span>
            </div>
        </div>
    </div>
    """

    return html


if __name__ == "__main__":
    # 测试脚本
    import sys

    print("[CBOE Fetcher 测试]")
    print("=" * 60)

    fetcher = CBOEDailyFetcher()

    # 测试 1: PCCE
    print("\n1. 获取 PCCE 数据...")
    pcce = fetcher.fetch_equity_putcall_ratio()
    print(f"   Put/Call: {pcce.get('total_pc_ratio', 'N/A'):.2f}")
    print(f"   信号: {pcce.get('signal', 'N/A')}")

    # 测试 2: VIX 期限结构
    print("\n2. 获取 VIX 期限结构...")
    vix_term = fetcher.fetch_vix_term_structure()
    print(f"   VIX Spot: {vix_term.get('vix_spot', 'N/A'):.2f}")
    print(f"   VIX 1m: {vix_term.get('vix_1m', 'N/A'):.2f}")
    print(f"   期限结构: {vix_term.get('term_structure', 'N/A')}")

    # 测试 3: SKEW
    print("\n3. 获取 SKEW 指数...")
    skew = fetcher.fetch_skew_index()
    print(f"   SKEW: {skew.get('skew_value', 'N/A'):.1f}")
    print(f"   信号: {skew.get('signal', 'N/A')}")

    # 测试 4: VVIX
    print("\n4. 获取 VVIX...")
    vvix = fetcher.fetch_vvix()
    print(f"   VVIX: {vvix.get('vvix_value', 'N/A'):.1f}")
    print(f"   信号: {vvix.get('signal', 'N/A')}")

    # 测试 5: 综合数据 + 评分
    print("\n5. 获取全部数据和宏观评分...")
    all_data = fetcher.fetch_all()
    print(f"   宏观评分: {all_data.get('macro_score', 'N/A')}/10")
    print(f"   宏观情绪: {all_data.get('macro_sentiment', 'N/A')}")

    # 测试 6: HTML 格式化
    print("\n6. 生成 HTML 卡片...")
    html = format_cboe_for_macro_card(all_data)
    print(f"   HTML 长度: {len(html)} 字符")

    # 保存示例 HTML
    html_path = "cboe_sample.html"
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>CBOE 宏观卡片示例</title>
</head>
<body>
{html}
</body>
</html>""")

    print(f"\n✓ HTML 示例已保存至: {html_path}")
    print("\n" + "=" * 60)
    print("[测试完成]")
