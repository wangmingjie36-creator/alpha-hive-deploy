"""
SEC EDGAR 内幕交易（Form 4）数据采集模块

使用免费 SEC EDGAR API：
1. company_tickers.json → ticker 转 CIK
2. data.sec.gov/submissions → 最近 Form 4 列表
3. XML 解析 → 交易明细（买入/卖出/数量/价格/内幕人信息）

限制：10 req/s，必须设置 User-Agent
"""

import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    requests = None

from hive_logger import PATHS, get_logger
from resilience import sec_limiter, sec_breaker

_log = get_logger("sec_edgar")

# 缓存目录
CACHE_DIR = PATHS.home / "sec_cache"
CACHE_DIR.mkdir(exist_ok=True)

# SEC 要求的 User-Agent
SEC_USER_AGENT = "AlphaHive research@alphahive.dev"
SEC_HEADERS = {"User-Agent": SEC_USER_AGENT, "Accept": "application/json"}


class SECEdgarClient:
    """SEC EDGAR Form 4 内幕交易数据客户端"""

    def __init__(self):
        self._cik_map: Dict[str, int] = {}
        self._load_cik_map()

    # ==================== CIK 映射 ====================

    def _load_cik_map(self):
        """加载 ticker → CIK 映射（优先从本地缓存）"""
        cache_path = CACHE_DIR / "company_tickers.json"

        # 缓存有效期 24 小时
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < 86400:
                try:
                    with open(cache_path) as f:
                        data = json.load(f)
                    self._cik_map = {
                        v["ticker"].upper(): v["cik_str"]
                        for v in data.values()
                    }
                    return
                except (json.JSONDecodeError, OSError, KeyError) as e:
                    _log.debug("CIK cache read failed, will re-download: %s", e)

        # 从 SEC 下载
        if requests is None:
            return

        try:
            resp = self._request_get(
                "https://www.sec.gov/files/company_tickers.json",
                timeout=10,
            )
            if resp is None:
                return
            data = resp.json()

            # 写入缓存
            with open(cache_path, "w") as f:
                json.dump(data, f)

            self._cik_map = {
                v["ticker"].upper(): v["cik_str"]
                for v in data.values()
            }
        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
            _log.warning("加载 SEC CIK 映射失败: %s", e)

    def get_cik(self, ticker: str) -> Optional[int]:
        """根据 ticker 获取 CIK 编号"""
        return self._cik_map.get(ticker.upper())

    # ==================== 请求限流 ====================

    def _throttle(self):
        """通过 RateLimiter 遵守 SEC 10 req/s 限制"""
        sec_limiter.acquire()

    def _request_get(self, url: str, headers: Dict = None, timeout: int = 15):
        """带熔断保护的 HTTP GET"""
        if not sec_breaker.allow_request():
            _log.warning("SEC EDGAR 熔断器已打开，跳过请求: %s", url[:80])
            return None
        try:
            self._throttle()
            resp = requests.get(url, headers=headers or SEC_HEADERS, timeout=timeout)
            resp.raise_for_status()
            sec_breaker.record_success()
            return resp
        except (ConnectionError, TimeoutError, OSError, ValueError) as e:
            sec_breaker.record_failure()
            raise

    # ==================== Form 4 列表 ====================

    def get_recent_form4_filings(
        self, ticker: str, limit: int = 20
    ) -> List[Dict]:
        """
        获取指定 ticker 的最近 Form 4 申报列表

        返回: [{accessionNumber, filingDate, reportDate, primaryDocument}, ...]
        """
        cik = self.get_cik(ticker)
        if not cik:
            return []

        # 检查缓存（5 分钟有效）
        cache_path = CACHE_DIR / f"{ticker}_form4_list.json"
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < 300:
                try:
                    with open(cache_path) as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    _log.debug("Form4 cache read failed: %s", e)

        if requests is None:
            return []

        try:
            cik_padded = str(cik).zfill(10)
            resp = self._request_get(
                f"https://data.sec.gov/submissions/CIK{cik_padded}.json",
                timeout=15,
            )
            if resp is None:
                return []
            data = resp.json()

            recent = data.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])

            filings = []
            for i, form in enumerate(forms):
                if form == "4" and len(filings) < limit:
                    filings.append({
                        "accessionNumber": recent["accessionNumber"][i],
                        "filingDate": recent["filingDate"][i],
                        "reportDate": recent.get("reportDate", [""])[i] if i < len(recent.get("reportDate", [])) else "",
                        "primaryDocument": recent.get("primaryDocument", [""])[i] if i < len(recent.get("primaryDocument", [])) else "",
                        "cik": cik,
                    })

            # 写入缓存
            with open(cache_path, "w") as f:
                json.dump(filings, f)

            return filings

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
            _log.warning("获取 %s Form 4 列表失败: %s", ticker, e)
            return []

    # ==================== Form 4 XML 解析 ====================

    def parse_form4_xml(self, cik: int, accession_number: str, primary_doc: str) -> Optional[Dict]:
        """
        解析单个 Form 4 XML 文件，提取交易明细

        返回: {
            insider_name, insider_title, is_officer, is_director,
            transactions: [{date, code, shares, price, acquired_disposed, security}],
            report_date
        }
        """
        if requests is None:
            return None

        try:
            # 构建 XML URL
            acc_no_dashes = accession_number.replace("-", "")

            # primaryDocument 可能有 xsl 前缀，去掉
            xml_file = primary_doc
            if "/" in xml_file:
                xml_file = xml_file.split("/")[-1]

            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no_dashes}/{xml_file}"

            resp = self._request_get(url, headers={
                "User-Agent": SEC_USER_AGENT,
                "Accept": "application/xml",
            }, timeout=15)
            if resp is None:
                return None

            return self._parse_xml_content(resp.text)

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError, ET.ParseError) as e:
            # 尝试备用路径（直接使用完整 primaryDocument）
            try:
                acc_no_dashes = accession_number.replace("-", "")
                url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no_dashes}/{primary_doc}"
                resp = self._request_get(url, headers={
                    "User-Agent": SEC_USER_AGENT,
                }, timeout=15)
                if resp is None:
                    return None
                return self._parse_xml_content(resp.text)
            except (ConnectionError, TimeoutError, OSError, ValueError, ET.ParseError) as e2:
                _log.debug("Form4 XML fallback also failed: %s", e2)
                return None

    def _parse_xml_content(self, xml_text: str) -> Optional[Dict]:
        """解析 Form 4 XML 内容"""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return None

        result = {
            "insider_name": "",
            "insider_title": "",
            "is_officer": False,
            "is_director": False,
            "is_ten_percent_owner": False,
            "transactions": [],
            "report_date": "",
            "issuer_ticker": "",
        }

        # 报告日期
        pod = root.find(".//periodOfReport")
        if pod is not None and pod.text:
            result["report_date"] = pod.text

        # 发行人
        issuer_ticker = root.find(".//issuerTradingSymbol")
        if issuer_ticker is not None and issuer_ticker.text:
            result["issuer_ticker"] = issuer_ticker.text

        # 报告人信息
        owner_name = root.find(".//rptOwnerName")
        if owner_name is not None and owner_name.text:
            result["insider_name"] = owner_name.text

        officer_title = root.find(".//officerTitle")
        if officer_title is not None and officer_title.text:
            result["insider_title"] = officer_title.text

        is_officer = root.find(".//isOfficer")
        result["is_officer"] = is_officer is not None and is_officer.text == "1"

        is_director = root.find(".//isDirector")
        result["is_director"] = is_director is not None and is_director.text == "1"

        is_ten_pct = root.find(".//isTenPercentOwner")
        result["is_ten_percent_owner"] = is_ten_pct is not None and is_ten_pct.text == "1"

        # 非衍生品交易
        for txn in root.findall(".//nonDerivativeTransaction"):
            t = self._parse_transaction(txn)
            if t:
                result["transactions"].append(t)

        # 衍生品交易
        for txn in root.findall(".//derivativeTransaction"):
            t = self._parse_transaction(txn, is_derivative=True)
            if t:
                result["transactions"].append(t)

        return result

    def _parse_transaction(self, txn_elem, is_derivative: bool = False) -> Optional[Dict]:
        """解析单笔交易"""
        t = {
            "security": "",
            "date": "",
            "code": "",  # P=买入, S=卖出, M=行权, G=赠与, F=税费
            "shares": 0.0,
            "price": 0.0,
            "acquired_disposed": "",  # A=获得, D=处置
            "is_derivative": is_derivative,
        }

        # 证券名称
        sec_title = txn_elem.find(".//securityTitle/value")
        if sec_title is not None and sec_title.text:
            t["security"] = sec_title.text

        # 交易日期
        txn_date = txn_elem.find(".//transactionDate/value")
        if txn_date is not None and txn_date.text:
            t["date"] = txn_date.text

        # 交易代码
        txn_code = txn_elem.find(".//transactionCode")
        if txn_code is not None and txn_code.text:
            t["code"] = txn_code.text

        # 股数
        shares = txn_elem.find(".//transactionShares/value")
        if shares is not None and shares.text:
            try:
                t["shares"] = float(shares.text)
            except ValueError:
                pass

        # 价格（可能为 footnote 引用而非数值）
        price = txn_elem.find(".//transactionPricePerShare/value")
        if price is not None and price.text:
            try:
                t["price"] = float(price.text)
            except ValueError:
                t["price"] = 0.0

        # 获得/处置
        ad_code = txn_elem.find(".//transactionAcquiredDisposedCode/value")
        if ad_code is not None and ad_code.text:
            t["acquired_disposed"] = ad_code.text

        return t

    # ==================== 高层接口 ====================

    def get_insider_trades(
        self, ticker: str, days: int = 30, max_filings: int = 10
    ) -> Dict:
        """
        获取指定标的最近 N 天的内幕交易摘要

        返回: {
            ticker, total_filings, period_days,
            net_shares_bought, net_shares_sold, net_dollar_value,
            notable_trades: [{insider, title, code, shares, price, date, total_value}],
            insider_sentiment: "bullish" | "bearish" | "neutral",
            sentiment_score: 0-10,
            summary: str
        }
        """
        # 检查缓存（30 分钟有效）
        cache_path = CACHE_DIR / f"{ticker}_insider_summary.json"
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < 1800:
                try:
                    with open(cache_path) as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    _log.debug("Insider summary cache read failed: %s", e)

        filings = self.get_recent_form4_filings(ticker, limit=max_filings)

        if not filings:
            return self._empty_result(ticker, days)

        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        # 过滤最近 N 天
        recent_filings = [f for f in filings if f.get("filingDate", "") >= cutoff]

        if not recent_filings:
            # 使用所有可用 filings（可能超出 N 天但有参考价值）
            recent_filings = filings[:5]

        total_bought = 0.0
        total_sold = 0.0
        dollar_bought = 0.0
        dollar_sold = 0.0
        notable_trades = []

        for filing in recent_filings:
            parsed = self.parse_form4_xml(
                filing["cik"],
                filing["accessionNumber"],
                filing["primaryDocument"],
            )
            if not parsed:
                continue

            for txn in parsed.get("transactions", []):
                shares = txn.get("shares", 0)
                price = txn.get("price", 0)
                code = txn.get("code", "")
                ad = txn.get("acquired_disposed", "")

                # 只关注有意义的交易（P=买入, S=卖出）
                # F=税费扣股, M=行权, A=授予 不计入净买卖
                is_buy = code == "P" or (ad == "A" and code not in ("F", "M", "A", "G"))
                is_sell = code == "S" or (ad == "D" and code == "S")

                if is_buy:
                    total_bought += shares
                    dollar_bought += shares * price
                elif is_sell:
                    total_sold += shares
                    dollar_sold += shares * price

                # 记录重要交易（>$100K 或 >10000 股）
                total_value = shares * price
                if total_value > 100000 or shares > 10000 or code == "P":
                    notable_trades.append({
                        "insider": parsed.get("insider_name", "Unknown"),
                        "title": parsed.get("insider_title", ""),
                        "is_officer": parsed.get("is_officer", False),
                        "is_director": parsed.get("is_director", False),
                        "code": code,
                        "code_desc": self._code_desc(code),
                        "shares": shares,
                        "price": price,
                        "date": txn.get("date", filing.get("filingDate", "")),
                        "total_value": round(total_value, 2),
                        "security": txn.get("security", "Common Stock"),
                    })

        # 计算情绪
        net_dollar = dollar_bought - dollar_sold
        net_shares = total_bought - total_sold

        if dollar_bought > dollar_sold * 2 and dollar_bought > 50000:
            sentiment = "bullish"
            score = min(10.0, 6.0 + (dollar_bought / max(dollar_sold, 1)) * 0.5)
        elif dollar_sold > dollar_bought * 2 and dollar_sold > 50000:
            sentiment = "bearish"
            score = max(1.0, 5.0 - (dollar_sold / max(dollar_bought, 1)) * 0.5)
        else:
            sentiment = "neutral"
            score = 5.0

        # 如果有高管主动买入（code=P），加分
        officer_buys = [t for t in notable_trades if t["code"] == "P" and t["is_officer"]]
        if officer_buys:
            score = min(10.0, score + len(officer_buys) * 1.0)
            sentiment = "bullish"

        # 排序：按金额降序
        notable_trades.sort(key=lambda x: x["total_value"], reverse=True)
        notable_trades = notable_trades[:10]  # 最多保留 10 条

        # 生成摘要
        summary = self._build_summary(
            ticker, len(recent_filings), total_bought, total_sold,
            dollar_bought, dollar_sold, officer_buys, notable_trades
        )

        result = {
            "ticker": ticker,
            "total_filings": len(recent_filings),
            "period_days": days,
            "net_shares_bought": round(total_bought, 0),
            "net_shares_sold": round(total_sold, 0),
            "net_dollar_value": round(net_dollar, 2),
            "dollar_bought": round(dollar_bought, 2),
            "dollar_sold": round(dollar_sold, 2),
            "notable_trades": notable_trades,
            "insider_sentiment": sentiment,
            "sentiment_score": round(score, 1),
            "summary": summary,
        }

        # 写入缓存
        try:
            with open(cache_path, "w") as f:
                json.dump(result, f, ensure_ascii=False)
        except (OSError, TypeError) as e:
            _log.debug("Insider summary cache write failed: %s", e)

        return result

    def _empty_result(self, ticker: str, days: int) -> Dict:
        return {
            "ticker": ticker,
            "total_filings": 0,
            "period_days": days,
            "net_shares_bought": 0, "net_shares_sold": 0,
            "net_dollar_value": 0, "dollar_bought": 0, "dollar_sold": 0,
            "notable_trades": [],
            "insider_sentiment": "neutral",
            "sentiment_score": 5.0,
            "summary": f"{ticker}：近 {days} 天无内幕交易申报",
        }

    def _code_desc(self, code: str) -> str:
        return {
            "P": "买入", "S": "卖出", "M": "行权",
            "G": "赠与", "F": "税费扣股", "A": "授予",
            "D": "向公司处置", "C": "衍生品转换",
        }.get(code, code)

    def _build_summary(
        self, ticker, filing_count, bought, sold,
        dollar_bought, dollar_sold, officer_buys, notable_trades
    ) -> str:
        parts = [f"{ticker}：近期 {filing_count} 份 Form 4 申报"]

        if dollar_bought > 0:
            parts.append(f"内幕买入 ${dollar_bought:,.0f}")
        if dollar_sold > 0:
            parts.append(f"内幕卖出 ${dollar_sold:,.0f}")

        if officer_buys:
            names = ", ".join(t["insider"] for t in officer_buys[:3])
            parts.append(f"高管主动买入：{names}")

        if notable_trades:
            top = notable_trades[0]
            parts.append(
                f"最大交易：{top['insider']} {top['code_desc']} "
                f"{top['shares']:,.0f} 股 @ ${top['price']:.2f}"
            )

        return " | ".join(parts)


# ==================== 便捷函数 ====================

_client: Optional[SECEdgarClient] = None


def get_insider_trades(ticker: str, days: int = 30) -> Dict:
    """便捷函数：获取内幕交易摘要"""
    global _client
    if _client is None:
        _client = SECEdgarClient()
    return _client.get_insider_trades(ticker, days=days)
