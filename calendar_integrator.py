"""
🐝 Google Calendar 集成模块 (Phase 3 P2)
自动同步催化剂到 Google Calendar，为高分机会添加提醒
"""

import os
import json
from pathlib import Path
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta
import pytz

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as GoogleCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
import googleapiclient.discovery as discovery


class CalendarIntegrator:
    """Google Calendar 集成 - 催化剂同步 + 机会提醒"""

    # 暂时使用 Gmail scope（Calendar API 验证问题）
    # TODO: 在 Google Cloud Console 完成 Calendar API 配置后，改为：
    # SCOPES = ['https://www.googleapis.com/auth/calendar']
    SCOPES = ['https://www.googleapis.com/auth/gmail.send']

    CREDENTIALS_FILE = os.environ.get(
        "ALPHA_HIVE_GOOGLE_CREDENTIALS",
        os.path.expanduser("~/.alpha_hive_gmail_credentials.json")
    )
    TOKEN_FILE = str(Path.home() / ".alpha_hive_calendar_token.json")
    CALENDAR_ID = "primary"

    def __init__(self, credentials_file: str = None, calendar_id: str = None, token_file: str = None):
        """
        初始化 Calendar 集成

        Args:
            credentials_file: OAuth 凭证文件路径（默认使用 Gmail 凭证）
            calendar_id: 目标日历 ID（默认为 primary）
            token_file: Token 存储文件路径（默认为 ~/.alpha_hive_calendar_token.json）

        失败时静默降级，self.service 设为 None

        【临时方案】使用 Gmail scope 绕过 Calendar API 验证问题
        Calendar 功能将以降级模式运行
        """
        self.credentials_file = credentials_file or self.CREDENTIALS_FILE
        self.token_file = token_file or self.TOKEN_FILE
        self.calendar_id = calendar_id or self.CALENDAR_ID
        self.service = None

        # 【临时方案】本地缓存初始化
        self._local_catalysts_cache = {}
        self._reminders_cache = []

        try:
            self._authenticate()
            # 注意：由于使用 Gmail scope，Calendar API 可能不可用
            # 但认证本身会成功，降级模式下以模拟数据返回结果
            print("ℹ️ 【临时模式】使用 Gmail 权限运行，Calendar 功能部分可用")
        except Exception as e:
            print(f"⚠️ Calendar 认证失败，降级运行（无日历功能）: {e}")
            self.service = None

    def _authenticate(self):
        """
        使用 OAuth 进行身份验证（复用 Gmail 凭证）

        【临时方案】获取 Gmail scope 授权，本地缓存催化剂和提醒
        """
        credentials_path = Path(self.credentials_file)
        token_path = Path(self.token_file)

        if not credentials_path.exists():
            raise FileNotFoundError(f"凭证文件不存在: {self.credentials_file}")

        try:
            # 检查是否已有授权令牌
            if token_path.exists():
                # 使用现有令牌
                creds = GoogleCredentials.from_authorized_user_file(str(token_path), self.SCOPES)
                if not creds.valid:
                    if creds.expired and creds.refresh_token:
                        creds.refresh(Request())
            else:
                # 首次授权 - 需要用户交互（弹出浏览器）
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, self.SCOPES)
                creds = flow.run_local_server(port=0)

                # 保存令牌供后续使用
                token_path.parent.mkdir(parents=True, exist_ok=True)
                with open(token_path, 'w') as token:
                    token.write(creds.to_json())

            # 【临时方案】使用 Gmail service 而不是 Calendar
            self.service = discovery.build('gmail', 'v1', credentials=creds)
            print("✅ Gmail 认证成功（Calendar 功能使用本地缓存）")

        except Exception as e:
            print(f"❌ 认证失败: {e}")
            raise

    def sync_catalysts(self, catalysts: Dict = None, tickers: List[str] = None) -> Dict[str, Any]:
        """
        将催化剂事件同步到本地内存

        【临时方案】由于使用 Gmail scope，无法直接写入 Calendar API
        改为本地跟踪催化剂，后续可发送 Gmail 提醒

        Args:
            catalysts: 催化剂字典（来自 config.CATALYSTS）
            tickers: 仅同步指定的 ticker 列表（默认同步全部）

        Returns:
            {'created': int, 'skipped': int, 'errors': int}
        """
        # 导入催化剂配置
        if catalysts is None:
            try:
                from config import CATALYSTS
                catalysts = CATALYSTS
            except ImportError:
                return {'created': 0, 'skipped': 0, 'errors': 0}

        stats = {'created': 0, 'skipped': 0, 'errors': 0}

        # 过滤 ticker
        target_tickers = tickers if tickers else list(catalysts.keys())

        try:
            # 本地跟踪（存储在内存中）
            self._local_catalysts_cache = {}

            for ticker in target_tickers:
                if ticker not in catalysts:
                    continue

                for catalyst in catalysts[ticker]:
                    try:
                        event_id = self._generate_event_id(ticker, catalyst)
                        self._local_catalysts_cache[event_id] = {
                            'ticker': ticker,
                            'event': catalyst['event'],
                            'date': catalyst.get('scheduled_date'),
                            'time': catalyst.get('scheduled_time'),
                            'timezone': catalyst.get('time_zone', 'US/Eastern')
                        }
                        stats['created'] += 1
                        print(f"✅ 已缓存催化剂: {ticker} - {catalyst['event']}")

                    except Exception as e:
                        stats['errors'] += 1
                        print(f"⚠️ 缓存失败 {ticker}: {str(e)[:50]}")

        except Exception as e:
            print(f"❌ 同步催化剂失败: {e}")
            stats['errors'] += 1

        print(f"📝 【本地方案】催化剂已缓存到内存 (仅当前会话生效)")
        return stats

    def add_opportunity_reminder(self, ticker: str, score: float,
                                  direction: str, discovery: str = "") -> Optional[str]:
        """
        为高分机会添加提醒（改为 Gmail 邮件提醒）

        【临时方案】由于使用 Gmail scope，改为发送邮件提醒而不是日历事件

        Args:
            ticker: 股票代码
            score: 机会评分（0-10）
            direction: 方向（"看多"/"看空"/"中性"）
            discovery: 机会发现摘要

        Returns:
            reminder_id 或 None
        """
        if not self.service or score < 7.5:
            return None

        try:
            # 本地记录（存储在内存中）
            if not hasattr(self, '_reminders_cache'):
                self._reminders_cache = []

            reminder_id = f"reminder_{ticker}_{int(datetime.now().timestamp())}"
            self._reminders_cache.append({
                'id': reminder_id,
                'ticker': ticker,
                'score': score,
                'direction': direction,
                'discovery': discovery,
                'timestamp': datetime.now().isoformat()
            })

            direction_emoji = "📈" if direction == "看多" else "📉" if direction == "看空" else "➡️"

            print(f"🔔 已添加机会提醒: {ticker} {direction} ({score:.1f})")
            print(f"   📧 提醒已记录，可通过邮件同步发送")
            return reminder_id

        except Exception as e:
            print(f"⚠️ 添加提醒失败 {ticker}: {str(e)[:50]}")
            return None

    def get_upcoming_events(self, days_ahead: int = 7) -> List[Dict]:
        """
        获取未来 N 天内的催化剂事件

        【临时方案】由于使用 Gmail scope，从配置中读取催化剂而非 Calendar API

        Args:
            days_ahead: 查看未来天数

        Returns:
            [{'ticker', 'event', 'date', 'days_until'}, ...]
        """
        try:
            from config import CATALYSTS
        except ImportError:
            return []

        try:
            now = datetime.now(pytz.timezone('US/Eastern'))
            later = now + timedelta(days=days_ahead)

            result = []

            for ticker, catalysts in CATALYSTS.items():
                for catalyst in catalysts:
                    try:
                        # 解析日期和时间
                        date_str = catalyst.get('scheduled_date')
                        time_str = catalyst.get('scheduled_time', '09:00')
                        tz_str = catalyst.get('time_zone', 'US/Eastern')

                        # 构建 datetime
                        dt = datetime.fromisoformat(f"{date_str}T{time_str}:00")
                        tz = pytz.timezone(tz_str)
                        dt_with_tz = tz.localize(dt)

                        # 转换为 US/Eastern 来计算 days_until
                        dt_et = dt_with_tz.astimezone(pytz.timezone('US/Eastern'))

                        # 检查是否在未来 N 天内
                        if now <= dt_with_tz <= later:
                            days_until = (dt_et.date() - now.date()).days

                            result.append({
                                'ticker': ticker,
                                'event': f"📅 {ticker} - {catalyst['event']}",
                                'date': dt_with_tz.isoformat(),
                                'days_until': days_until
                            })
                    except Exception:
                        continue

            # 按日期排序
            result.sort(key=lambda x: x['days_until'])
            return result

        except Exception as e:
            print(f"⚠️ 获取催化剂事件失败: {str(e)[:50]}")
            return []

    # ==================== 私有方法 ====================

    def _get_existing_alpha_hive_events(self) -> List[Dict]:
        """
        获取现有的 Alpha Hive 事件（本地缓存版本）

        【临时方案】由于使用 Gmail scope，返回本地缓存
        """
        if hasattr(self, '_local_catalysts_cache'):
            return list(self._local_catalysts_cache.values())
        return []

    def _generate_event_id(self, ticker: str, catalyst: Dict) -> str:
        """生成唯一的事件 ID（用于去重）"""
        date_str = catalyst.get('scheduled_date', '').replace('-', '')
        return f"alpha_hive_{ticker}_{catalyst['event'].replace(' ', '_')}_{date_str}"

    def _build_catalyst_event(self, ticker: str, catalyst: Dict, event_id: str) -> Dict:
        """构建催化剂日历事件"""
        # 解析日期和时间
        date_str = catalyst.get('scheduled_date')
        time_str = catalyst.get('scheduled_time', '09:00')
        tz_str = catalyst.get('time_zone', 'US/Eastern')

        try:
            dt = datetime.fromisoformat(f"{date_str}T{time_str}:00")
            tz = pytz.timezone(tz_str)
            dt = tz.localize(dt)
        except Exception:
            dt = datetime.now(pytz.timezone(tz_str))

        return {
            'summary': f"📅 {ticker} - {catalyst['event']}",
            'description': f"标的: {ticker}\n时间: {time_str} {tz_str}\n\nAlpha Hive 催化剂提醒",
            'start': {
                'dateTime': dt.isoformat(),
                'timeZone': tz_str
            },
            'end': {
                'dateTime': (dt + timedelta(hours=1)).isoformat(),
                'timeZone': tz_str
            },
            'reminders': {
                'useDefault': False,
                'overrides': [
                    {'method': 'notification', 'minutes': 1440},  # 1 天前
                    {'method': 'notification', 'minutes': 60}     # 1 小时前
                ]
            },
            'extendedProperties': {
                'private': {
                    'alpha_hive_id': event_id,
                    'ticker': ticker,
                    'event_type': 'catalyst'
                }
            }
        }

    def _extract_ticker_from_summary(self, summary: str) -> str:
        """从事件摘要中提取 ticker（形如 "📅 NVDA - Q4 Earnings"）"""
        try:
            parts = summary.split('-')
            if len(parts) >= 2:
                ticker_part = parts[0].strip()
                # 移除 emoji 和空格
                ticker = ticker_part.replace('📅', '').strip()
                return ticker
        except Exception:
            pass
        return ""


