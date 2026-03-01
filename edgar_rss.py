"""
P2: SEC EDGAR Form 4 RSS 实时流 - 事件驱动告警

数据源：SEC EDGAR Atom RSS Feed（免费，无需注册）
- URL: https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&count=40&output=atom
- 缓存：15 分钟（RSS 更新频率约 10 分钟）
- 功能：过滤 watchlist 公司当日新鲜 Form 4 申报

用于 ScoutBeeNova：在 REST API 之前先检查今日实时告警，
发现当日新鲜内幕交易申报（比 REST API 反应更快）。
"""

import json
import logging as _logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from hive_logger import PATHS, atomic_json_write

_log = _logging.getLogger("alpha_hive.edgar_rss")

try:
    import requests as _req
except ImportError:
    _req = None

_FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=4&dateb=&owner=include&count=40&search_text=&output=atom"
)
_SEC_HEADERS = {
    "User-Agent": "AlphaHive research@alphahive.dev",
    "Accept": "application/atom+xml,application/xml",
}

_CACHE_PATH = Path(PATHS.home) / "sec_cache" / "edgar_rss.json"
try:
    from config import CACHE_CONFIG as _CC
    _CACHE_TTL = _CC["ttl"].get("edgar_rss", 900)
except (ImportError, KeyError):
    _CACHE_TTL = 900
_lock = threading.Lock()

# ── RSS 健康追踪（#7）──
_RSS_FAIL_THRESHOLD = 3
_rss_fail_count = 0
_rss_degraded = False


def _try_rss_slack_alert(fail_count: int):
    """尝试通过 Slack 发送 EDGAR RSS 降级告警（静默失败）"""
    try:
        from slack_report_notifier import SlackReportNotifier
        n = SlackReportNotifier()
        if getattr(n, "enabled", False):
            n.send_risk_alert(
                alert_title="EDGAR RSS 降级",
                alert_message=f"SEC EDGAR Form4 RSS 已连续失败 {fail_count} 次，实时内幕交易告警不可用。",
                severity="MEDIUM",
            )
    except (ImportError, OSError, RuntimeError, ValueError) as _se:
        _log.debug("Slack RSS 降级告警发送失败: %s", _se)


