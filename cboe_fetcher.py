"""
CBOE 日度统计数据抓取器 — Alpha Hive 宏观分析层增强

功能：
1. 获取股票 Put/Call 比率（合成值，替代 2026-04 下架的 ^PCCE）
2. 获取 VIX 期限结构（远期溢价 vs 现货）
3. 获取 SKEW 指数（尾部风险偏度）
4. 获取 VVIX（波动率之波动率）
5. 提供宏观评分组合

数据源（v0.45.241 起）：**CBOE CDN 优先，yfinance 备选**，都拿不到才落 `source='default_fallback'`。
本地缓存 30 分钟（盘中）或 4 小时（盘后）。
  - SKEW / VVIX：`cdn.cboe.com/api/global/us_indices/daily_prices/{SKEW,VVIX}_History.csv` 最新一行
  - P/C：`cdn.cboe.com/api/global/delayed_quotes/options/{SPY,QQQ,IWM}.json` 近 3 个到期日成交量合成
    （经 `cboe_options._fetch_cboe_payload`：串行化、重试、进程缓存、陈旧 CDN 文件拒收，全部复用）
  - VIX 期限结构：vixcentral VX 期货（v0.45.29，未变）
为什么换：此前三项**只走 yfinance**，云端快照沙箱够不到 yfinance ⇒ 08-26 ~ 09-11 共 12 份
market.json 里 pcce / skew / vvix **12/12 天全是兜底常量**（09-11 兜底 SKEW 120 / VVIX 85，
CBOE 当日真值 154.49 / 91.28）。云端够得到 cdn.cboe.com（同日期权快照 27/30 走的就是它）。

2026-04-22 变更：因 Yahoo Finance 下架 ^PCCE / ^CPCE / ^CPC 等 CBOE 官方 P/C 比率符号，
fetch_equity_putcall_ratio() 改为从 SPY/QQQ/IWM 期权链 volume 合成。
"""

import csv
import io
import os
import json
import time
import urllib.request
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from zoneinfo import ZoneInfo
import warnings

try:
    import yfinance as yf
except ImportError:
    yf = None


def _last_close(df) -> float:
    """安全提取 yf.download() 结果的最后一根 Close 标量。

    v0.43.12: 新版 yfinance 对单只标的 download() 也返回 MultiIndex 列名
    （如 ('Close','^VIX')），df['Close'] 是 (N,1) DataFrame，.iloc[-1] 得到
    Series，float() 直接 TypeError——VIX/SKEW/VVIX 因此长期落默认值
    （15.0/120.0/85.0），VIXY 更被裸 except 吞掉导致期限结构恒判 contango。
    """
    close = df["Close"]
    if hasattr(close, "columns"):  # MultiIndex → (N,1) DataFrame
        close = close.iloc[:, 0]
    return float(close.iloc[-1])


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


# ── CBOE 指数日线 CSV（v0.45.241）────────────────────────────────────────────
_CBOE_INDEX_CSV_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{}_History.csv"
_CBOE_NET_TIMEOUT = 15
_CBOE_RETRIES = 3
# 解析合理区间（防格式漂移把别的列读成指数值），不是行情判断。
# 实测史上 SKEW ≈ 100–170、VVIX ≈ 60–210，区间放宽一倍余量。
_INDEX_VALID_RANGE = {"SKEW": (80.0, 250.0), "VVIX": (40.0, 300.0)}
# 最新一行比 ET 今天早**超过**这么多日历日 ⇒ 当作 CDN 文件停更，弃用本源。
# CSV 当天何时更新**未实测**（09-14 看 Last-Modified 是周日重生成、内容仍是周五），
# 所以不要求「必须是今天」—— 那会让收盘后不久跑的云端快照天天落回兜底。
# 放行的那一行把**观测日**如实写进 `date`，由读的人判新鲜度。
_INDEX_MAX_STALE_DAYS = 7
_ET = ZoneInfo("America/New_York")


def _today_et():
    """ET 今天（date）。单独成函数：测试钉死它，停更天数的边界才不随跑的时刻漂。"""
    return datetime.now(_ET).date()