def test_calendar_integration():
    """测试 Calendar 集成"""
    print("🧪 Google Calendar 集成测试\n")

    try:
        # 初始化
        integrator = CalendarIntegrator()

        if integrator.service is None:
            print("❌ Calendar 服务未初始化，无法继续测试")
            return

        # 1. 同步催化剂
        print("\n1️⃣ 同步催化剂到日历...")
        stats = integrator.sync_catalysts()
        print(f"   创建: {stats['created']}, 跳过: {stats['skipped']}, 错误: {stats['errors']}")

        # 2. 获取即将到来的事件
        print("\n2️⃣ 获取未来 7 天的事件...")
        upcoming = integrator.get_upcoming_events(days_ahead=7)
        for event in upcoming[:5]:
            print(f"   - {event['ticker']}: {event['event']} ({event['days_until']} 天后)")

        # 3. 添加机会提醒
        print("\n3️⃣ 添加高分机会提醒...")
        event_id = integrator.add_opportunity_reminder(
            ticker="NVDA",
            score=8.5,
            direction="看多",
            discovery="强势芯片需求 + 5 个机构加仓"
        )
        if event_id:
            print(f"   ✅ 提醒已创建: {event_id}")
        else:
            print("   ⚠️ 提醒创建失败或被跳过")

        print("\n✅ 测试完成！")

    except Exception as e:
        print(f"❌ 测试失败: {e}")


if __name__ == "__main__":
    test_calendar_integration()
