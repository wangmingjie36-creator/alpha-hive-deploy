"""
🐝 Alpha Hive - 财报自动监控器 (Earnings Watcher)

功能：
1. 从 yfinance 自动获取 watchlist 中每只股票的下次财报日期
2. 财报发布后自动抓取实际业绩数据（营收、EPS、指引等）
3. 更新当日简报中对应标的的数据
4. 可通过 scheduler 定时轮询，或手动触发

数据源优先级：
- yfinance earnings_dates / quarterly_financials（免费，无 API Key）
- Yahoo Finance 网页抓取（备用）

限速：yfinance 内部限流，本模块额外 2s 间隔
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hive_logger import PATHS, get_logger, atomic_json_write

_log = get_logger("earnings_watcher")

try:
    import yfinance as yf
except ImportError:
    yf = None

try:
    import requests as _requests
except ImportError:
    _requests = None

CACHE_DIR = PATHS.home / "earnings_cache"
CACHE_DIR.mkdir(exist_ok=True)

# yfinance 请求间隔（避免限速）
_yf_lock = threading.Lock()
_last_yf_request = 0.0
_YF_MIN_INTERVAL = 2.0


def _yf_throttle():
    """yfinance 请求限流"""
    global _last_yf_request
    with _yf_lock:
        now = time.time()
        elapsed = now - _last_yf_request
        if elapsed < _YF_MIN_INTERVAL:
            time.sleep(_YF_MIN_INTERVAL - elapsed)
        _last_yf_request = time.time()


class EarningsWatcher:
    """财报自动监控器"""

    def __init__(self):
        self._calendar_cache: Dict[str, Dict] = {}  # ticker -> {date, source, ts}

    # ==================== 财报日期获取 ====================

    def get_earnings_date(self, ticker: str) -> Optional[Dict]:
        """
        获取指定标的的下次财报日期

        返回: {
            ticker, earnings_date (str YYYY-MM-DD), earnings_time (str "BMO"/"AMC"/"TAS"),
            source, cached
        } 或 None
        """
        # 磁盘缓存 12 小时
        cache_path = CACHE_DIR / f"{ticker.upper()}_date.json"
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < 43200:  # 12h
                try:
                    with open(cache_path) as f:
                        data = json.load(f)
                        data["cached"] = True
                        return data
                except (json.JSONDecodeError, OSError) as exc:
                    _log.debug("earnings date cache read failed: %s", exc)

        if yf is None:
            _log.warning("yfinance 未安装，无法获取财报日期")
            return None

        try:
            _yf_throttle()
            stock = yf.Ticker(ticker)
            cal = stock.calendar

            if cal is None or (isinstance(cal, dict) and not cal):
                _log.info("%s: 无财报日期数据", ticker)
                return None

            # yfinance calendar 返回 dict: {'Earnings Date': [...], 'Revenue Avg': ...}
            earnings_dates = None
            if isinstance(cal, dict):
                earnings_dates = cal.get("Earnings Date")
                if earnings_dates and isinstance(earnings_dates, list):
                    # 取最近的一个
                    earnings_dates = earnings_dates[0]
            # 有时返回 DataFrame
            elif hasattr(cal, "iloc"):
                try:
                    earnings_dates = cal.iloc[0, 0]
                except (IndexError, KeyError):
                    pass

            if earnings_dates is None:
                return None

            # 转为字符串
            if hasattr(earnings_dates, "strftime"):
                date_str = earnings_dates.strftime("%Y-%m-%d")
            else:
                date_str = str(earnings_dates)[:10]

            result = {
                "ticker": ticker.upper(),
                "earnings_date": date_str,
                "earnings_time": "AMC",  # 默认盘后，yfinance 不总提供时间
                "source": "yfinance",
                "cached": False,
                "fetched_at": datetime.now().isoformat(),
            }

            try:
                atomic_json_write(cache_path, result)
            except (OSError, TypeError) as exc:
                _log.debug("earnings date cache write failed: %s", exc)

            return result

        except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
            _log.warning("获取 %s 财报日期失败: %s", ticker, e)
            return None

    def get_all_earnings_dates(self, tickers: List[str]) -> Dict[str, Dict]:
        """批量获取财报日期"""
        results = {}
        for ticker in tickers:
            data = self.get_earnings_date(ticker)
            if data:
                results[ticker] = data
        return results

    def get_today_earnings(self, tickers: List[str]) -> List[str]:
        """获取今日有财报的标的列表"""
        today = date.today().isoformat()
        # 也检查昨天的（盘后财报可能是昨天发布的）
        yesterday = (date.today() - timedelta(days=1)).isoformat()

        reporting_today = []
        dates = self.get_all_earnings_dates(tickers)
        for ticker, info in dates.items():
            ed = info.get("earnings_date", "")
            if ed in (today, yesterday):
                reporting_today.append(ticker)
        return reporting_today

    # ==================== 财报结果抓取 ====================

    def fetch_earnings_results(self, ticker: str) -> Optional[Dict]:
        """
        抓取已发布的财报实际结果

        返回: {
            ticker, quarter, fiscal_year,
            revenue_actual, revenue_estimate, revenue_beat (bool),
            eps_actual, eps_estimate, eps_beat (bool),
            guidance_revenue, guidance_commentary,
            yoy_revenue_growth, gross_margin,
            source, fetched_at
        }
        """
        # 缓存 30 分钟（财报后数据短期内不变）
        cache_path = CACHE_DIR / f"{ticker.upper()}_results.json"
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < 1800:
                try:
                    with open(cache_path) as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError) as exc:
                    _log.debug("earnings results cache read failed: %s", exc)

        if yf is None:
            _log.warning("yfinance 未安装")
            return None

        result = {}
        try:
            _yf_throttle()
            stock = yf.Ticker(ticker)

            # 1. 基础财务数据
            info = stock.fast_info if hasattr(stock, 'fast_info') else {}

            # 2. 季度财务报表
            _yf_throttle()
            quarterly_income = stock.quarterly_income_stmt
            if quarterly_income is not None and not quarterly_income.empty:
                latest_q = quarterly_income.iloc[:, 0]  # 最近一季
                prev_year_q = quarterly_income.iloc[:, 4] if quarterly_income.shape[1] > 4 else None

                # 营收
                revenue = None
                for label in ["Total Revenue", "Revenue", "Net Sales"]:
                    if label in latest_q.index:
                        revenue = float(latest_q[label])
                        break

                # 净利润
                net_income = None
                for label in ["Net Income", "Net Income Common Stockholders"]:
                    if label in latest_q.index:
                        net_income = float(latest_q[label])
                        break

                # 毛利
                gross_profit = None
                for label in ["Gross Profit"]:
                    if label in latest_q.index:
                        gross_profit = float(latest_q[label])
                        break

                # YoY 增长
                yoy_growth = None
                if revenue and prev_year_q is not None:
                    for label in ["Total Revenue", "Revenue", "Net Sales"]:
                        if label in prev_year_q.index:
                            prev_rev = float(prev_year_q[label])
                            if prev_rev > 0:
                                yoy_growth = (revenue - prev_rev) / prev_rev
                            break

                # 毛利率
                gross_margin = None
                if gross_profit and revenue and revenue > 0:
                    gross_margin = gross_profit / revenue

                # 季度标签
                q_date = quarterly_income.columns[0]
                if hasattr(q_date, "strftime"):
                    q_date_str = q_date.strftime("%Y-%m-%d")
                else:
                    q_date_str = str(q_date)

                # EPS（从 earnings history）
                eps_actual = None
                eps_estimate = None
                try:
                    _yf_throttle()
                    earnings_hist = stock.earnings_history
                    if earnings_hist is not None and not earnings_hist.empty:
                        latest_eh = earnings_hist.iloc[-1]
                        eps_actual = float(latest_eh.get("epsActual", 0)) if "epsActual" in latest_eh else None
                        eps_estimate = float(latest_eh.get("epsEstimate", 0)) if "epsEstimate" in latest_eh else None
                except (AttributeError, KeyError, TypeError, ValueError):
                    pass

                # 共识预期（从 analyst info）
                revenue_estimate = None
                try:
                    _yf_throttle()
                    analyst_info = stock.analyst_price_targets
                    # 备用：从 earnings_estimate 获取
                except (AttributeError, KeyError, TypeError, ValueError):
                    pass

                result = {
                    "ticker": ticker.upper(),
                    "quarter_end_date": q_date_str,
                    "revenue_actual": revenue,
                    "revenue_estimate": revenue_estimate,
                    "revenue_beat": None,  # 需要 estimate 才能判断
                    "eps_actual": eps_actual,
                    "eps_estimate": eps_estimate,
                    "eps_beat": (eps_actual > eps_estimate) if (eps_actual is not None and eps_estimate is not None) else None,
                    "net_income": net_income,
                    "gross_profit": gross_profit,
                    "gross_margin": round(gross_margin, 4) if gross_margin else None,
                    "yoy_revenue_growth": round(yoy_growth, 4) if yoy_growth else None,
                    "guidance_revenue": None,  # 指引需要从新闻/PR 获取
                    "guidance_commentary": None,
                    "source": "yfinance",
                    "fetched_at": datetime.now().isoformat(),
                    "data_completeness": "partial",  # yfinance 不提供指引
                }

                # 标记数据完整度
                filled = sum(1 for v in [revenue, eps_actual, net_income, gross_profit, yoy_growth]
                             if v is not None)
                if filled >= 4:
                    result["data_completeness"] = "good"
                elif filled >= 2:
                    result["data_completeness"] = "partial"
                else:
                    result["data_completeness"] = "minimal"

            else:
                _log.info("%s: 无季度财务数据", ticker)
                return None

        except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
            _log.warning("获取 %s 财报结果失败: %s", ticker, e)
            return None

        if result:
            try:
                atomic_json_write(cache_path, result)
            except (OSError, TypeError) as exc:
                _log.debug("earnings results cache write failed: %s", exc)

        return result if result else None

    # ==================== 简报更新逻辑 ====================

    def update_report_with_earnings(
        self,
        report_path: str,
        ticker: str,
        earnings_data: Dict,
    ) -> bool:
        """
        用实际财报数据更新已生成的 Markdown 简报

        Args:
            report_path: 简报文件路径
            ticker: 股票代码
            earnings_data: fetch_earnings_results() 的返回值

        Returns:
            True 如果成功更新
        """
        report_path = Path(report_path)
        if not report_path.exists():
            _log.warning("简报文件不存在: %s", report_path)
            return False

        try:
            content = report_path.read_text(encoding="utf-8")
        except OSError as e:
            _log.warning("读取简报失败: %s", e)
            return False

        ticker_upper = ticker.upper()
        updated = False

        # 构建更新摘要
        rev = earnings_data.get("revenue_actual")
        eps = earnings_data.get("eps_actual")
        eps_est = earnings_data.get("eps_estimate")
        yoy = earnings_data.get("yoy_revenue_growth")
        gm = earnings_data.get("gross_margin")
        q_date = earnings_data.get("quarter_end_date", "")

        # 格式化数字
        def fmt_rev(v):
            if v is None:
                return "N/A"
            if abs(v) >= 1e9:
                return f"${v / 1e9:.1f}B"
            if abs(v) >= 1e6:
                return f"${v / 1e6:.0f}M"
            return f"${v:,.0f}"

        def fmt_pct(v):
            if v is None:
                return "N/A"
            return f"{v * 100:.1f}%"

        # 1. 在简报头部添加财报更新标记
        if f"**{ticker_upper} 财报已更新**" not in content:
            # 在 "---" 后的第一个位置插入更新通知
            earnings_note = (
                f"\n> **{ticker_upper} 财报已更新**（自动抓取 {datetime.now().strftime('%H:%M')}）："
                f" 营收 {fmt_rev(rev)}"
            )
            if yoy is not None:
                earnings_note += f"（YoY {'+' if yoy > 0 else ''}{fmt_pct(yoy)}）"
            if eps is not None:
                earnings_note += f"，EPS ${eps:.2f}"
                if eps_est is not None:
                    beat = "超预期" if eps > eps_est else "低于预期"
                    earnings_note += f"（{beat} ${eps_est:.2f}）"
            if gm is not None:
                earnings_note += f"，毛利率 {fmt_pct(gm)}"
            earnings_note += "\n"

            # 插入到第一个 "---" 之后
            first_hr = content.find("\n---\n")
            if first_hr >= 0:
                insert_pos = first_hr + len("\n---\n")
                content = content[:insert_pos] + earnings_note + content[insert_pos:]
                updated = True

        # 2. 在对应 ticker 的表格中添加实际数据行
        # 查找 ticker 的 section (### TICKER | ...)
        ticker_section_pattern = rf"(### {ticker_upper}\s*\|[^\n]*\n)"
        match = re.search(ticker_section_pattern, content)
        if match:
            section_start = match.end()
            # 查找该 section 内的表格结尾（下一个 "###" 或 "---" 之前）
            next_section = re.search(r"\n(###|---)", content[section_start:])
            section_end = section_start + next_section.start() if next_section else len(content)
            section_content = content[section_start:section_end]

            # 如果有 "待财报验证" 等文字，替换为 "财报已验证"
            if "待财报验证" in section_content:
                section_content = section_content.replace("待财报验证", "财报已验证")
                content = content[:section_start] + section_content + content[section_end:]
                updated = True

            # 在表格中追加实际数据行（如果还没有）
            if "实际营收" not in section_content and rev is not None:
                # 找表格的最后一行 "|...|...|"
                table_lines = [l for l in section_content.split("\n") if l.strip().startswith("|")]
                if table_lines:
                    last_table_line = table_lines[-1]
                    insert_after = content.find(last_table_line, section_start) + len(last_table_line)

                    new_rows = ""
                    if rev is not None:
                        new_rows += f"\n| 实际营收 | **{fmt_rev(rev)}**"
                        if yoy is not None:
                            new_rows += f"（{'+' if yoy > 0 else ''}{fmt_pct(yoy)} YoY）"
                        new_rows += " |"
                    if eps is not None:
                        new_rows += f"\n| 实际 EPS | **${eps:.2f}**"
                        if eps_est is not None:
                            diff = eps - eps_est
                            new_rows += f"（{'超' if diff >= 0 else '低于'}预期 ${eps_est:.2f}）"
                        new_rows += " |"
                    if gm is not None:
                        new_rows += f"\n| 毛利率 | **{fmt_pct(gm)}** |"

                    if new_rows:
                        content = content[:insert_after] + new_rows + content[insert_after:]
                        updated = True

        if updated:
            try:
                report_path.write_text(content, encoding="utf-8")
                _log.info("简报已更新：%s 的财报数据（%s）", ticker_upper, report_path.name)
                return True
            except OSError as e:
                _log.warning("写入简报失败: %s", e)
                return False

        _log.info("简报未更新：%s（可能未找到匹配的 section 或数据已存在）", ticker_upper)
        return False

    # ==================== 自动监控主流程 ====================

    def check_and_update(
        self,
        tickers: List[str],
        report_path: Optional[str] = None,
    ) -> Dict:
        """
        自动检查今日是否有财报发布，若有则抓取结果并更新简报

        Args:
            tickers: 要检查的标的列表
            report_path: 简报路径（默认为今日简报）

        Returns: {
            checked: int, reporting_today: [str], updated: [str],
            earnings_data: {ticker: {...}}, errors: [str]
        }
        """
        if report_path is None:
            today_str = date.today().isoformat()
            report_path = str(PATHS.home / "reports" / f"alpha_hive_daily_{today_str}.md")

        _log.info("财报自动监控：检查 %d 个标的", len(tickers))

        # Step 1: 查找今日有财报的标的
        reporting_today = self.get_today_earnings(tickers)
        _log.info("今日财报标的：%s", reporting_today if reporting_today else "无")

        result = {
            "checked": len(tickers),
            "reporting_today": reporting_today,
            "updated": [],
            "earnings_data": {},
            "errors": [],
        }

        if not reporting_today:
            return result

        # Step 2: 逐个抓取财报结果
        for ticker in reporting_today:
            try:
                earnings = self.fetch_earnings_results(ticker)
                if earnings:
                    result["earnings_data"][ticker] = earnings
                    _log.info("%s 财报数据：营收 %s, EPS %s, 完整度 %s",
                              ticker,
                              earnings.get("revenue_actual"),
                              earnings.get("eps_actual"),
                              earnings.get("data_completeness"))

                    # Step 3: 更新简报
                    if Path(report_path).exists():
                        success = self.update_report_with_earnings(
                            report_path, ticker, earnings
                        )
                        if success:
                            result["updated"].append(ticker)
                else:
                    result["errors"].append(f"{ticker}: 无法获取财报数据")
            except (ValueError, KeyError, TypeError, OSError) as e:
                err_msg = f"{ticker}: {e}"
                result["errors"].append(err_msg)
                _log.warning("财报抓取失败: %s", err_msg)

        _log.info("财报监控完成：检查 %d | 今日财报 %d | 更新 %d | 错误 %d",
                  result["checked"], len(reporting_today),
                  len(result["updated"]), len(result["errors"]))

        return result


# ==================== 便捷函数 ====================

_watcher: Optional[EarningsWatcher] = None
_watcher_lock = threading.Lock()


def get_watcher() -> EarningsWatcher:
    """获取全局 EarningsWatcher 单例（线程安全）"""
    global _watcher
    if _watcher is None:
        with _watcher_lock:
            if _watcher is None:
                _watcher = EarningsWatcher()
    return _watcher


def auto_check_earnings(tickers: List[str] = None, report_path: str = None) -> Dict:
    """
    便捷函数：自动检查财报并更新简报

    用于 scheduler 调用：
        from earnings_watcher import auto_check_earnings
        auto_check_earnings()  # 使用 WATCHLIST 默认标的
    """
    if tickers is None:
        try:
            from config import WATCHLIST
            tickers = list(WATCHLIST.keys())
        except ImportError:
            _log.warning("无法导入 WATCHLIST")
            return {"error": "WATCHLIST not available"}

    watcher = get_watcher()
    return watcher.check_and_update(tickers, report_path)


if __name__ == "__main__":
    import sys

    # 命令行使用
    if len(sys.argv) > 1:
        if sys.argv[1] == "dates":
            # 列出所有标的的财报日期
            from config import WATCHLIST
            watcher = EarningsWatcher()
            dates = watcher.get_all_earnings_dates(list(WATCHLIST.keys()))
            for t, d in sorted(dates.items(), key=lambda x: x[1].get("earnings_date", "")):
                print(f"  {t:6s}  {d.get('earnings_date', 'N/A'):12s}  {d.get('source', '')}")

        elif sys.argv[1] == "today":
            # 检查今日财报
            from config import WATCHLIST
            result = auto_check_earnings()
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))

        elif sys.argv[1] == "fetch":
            # 抓取指定标的的财报结果
            if len(sys.argv) < 3:
                print("用法: python3 earnings_watcher.py fetch NVDA")
                sys.exit(1)
            ticker = sys.argv[2].upper()
            watcher = EarningsWatcher()
            result = watcher.fetch_earnings_results(ticker)
            if result:
                print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
            else:
                print(f"无法获取 {ticker} 的财报数据")

        else:
            print("用法:")
            print("  python3 earnings_watcher.py dates   # 列出所有财报日期")
            print("  python3 earnings_watcher.py today   # 检查今日财报并更新简报")
            print("  python3 earnings_watcher.py fetch NVDA  # 抓取指定标的财报")
    else:
        # 默认：自动检查
        result = auto_check_earnings()
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