def _download_cboe_index_csv(symbol: str) -> Optional[str]:
    """拉取 CBOE 指数日线 CSV 原文；失败返回 None（不抛，调用方据此降级）。"""
    try:
        from cboe_options import _CBOE_SEM  # 与全部 CBOE 请求共用一把锁，同 cboe_vix
    except Exception:  # pragma: no cover
        import threading
        _CBOE_SEM = threading.Semaphore(1)
    url = _CBOE_INDEX_CSV_URL.format(symbol)
    last_err = None
    for attempt in range(_CBOE_RETRIES):
        try:
            with _CBOE_SEM:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                raw = urllib.request.urlopen(req, timeout=_CBOE_NET_TIMEOUT).read()
            return raw.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001 - 网络层什么都可能抛
            last_err = e
            if attempt < _CBOE_RETRIES - 1:
                time.sleep(0.7 * (attempt + 1))
    logger.warning(f"CBOE {symbol} CSV 下载失败（重试 {_CBOE_RETRIES} 次耗尽）: {last_err}")
    return None


def _latest_cboe_index_value(symbol: str) -> Optional[Tuple[float, str]]:
    """CBOE 指数 CSV 最新一行 → `(值, ISO 观测日)`。

    拿不到 / 格式变了 / 最新一行停更超过 `_INDEX_MAX_STALE_DAYS` ⇒ None，**不返回猜测值**。
    CSV 形如 `DATE,SKEW` + `MM/DD/YYYY,147.02`；值列名就是指数符号。坏行跳过。
    """
    text = _download_cboe_index_csv(symbol)
    if not text:
        return None
    lo, hi = _INDEX_VALID_RANGE[symbol]
    rows: List[Tuple[str, float]] = []
    try:
        reader = csv.DictReader(io.StringIO(text))
        if symbol not in (reader.fieldnames or []):
            logger.warning(f"CBOE {symbol} CSV 表头变了（{reader.fieldnames}），弃用本源")
            return None
        for row in reader:
            d, v = (row.get("DATE") or "").strip(), (row.get(symbol) or "").strip()
            try:
                iso = datetime.strptime(d, "%m/%d/%Y").strftime("%Y-%m-%d")
                val = float(v)
            except (ValueError, TypeError):
                continue
            if lo <= val <= hi:
                rows.append((iso, val))
    except csv.Error as e:
        logger.warning(f"CBOE {symbol} CSV 解析失败: {e}")
        return None
    if not rows:
        logger.warning(f"CBOE {symbol} CSV 没有任何可用行，弃用本源")
        return None
    iso, val = max(rows)
    lag = (_today_et() - datetime.strptime(iso, "%Y-%m-%d").date()).days
    if lag > _INDEX_MAX_STALE_DAYS:
        logger.warning(f"CBOE {symbol} CSV 最新一行是 {iso}（{lag} 天前），疑似停更，弃用本源")
        return None
    return round(val, 2), iso


def _classify_skew(v: float) -> str:
    if v > 150:
        return 'extreme_tail_risk'
    if v > 130:
        return 'elevated'
    if v > 115:
        return 'normal'
    return 'complacent'


def _classify_vvix(v: float) -> str:
    if v > 130:
        return 'vol_explosion_risk'
    if v > 100:
        return 'elevated'
    if v > 80:
        return 'normal'
    return 'compressed'