class EdgarRSSClient:
    """SEC EDGAR Form 4 RSS 实时告警客户端"""

    def __init__(self):
        self._cache: List[Dict] = []
        self._cache_ts: float = 0.0

    # ==================== 数据获取 ====================

    def get_recent_form4_alerts(self, force_refresh: bool = False) -> List[Dict]:
        """
        获取最近 40 条 Form 4 申报（Atom RSS 实时流）

        返回: [{company_name, cik, filing_date, updated_ts, feed_url, accession_number}, ...]
        """
        with _lock:
            now = time.time()
            # 内存缓存
            if not force_refresh and (now - self._cache_ts) < _CACHE_TTL:
                return self._cache

            # 磁盘缓存
            if not force_refresh and _CACHE_PATH.exists():
                age = now - _CACHE_PATH.stat().st_mtime
                if age < _CACHE_TTL:
                    try:
                        with open(_CACHE_PATH) as f:
                            self._cache = json.load(f)
                            self._cache_ts = now
                            return self._cache
                    except (json.JSONDecodeError, OSError):
                        pass

            if _req is None:
                return self._cache

            # 健康追踪（一次声明，两个分支共用）
            global _rss_fail_count, _rss_degraded
            try:
                resp = _req.get(_FEED_URL, headers=_SEC_HEADERS, timeout=15)
                if not resp.ok:
                    _log.debug("EDGAR RSS HTTP %s", resp.status_code)
                    return self._cache

                entries = self._parse_atom(resp.text)
                self._cache = entries
                self._cache_ts = now

                # 写入磁盘缓存
                try:
                    _CACHE_PATH.parent.mkdir(exist_ok=True)
                    atomic_json_write(_CACHE_PATH, entries)
                except (OSError, TypeError):
                    pass

                _log.debug("EDGAR RSS: %d entries", len(entries))
                # 成功：重置计数器
                if _rss_fail_count >= _RSS_FAIL_THRESHOLD:
                    _log.info("✅ EDGAR RSS 已恢复（之前连续失败 %d 次）", _rss_fail_count)
                _rss_fail_count = 0
                _rss_degraded = False
                return entries

            except (ConnectionError, TimeoutError, OSError, ValueError) as e:
                _log.debug("EDGAR RSS fetch error: %s", e)
                _rss_fail_count += 1
                if _rss_fail_count == _RSS_FAIL_THRESHOLD and not _rss_degraded:
                    _rss_degraded = True
                    _log.warning("⚠️ EDGAR RSS 连续失败 %d 次，进入降级模式", _rss_fail_count)
                    _try_rss_slack_alert(_rss_fail_count)
                elif _rss_fail_count > _RSS_FAIL_THRESHOLD and _rss_fail_count % 5 == 0:
                    _log.warning("⚠️ EDGAR RSS 持续降级，累计失败 %d 次", _rss_fail_count)
                return self._cache

    # ==================== Atom 解析 ====================

    def _parse_atom(self, xml_text: str) -> List[Dict]:
        """解析 Atom XML，提取 Form 4 申报信息"""
        entries = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            _log.debug("EDGAR Atom parse error: %s", e)
            return []

        NS = "http://www.w3.org/2005/Atom"
        for entry in root.findall(f"{{{NS}}}entry"):
            try:
                title_el = entry.find(f"{{{NS}}}title")
                updated_el = entry.find(f"{{{NS}}}updated")
                link_el = entry.find(f"{{{NS}}}link")
                id_el = entry.find(f"{{{NS}}}id")

                title = title_el.text if title_el is not None else ""
                updated_str = updated_el.text if updated_el is not None else ""
                feed_url = link_el.get("href", "") if link_el is not None else ""
                entry_id = id_el.text if id_el is not None else ""

                # 解析 title: "4 - COMPANY NAME (CIK_ISSUER) (CIK_REPORTER)"
                company_name = ""
                cik = ""
                if title and " - " in title:
                    rest = title.split(" - ", 1)[1]
                    ciks = re.findall(r'\((\d+)\)', rest)
                    if ciks:
                        cik = ciks[0]   # 第一个括号是发行人 CIK
                    company_name = re.sub(r'\s*\(\d+\).*$', '', rest).strip()

                # 从 entry id 提取 accession number
                accession = ""
                if entry_id and "accession-number=" in entry_id:
                    accession = entry_id.split("accession-number=")[-1].strip()

                # 日期取 updated 的日期部分（YYYY-MM-DD）
                filing_date = updated_str[:10] if updated_str else ""

                entries.append({
                    "company_name": company_name,
                    "cik": cik,
                    "title": title,
                    "filing_date": filing_date,
                    "updated_ts": updated_str,
                    "feed_url": feed_url,
                    "accession_number": accession,
                })

            except (AttributeError, IndexError, ValueError) as e:
                _log.debug("RSS entry parse error: %s", e)

        return entries

    # ==================== 过滤查询 ====================

    def get_today_filings_for_cik(self, cik: str) -> List[Dict]:
        """获取今日指定 CIK 的新鲜 Form 4 申报"""
        today = datetime.now().strftime("%Y-%m-%d")
        entries = self.get_recent_form4_alerts()
        return [e for e in entries if e["filing_date"] == today and e["cik"] == str(cik)]

    def get_today_filings_by_name(self, ticker: str) -> List[Dict]:
        """通过公司名模糊匹配（无 CIK 时的备选）"""
        today = datetime.now().strftime("%Y-%m-%d")
        entries = self.get_recent_form4_alerts()
        ticker_upper = ticker.upper()
        return [
            e for e in entries
            if e["filing_date"] == today and ticker_upper in e["company_name"].upper()
        ]

    def summarize_ticker_alerts(self, ticker: str, cik: Optional[str] = None) -> Dict:
        """
        综合获取指定 ticker 今日的 RSS 告警摘要

        返回: {ticker, fresh_filings_count, has_fresh_filings, filings, summary}
        """
        filings = []
        if cik:
            filings = self.get_today_filings_for_cik(str(cik))
        # CIK 无结果时降级到名称匹配
        if not filings:
            filings = self.get_today_filings_by_name(ticker)

        has_fresh = len(filings) > 0
        summary = (
            f"今日 {len(filings)} 份新鲜 Form 4 申报（RSS 实时）"
            if has_fresh else "今日暂无实时 Form 4 申报"
        )

        return {
            "ticker": ticker,
            "fresh_filings_count": len(filings),
            "has_fresh_filings": has_fresh,
            "filings": filings[:5],
            "summary": summary,
        }


# ==================== 单例 + 便捷函数 ====================

_client: Optional[EdgarRSSClient] = None
_client_lock = threading.Lock()


def get_rss_client() -> EdgarRSSClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = EdgarRSSClient()
    return _client


def get_today_form4_alerts(ticker: str, cik: Optional[str] = None) -> Dict:
    """便捷函数：获取今日 Form 4 RSS 告警"""
    return get_rss_client().summarize_ticker_alerts(ticker, cik)