class CBOEDailyFetcher:
    """
    CBOE 日度统计数据抓取器

    提供无需 API Key 的免费 CBOE 数据获取：
    - 股票 Put/Call 比率
    - VIX 期限结构
    - SKEW 指数
    - VVIX（波动率的波动率）
    """

    def __init__(self, cache_dir: Optional[str] = None):
        """
        初始化抓取器

        Args:
            cache_dir: 缓存目录路径；None ⇒ 调用时解析 `PATHS.cboe_daily_cache`（v0.45.230）。
                原默认值是 cwd 相对的 "cache/cboe_daily"，不读 `ALPHA_HIVE_CACHE_DIR`、
                在哪跑就建进哪（CLAUDE.md「新产物的默认路径不许是相对路径」）。
        """
        if cache_dir is None:
            from hive_logger import PATHS
            cache_dir = str(PATHS.cboe_daily_cache)
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

        口径：SPY / QQQ / IWM 各取**最近 3 个到期日**的全部 put / call 成交量，汇总后 put_vol / call_vol。
        v0.45.241 起数据源 = CBOE 延迟报价期权链（`source='synthetic_cboe_options'`），
        CBOE 一只都拿不到才退回 yfinance 同口径（`'synthetic_yf_options'`），再不行落兜底。
        不在两个源之间逐标的拼接：两家的「当前时刻成交量」不是同一个快照，拼出来的比值两边都不代表。

        注意：ETF 合成 P/C Ratio 的水位通常比 CBOE 官方 PCCE 高 0.2-0.3（因 SPY 等
        ETF 承担大量机构对冲盘），阈值已相应上调。

        Returns:
            {
                'total_pc_ratio': float,      # 合成 P/C 比率（put_vol / call_vol）
                'call_volume': int,           # 汇总 call 成交量
                'put_volume': int,            # 汇总 put 成交量
                'date': str,                  # ISO 日期：CBOE = 期权链成交日（ET）；其余 = 抓取日
                'signal': str,                # 情绪信号
                'source': str,                # 'synthetic_cboe_options' | 'synthetic_yf_options' | 'default_fallback'
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
            'source': 'synthetic_cboe_options',
            'tickers_used': [],
        }

        got = self._synthetic_pc_from_cboe()
        if got is None:
            try:
                got = self._synthetic_pc_from_yfinance()
                if got is not None:
                    result['source'] = 'synthetic_yf_options'
                    self.logger.warning("CBOE 期权链合成 P/C 不可用，退回 yfinance")
            except NETWORK_ERRORS as ne:
                self.logger.warning(f"合成 P/C Ratio 网络错误: {ne}")
            except Exception as e:
                self.logger.error(f"合成 P/C Ratio 异常: {e}")
                result['error'] = str(e)

        if got is not None:
            total_call_vol, total_put_vol, tickers_used, obs_date = got
            pc_ratio = total_put_vol / total_call_vol
            result['total_pc_ratio'] = round(pc_ratio, 3)
            result['call_volume'] = int(total_call_vol)
            result['put_volume'] = int(total_put_vol)
            result['tickers_used'] = tickers_used
            if obs_date:
                result['date'] = obs_date

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
                f"来源={result['source']} {tickers_used}, 信号={result['signal']})"
            )
        else:
            # 两个源的所有 ETF 都失败，降级中性默认值
            self.logger.warning("合成 P/C Ratio：CBOE 与 yfinance 均失败，使用历史中位数")
            result['total_pc_ratio'] = 0.95  # ETF 合成历史中位数（比 PCCE 的 0.75 高）
            result['call_volume'] = 0
            result['put_volume'] = 0
            result['signal'] = 'neutral'
            result['source'] = 'default_fallback'

        # 写入缓存
        self._write_cache('pcce', result)
        return result

    def _synthetic_pc_from_cboe(self) -> Optional[Tuple[float, float, List[str], Optional[str]]]:
        """CBOE 延迟报价期权链合成 → `(call_vol, put_vol, tickers_used, 成交日)`；拿不到返回 None。

        取数经 `cboe_options._fetch_cboe_payload`：陈旧 CDN 文件（成交日早于此刻应有日期）在那里就
        返回 None，这里不再判。到期日早于成交日的合约（已到期仍挂在文件里的）不计。
        """
        try:
            from cboe_options import _fetch_cboe_payload, _parse_occ, _payload_vintage_date
        except ImportError as e:  # pragma: no cover
            self.logger.warning(f"cboe_options 不可用，跳过 CBOE 合成 P/C: {e}")
            return None

        total_call_vol = 0.0
        total_put_vol = 0.0
        tickers_used: List[str] = []
        dates: List[str] = []
        for symbol in self._SYNTHETIC_PC_TICKERS:
            try:
                data = _fetch_cboe_payload(symbol, _CBOE_NET_TIMEOUT)
            except Exception as e:  # noqa: BLE001 - 契约是返回 None，防御性兜一层
                self.logger.debug(f"CBOE {symbol} 期权链失败: {e}")
                continue
            options = (data or {}).get("options") or []
            if not options:
                continue
            vintage = _payload_vintage_date(data)
            by_expiry: Dict[str, List[float]] = {}
            for o in options:
                parsed = _parse_occ(o.get("option", ""))
                if not parsed:
                    continue
                expiry, cp, _strike = parsed
                if vintage and expiry < vintage:
                    continue
                # 先登记到期日再看成交量：整个到期日零成交也占「最近 3 个」的一个名额，
                # 否则第 4 个到期日会被悄悄顶进来，口径随成交稀疏度漂移。
                pair = by_expiry.setdefault(expiry, [0.0, 0.0])
                try:
                    vol = float(o.get("volume") or 0)
                except (TypeError, ValueError):
                    continue
                if vol > 0:              # 也挡 NaN
                    pair[0 if cp == "C" else 1] += vol
            nearest = sorted(by_expiry)[: self._SYNTHETIC_PC_EXPIRIES]
            sym_call_vol = sum(by_expiry[e][0] for e in nearest)
            sym_put_vol = sum(by_expiry[e][1] for e in nearest)
            if sym_call_vol > 0 or sym_put_vol > 0:
                total_call_vol += sym_call_vol
                total_put_vol += sym_put_vol
                tickers_used.append(symbol)
                if vintage:
                    dates.append(vintage)
                self.logger.debug(
                    f"{symbol}(CBOE {nearest}): calls={sym_call_vol:.0f}, puts={sym_put_vol:.0f}, "
                    f"pc={sym_put_vol / max(sym_call_vol, 1):.3f}"
                )

        if not tickers_used or total_call_vol <= 0:
            return None
        return total_call_vol, total_put_vol, tickers_used, (min(dates) if dates else None)

    def _synthetic_pc_from_yfinance(self) -> Optional[Tuple[float, float, List[str], Optional[str]]]:
        """yfinance 期权链合成（与 CBOE 同口径）→ 同形元组；全部失败返回 None。yfinance 缺失则抛 ImportError。"""
        if yf is None:
            raise ImportError("yfinance 未安装")

        total_call_vol = 0.0
        total_put_vol = 0.0
        tickers_used: List[str] = []

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

        if not tickers_used or total_call_vol <= 0:
            return None
        return total_call_vol, total_put_vol, tickers_used, None

    def fetch_vix_term_structure(self) -> Dict[str, Any]:
        """
        获取 VIX 期限结构（v0.45.29 起主源 = 真实 VX 期货曲线）

        Returns:
            {
                'vix_spot': float,            # 现货 VIX
                'vix_1m': float,              # 1 月期 VIX 期货（VX M1 真值）
                'vix_3m': float,              # 3 月期 VIX 期货（VX M3 真值）
                'term_structure': str,        # 'contango' / 'backwardation' / 'flat'
                'contango_pct': float,        # 现货 vs 1 月差额 %
                'source': str,                # 'vx_futures' | 'default_fallback'
                'error': str (optional)
            }

        ⚠️ source='default_fallback' 时全部数值是兜底常量，**不可当观测值用**。

        历史缺陷（v0.45.29 修正）：此前 vix_1m = VIXY **ETF 股价** × 0.5、
        vix_3m = spot × 1.10——ETF 价格与 VIX 点位无可比性（实测算出
        backwardation −42.6% 的垃圾口径），且兜底常量不带任何标注。
        现复用 vix_term_structure.py 的 vixcentral VX 期货曲线（M1~M8 真值）；
        拿不到期货时**不再合成**，直接落 default_fallback 并标注。
        """
        cached = self._read_cache('vix_term')
        if cached and cached.get('source'):
            self.logger.debug("使用缓存 VIX 期限结构")
            return cached
        # 无 source 键 = v0.45.29 之前的旧缓存（可能是 VIXY 垃圾口径），忽略重抓

        result = {
            'vix_spot': 0.0,
            'vix_1m': 0.0,
            'vix_3m': 0.0,
            'term_structure': 'unknown',
            'contango_pct': 0.0,
            'source': 'default_fallback',
        }

        try:
            from vix_term_structure import get_vix_term_structure as _get_vts
            vts = _get_vts() or {}
            spot = vts.get('spot_vix')
            futures = vts.get('futures') or []

            if spot is None:
                # spot 的 CBOE 官网兜底（v0.43.24 起的主源，云端 yfinance 不通时仍可达）
                try:
                    from cboe_vix import get_vix_spot
                    _sp = get_vix_spot()
                    if _sp:
                        spot = _sp[0]  # (value, source_str)
                except Exception as e:  # noqa: BLE001
                    self.logger.debug(f"cboe_vix spot 兜底失败: {e}")

            if spot and len(futures) >= 3:
                result['vix_spot'] = round(float(spot), 2)
                result['vix_1m'] = round(float(futures[0]), 2)
                result['vix_3m'] = round(float(futures[2]), 2)
                result['term_structure'] = vts.get('structure') or 'unknown'
                result['contango_pct'] = round(
                    (result['vix_1m'] - result['vix_spot']) / result['vix_spot'] * 100, 2)
                result['source'] = 'vx_futures'
                self.logger.info(
                    f"VIX 期限结构(VX期货): spot={result['vix_spot']:.2f}, "
                    f"M1={result['vix_1m']:.2f}, M3={result['vix_3m']:.2f}, "
                    f"term={result['term_structure']}, contango={result['contango_pct']:.2f}%")
            else:
                raise RuntimeError(
                    f"VX 期货数据不足（spot={spot}, futures={len(futures)} 点）")

        except NETWORK_ERRORS as ne:
            self.logger.warning(f"网络错误获取 VIX: {ne}")
            result.update(vix_spot=15.0, vix_1m=15.75, vix_3m=16.5,
                          term_structure='contango', contango_pct=5.0,
                          source='default_fallback')
        except Exception as e:
            self.logger.error(f"VIX 期限结构抓取异常: {e}")
            result['error'] = str(e)
            result.update(vix_spot=15.0, vix_1m=15.75, vix_3m=16.5,
                          term_structure='contango', contango_pct=5.0,
                          source='default_fallback')

        self._write_cache('vix_term', result)
        return result

    def fetch_skew_index(self) -> Dict[str, Any]:
        """
        获取 CBOE SKEW 指数（尾部风险）

        Returns:
            {
                'skew_value': float,          # SKEW 指数值
                'signal': str,                # 'extreme_tail_risk' / 'elevated' / 'normal' / 'complacent'
                'date': str,                  # ISO 日期：cboe_cdn = 观测日；其余 = 抓取日
                'source': str,                # 'cboe_cdn' | 'yfinance' | 'default_fallback'
                'error': str (optional)
            }
        """
        return self._fetch_index_level('skew', 'SKEW', 'skew_value', 120.0, _classify_skew)

    def fetch_vvix(self) -> Dict[str, Any]:
        """
        获取 VVIX（波动率的波动率）

        Returns:
            {
                'vvix_value': float,          # VVIX 指数值
                'signal': str,                # 'vol_explosion_risk' / 'elevated' / 'normal' / 'compressed'
                'date': str,                  # ISO 日期：cboe_cdn = 观测日；其余 = 抓取日
                'source': str,                # 'cboe_cdn' | 'yfinance' | 'default_fallback'
                'error': str (optional)
            }
        """
        return self._fetch_index_level('vvix', 'VVIX', 'vvix_value', 85.0, _classify_vvix)

    def _fetch_index_level(self, key: str, symbol: str, value_key: str,
                           fallback_value: float, classify) -> Dict[str, Any]:
        """SKEW / VVIX 共用：CBOE CSV → yfinance `^SYMBOL` → 兜底常量（v0.45.241）。

        兜底一律标 `source='default_fallback'`（v0.45.29 契约，`cloud_snapshot_fetch._degradation_check`
        据此剔除）。CBOE 拿不到而 yfinance 拿到时打 warning —— 云端 yfinance 本就不通，
        那条路走得通说明是在本机、且 CBOE 出了事，要看得见。
        """
        cached = self._read_cache(key)
        if cached and cached.get('source'):
            self.logger.debug(f"使用缓存 {symbol} 数据")
            return cached
        # 无 source 键 = v0.45.29 之前的旧缓存，忽略重抓

        result = {
            value_key: 0.0,
            'signal': 'unknown',
            'date': datetime.now().isoformat()[:10],
            'source': 'default_fallback',
        }

        got = _latest_cboe_index_value(symbol)
        if got is not None:
            value, obs_date = got
            result.update({value_key: value, 'date': obs_date, 'source': 'cboe_cdn',
                           'signal': classify(value)})
            self.logger.info(f"{symbol} 数据获取(CBOE): value={value:.2f}, date={obs_date}, "
                             f"signal={result['signal']}")
            self._write_cache(key, result)
            return result

        try:
            if yf is None:
                raise ImportError("yfinance 未安装")

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")

                data = yf.download(f'^{symbol}', period='1d', progress=False)
                if not data.empty:
                    result[value_key] = _last_close(data)
                    result['source'] = 'yfinance'
                    result['signal'] = classify(result[value_key])
                    self.logger.warning(f"CBOE {symbol} 不可用，退回 yfinance: "
                                        f"value={result[value_key]:.1f}, signal={result['signal']}")
                else:
                    result['signal'] = 'normal'
                    result[value_key] = fallback_value
                    result['source'] = 'default_fallback'
                    self.logger.warning(f"{symbol} CBOE 与 yfinance 均无数据，使用默认值")

        except NETWORK_ERRORS as ne:
            self.logger.warning(f"网络错误获取 {symbol}: {ne}")
            result['signal'] = 'normal'
            result[value_key] = fallback_value
            result['source'] = 'default_fallback'
        except Exception as e:
            self.logger.error(f"{symbol} 抓取异常: {e}")
            result['error'] = str(e)
            result['signal'] = 'normal'
            result[value_key] = fallback_value
            result['source'] = 'default_fallback'

        self._write_cache(key, result)
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
