"""
Google Calendar 集成模块 (Phase 3 P2)
自动同步催化剂到 Google Calendar，为高分机会添加提醒
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytz
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as GoogleCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
import googleapiclient.discovery as discovery

_log = logging.getLogger("alpha_hive.calendar_integrator")

# 宏观事件提醒配置
_MACRO_REMINDERS = {
    "fomc": [{'method': 'popup', 'minutes': 1440}, {'method': 'popup', 'minutes': 120}],
    "cpi":  [{'method': 'popup', 'minutes': 1440}, {'method': 'popup', 'minutes': 120}],
    "gdp":  [{'method': 'popup', 'minutes': 1440}, {'method': 'popup', 'minutes': 60}],
    "nfp":  [{'method': 'popup', 'minutes': 1440}, {'method': 'popup', 'minutes': 60}],
}
_MACRO_TIME = {"fomc": "14:00", "cpi": "08:30", "nfp": "08:30", "gdp": "08:30"}


class CalendarIntegrator:
    """Google Calendar 集成 - 催化剂同步 + 机会提醒"""

    SCOPES = ['https://www.googleapis.com/auth/calendar']

    CREDENTIALS_FILE = os.environ.get(
        "ALPHA_HIVE_GOOGLE_CREDENTIALS",
        os.path.expanduser("~/.alpha_hive_gmail_credentials.json")
    )
    TOKEN_FILE = str(Path.home() / ".alpha_hive_calendar_token.json")
    CALENDAR_ID = "primary"

    def __init__(self, credentials_file: str = None, calendar_id: str = None, token_file: str = None):
        """
        初始化 Google Calendar 集成

        Args:
            credentials_file: OAuth 凭证文件路径
            calendar_id: 目标日历 ID（默认为 primary）
            token_file: Token 存储文件路径

        失败时静默降级，self.service 设为 None
        """
        # 从 CALENDAR_CONFIG 读取配置
        try:
            from config import CALENDAR_CONFIG as _CC
        except ImportError:
            _CC = {}

        self.credentials_file = credentials_file or _CC.get("credentials_file", self.CREDENTIALS_FILE)
        self.token_file = token_file or _CC.get("token_file", self.TOKEN_FILE)
        self.calendar_id = calendar_id or _CC.get("calendar_id", self.CALENDAR_ID)
        self._score_threshold = _CC.get("opportunity_score_threshold", 7.5)
        self._reminder_minutes = _CC.get("reminder_advance_minutes", 30)
        self.service = None

        try:
            self._authenticate()
            _log.info("Google Calendar 服务已初始化 (calendar_id=%s)", self.calendar_id)
        except Exception as e:
            _log.warning("Calendar 认证失败，降级运行: %s", e)
            self.service = None

    def _authenticate(self):
        """
        OAuth 认证并构建 Google Calendar v3 服务

        Token scope 变更处理：如果旧 token 的 scope 不匹配（如从 Gmail 切换到 Calendar），
        自动删除旧 token 并触发重新授权。
        """
        credentials_path = Path(self.credentials_file)
        token_path = Path(self.token_file)

        if not credentials_path.exists():
            raise FileNotFoundError(f"凭证文件不存在: {self.credentials_file}")

        creds = None

        # 尝试加载已有 token
        if token_path.exists():
            try:
                creds = GoogleCredentials.from_authorized_user_file(
                    str(token_path), self.SCOPES
                )
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                _log.warning("Token 文件无效或 scope 不匹配，将重新授权: %s", e)
                creds = None

        # 如果 token 无效或过期，尝试刷新
        if creds and not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    _log.warning("Token 刷新失败，将重新授权: %s", e)
                    creds = None
            else:
                creds = None

        # scope 不匹配检测：旧 token 可能有 gmail.send 而非 calendar
        # 注意：from_authorized_user_file(path, scopes) 会将 creds.scopes 覆盖为
        # 传入值，因此需要直接从 token 文件读取原始 scopes 进行对比
        if creds and token_path.exists():
            try:
                with open(token_path, 'r') as f:
                    token_data = json.load(f)
                saved_scopes = token_data.get('scopes', [])
                if saved_scopes and not set(self.SCOPES).issubset(set(saved_scopes)):
                    _log.warning(
                        "Token scope 不匹配 (需要 %s, 文件中 %s)，删除旧 token 并重新授权",
                        self.SCOPES, saved_scopes
                    )
                    token_path.unlink(missing_ok=True)
                    creds = None
            except (json.JSONDecodeError, OSError):
                pass  # 无法读取 token 文件时忽略，后续认证流程会处理

        # 需要全新授权
        if not creds or not creds.valid:
            # 后台/无头环境检测：无交互终端时不启动 OAuth 浏览器流程
            if not os.isatty(0) and not os.environ.get("DISPLAY"):
                raise RuntimeError(
                    "无法在后台环境中完成 Google OAuth 授权。"
                    "请在终端中手动运行一次: python3 calendar_integrator.py"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                self.credentials_file, self.SCOPES
            )
            creds = flow.run_local_server(port=0)

            # 保存新 token
            token_path.parent.mkdir(parents=True, exist_ok=True)
            with open(token_path, 'w') as token:
                token.write(creds.to_json())
            _log.info("新 Calendar token 已保存到 %s", token_path)

        # 构建 Calendar v3 服务
        self.service = discovery.build('calendar', 'v3', credentials=creds)
        _log.info("Google Calendar API v3 认证成功")

    # ==================== 公共方法 ====================

    def sync_catalysts(self, catalysts: Dict = None, tickers: List[str] = None) -> Dict[str, Any]:
        """
        将催化剂事件同步到 Google Calendar

        使用 extendedProperties.private["alpha_hive_id"] 进行去重，
        已存在的事件不会重复创建。

        Args:
            catalysts: 催化剂字典（来自 config.CATALYSTS 或 earnings_watcher）
            tickers: 仅同步指定的 ticker 列表（默认同步全部）

        Returns:
            {'created': int, 'skipped': int, 'errors': int}
        """
        if catalysts is None:
            try:
                from config import CATALYSTS
                catalysts = CATALYSTS
            except ImportError:
                return {'created': 0, 'skipped': 0, 'errors': 0}

        stats = {'created': 0, 'skipped': 0, 'errors': 0}

        if not self.service:
            _log.warning("Calendar 服务不可用，跳过催化剂同步")
            return stats

        # 获取现有 Alpha Hive 事件的 ID 集合（用于去重）
        existing_ids = set()
        try:
            existing_events = self._get_existing_alpha_hive_events()
            for ev in existing_events:
                ext_props = ev.get('extendedProperties', {}).get('private', {})
                ah_id = ext_props.get('alpha_hive_id', '')
                if ah_id:
                    existing_ids.add(ah_id)
        except Exception as e:
            _log.warning("获取现有事件失败，将无法去重: %s", e)

        target_tickers = tickers if tickers else list(catalysts.keys())

        for ticker in target_tickers:
            if ticker not in catalysts:
                continue

            for catalyst in catalysts[ticker]:
                try:
                    event_id = self._generate_event_id(ticker, catalyst)

                    # 去重检查
                    if event_id in existing_ids:
                        stats['skipped'] += 1
                        _log.debug("跳过已存在事件: %s - %s", ticker, catalyst.get('event', '?'))
                        continue

                    # 构建日历事件
                    event_body = self._build_catalyst_event(ticker, catalyst, event_id)

                    # 调用 Calendar API 创建事件
                    created = self.service.events().insert(
                        calendarId=self.calendar_id,
                        body=event_body
                    ).execute()

                    stats['created'] += 1
                    _log.info(
                        "已创建日历事件: %s - %s (id=%s)",
                        ticker, catalyst.get('event', '?'), created.get('id', '?')
                    )

                except (KeyError, ValueError, TypeError) as e:
                    stats['errors'] += 1
                    _log.warning("创建日历事件失败 %s - %s: %s", ticker, catalyst.get('event', '?'), e)
                except Exception as e:
                    stats['errors'] += 1
                    _log.warning("Calendar API 错误 %s: %s", ticker, e)

        _log.info(
            "催化剂同步完成: 创建 %d, 跳过 %d, 错误 %d",
            stats['created'], stats['skipped'], stats['errors']
        )
        return stats

    def add_opportunity_reminder(self, ticker: str, score: float,
                                  direction: str, discovery: str = "") -> Optional[str]:
        """
        为高分机会添加 Google Calendar 提醒事件

        创建明日 09:00 ET 的提醒事件，包含提前通知。

        Args:
            ticker: 股票代码
            score: 机会评分（0-10）
            direction: 方向（"看多"/"看空"/"中性"）
            discovery: 机会发现摘要

        Returns:
            Google Calendar event ID 或 None
        """
        if not self.service or score < self._score_threshold:
            return None

        try:
            direction_emoji = "\U0001f4c8" if direction == "看多" else "\U0001f4c9" if direction == "看空" else "\u27a1\ufe0f"

            # 事件时间：明天 09:00 US/Eastern
            et = pytz.timezone('US/Eastern')
            tomorrow_9am = datetime.now(et).replace(
                hour=9, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)

            # 去重 ID
            reminder_id = f"alpha_hive_opp_{ticker}_{tomorrow_9am.strftime('%Y%m%d')}"

            # 检查是否已存在同一 ticker 同一天的提醒
            try:
                existing_events = self._get_existing_alpha_hive_events()
                for ev in existing_events:
                    ext_props = ev.get('extendedProperties', {}).get('private', {})
                    if ext_props.get('alpha_hive_id') == reminder_id:
                        _log.debug("跳过已存在的机会提醒: %s", reminder_id)
                        return ev.get('id', reminder_id)
            except Exception:
                pass  # 去重查询失败不阻断创建

            event_body = {
                'summary': f"\U0001f41d Alpha Hive: {ticker} {direction_emoji}{direction} ({score:.1f}/10)",
                'description': (
                    f"标的: {ticker}\n"
                    f"评分: {score:.1f}/10\n"
                    f"方向: {direction}\n"
                    f"摘要: {discovery}\n\n"
                    f"Alpha Hive 蜂群高分机会提醒"
                ),
                'start': {
                    'dateTime': tomorrow_9am.isoformat(),
                    'timeZone': 'US/Eastern'
                },
                'end': {
                    'dateTime': (tomorrow_9am + timedelta(minutes=30)).isoformat(),
                    'timeZone': 'US/Eastern'
                },
                'reminders': {
                    'useDefault': False,
                    'overrides': [
                        {'method': 'popup', 'minutes': self._reminder_minutes},
                        {'method': 'popup', 'minutes': 15}
                    ]
                },
                'extendedProperties': {
                    'private': {
                        'alpha_hive_id': reminder_id,
                        'ticker': ticker,
                        'event_type': 'opportunity_reminder',
                        'source': 'alpha_hive',
                        'score': str(score),
                        'direction': direction
                    }
                }
            }

            created = self.service.events().insert(
                calendarId=self.calendar_id,
                body=event_body
            ).execute()

            google_event_id = created.get('id', '')
            _log.info(
                "已添加机会提醒: %s %s%s (%.1f) -> event_id=%s",
                ticker, direction_emoji, direction, score, google_event_id
            )
            return google_event_id

        except Exception as e:
            _log.warning("添加提醒失败 %s: %s", ticker, e)
            return None

    def add_feedback_reminders(
        self,
        ticker: str,
        score: float,
        direction: str,
        evidence: str = "",
        base_date: Optional[datetime] = None,
    ) -> Dict[str, Optional[str]]:
        """
        为高分机会创建 T+1/T+7/T+30 回测提醒

        Args:
            ticker: 股票代码
            score: 机会评分（0-10）
            direction: 方向（"看多"/"看空"/"中性"）
            evidence: 关键证据摘要
            base_date: 机会的基准日期（默认：当前 ET 时间）

        Returns:
            {"feedback_t1": event_id, "feedback_t7": event_id, "feedback_t30": event_id}
        """
        result = {"feedback_t1": None, "feedback_t7": None, "feedback_t30": None}

        if not self.service or score < self._score_threshold:
            return result

        et = pytz.timezone('US/Eastern')
        base = base_date or datetime.now(et)
        if base.tzinfo is None:
            base = et.localize(base)
        date_str = base.strftime('%Y%m%d')

        _FEEDBACK_PLAN = [
            (1,  "feedback_t1",  f"\U0001f501 回看 {ticker} {direction} {score:.1f} — 价格变化？"),
            (7,  "feedback_t7",  f"\U0001f501 一周回顾 {ticker} — 趋势验证？"),
            (30, "feedback_t30", f"\U0001f501 月度复盘 {ticker} — 写回信息素板"),
        ]

        # 单次去重查询
        existing_ids = set()
        try:
            for ev in self._get_existing_alpha_hive_events():
                ah_id = ev.get('extendedProperties', {}).get('private', {}).get('alpha_hive_id', '')
                if ah_id:
                    existing_ids.add(ah_id)
        except Exception:
            pass

        for offset, event_type, summary in _FEEDBACK_PLAN:
            try:
                ah_id = f"alpha_hive_fb_{ticker}_{offset}d_{date_str}"

                if ah_id in existing_ids:
                    _log.debug("跳过已存在的回测提醒: %s", ah_id)
                    continue

                event_dt = base.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=offset)

                event_body = {
                    'summary': summary,
                    'description': (
                        f"标的: {ticker}\n"
                        f"原始评分: {score:.1f}/10\n"
                        f"方向: {direction}\n"
                        f"关键证据: {evidence[:200]}\n\n"
                        f"Alpha Hive 回测提醒 - T+{offset} 日\n"
                        f"请检查实际价格变化，更新 report_snapshots/ 快照数据"
                    ),
                    'start': {
                        'dateTime': event_dt.isoformat(),
                        'timeZone': 'US/Eastern'
                    },
                    'end': {
                        'dateTime': (event_dt + timedelta(minutes=30)).isoformat(),
                        'timeZone': 'US/Eastern'
                    },
                    'reminders': {
                        'useDefault': False,
                        'overrides': [{'method': 'popup', 'minutes': 30}]
                    },
                    'extendedProperties': {
                        'private': {
                            'alpha_hive_id': ah_id,
                            'ticker': ticker,
                            'event_type': event_type,
                            'source': 'alpha_hive',
                            'score': str(score),
                            'direction': direction,
                            'base_date': date_str,
                        }
                    }
                }

                created = self.service.events().insert(
                    calendarId=self.calendar_id,
                    body=event_body
                ).execute()

                result[event_type] = created.get('id', '')
                _log.info("已创建回测提醒: %s T+%d (id=%s)", ticker, offset, result[event_type])

            except Exception as e:
                _log.warning("创建回测提醒失败 %s T+%d: %s", ticker, offset, e)

        return result

    def sync_economic_calendar(self, days_ahead: int = 60) -> Dict[str, Any]:
        """
        同步宏观经济事件（FOMC/CPI/NFP/GDP）到 Google Calendar

        Args:
            days_ahead: 前瞻天数（默认 60）

        Returns:
            {'created': int, 'skipped': int, 'errors': int}
        """
        stats = {'created': 0, 'skipped': 0, 'errors': 0}

        if not self.service:
            _log.warning("Calendar 服务不可用，跳过经济日历同步")
            return stats

        try:
            from economic_calendar import get_upcoming_events
            macro_events = get_upcoming_events(days=days_ahead)
        except ImportError:
            _log.warning("economic_calendar 模块不可用")
            return stats

        if not macro_events:
            return stats

        # 去重
        existing_ids = set()
        try:
            for ev in self._get_existing_alpha_hive_events():
                ah_id = ev.get('extendedProperties', {}).get('private', {}).get('alpha_hive_id', '')
                if ah_id:
                    existing_ids.add(ah_id)
        except Exception as e:
            _log.warning("获取现有事件失败，将无法去重: %s", e)

        et = pytz.timezone('US/Eastern')

        for ev in macro_events:
            try:
                ev_type = ev.get('type', 'unknown')
                ev_date = ev.get('date', '')
                ev_name = ev.get('event', '')
                severity = ev.get('severity', 'medium')

                ah_id = f"alpha_hive_macro_{ev_type}_{ev_date.replace('-', '')}"

                if ah_id in existing_ids:
                    stats['skipped'] += 1
                    continue

                time_str = _MACRO_TIME.get(ev_type, "09:00")
                dt = datetime.fromisoformat(f"{ev_date}T{time_str}:00")
                dt = et.localize(dt)

                reminders = _MACRO_REMINDERS.get(ev_type, [
                    {'method': 'popup', 'minutes': 1440},
                    {'method': 'popup', 'minutes': 60}
                ])

                event_body = {
                    'summary': f"\U0001f3db\ufe0f {ev_name}",
                    'description': (
                        f"类型: {ev_type.upper()}\n"
                        f"严重程度: {severity}\n"
                        f"距今: {ev.get('days_until', '?')} 天\n\n"
                        f"Alpha Hive 宏观事件提醒"
                    ),
                    'start': {
                        'dateTime': dt.isoformat(),
                        'timeZone': 'US/Eastern'
                    },
                    'end': {
                        'dateTime': (dt + timedelta(hours=1)).isoformat(),
                        'timeZone': 'US/Eastern'
                    },
                    'reminders': {
                        'useDefault': False,
                        'overrides': reminders
                    },
                    'extendedProperties': {
                        'private': {
                            'alpha_hive_id': ah_id,
                            'ticker': '',
                            'event_type': 'macro_event',
                            'macro_type': ev_type,
                            'source': 'alpha_hive',
                            'severity': severity,
                        }
                    }
                }

                created = self.service.events().insert(
                    calendarId=self.calendar_id,
                    body=event_body
                ).execute()

                stats['created'] += 1
                _log.info("已创建宏观事件: %s (id=%s)", ev_name, created.get('id', '?'))

            except (KeyError, ValueError, TypeError) as e:
                stats['errors'] += 1
                _log.warning("创建宏观事件失败 %s: %s", ev.get('event', '?'), e)
            except Exception as e:
                stats['errors'] += 1
                _log.warning("Calendar API 错误 (macro): %s", e)

        _log.info("经济日历同步完成: 创建 %d, 跳过 %d, 错误 %d",
                  stats['created'], stats['skipped'], stats['errors'])
        return stats

    def add_thesis_break_alert(
        self,
        ticker: str,
        level: int,
        conditions: List[Dict],
        score_adjustment: float,
        original_score: float = 0.0,
    ) -> Optional[str]:
        """
        为 Thesis Break 触发创建紧急日历提醒

        Args:
            ticker: 股票代码
            level: 1 (预警) 或 2 (认输)
            conditions: 已触发的条件列表
            score_adjustment: 总评分调整量
            original_score: 原始评分

        Returns:
            Google Calendar event ID 或 None
        """
        if not self.service:
            return None

        try:
            et = pytz.timezone('US/Eastern')
            now = datetime.now(et)
            date_str = now.strftime('%Y%m%d')

            # L1: 30 分钟后事件, L2: 15 分钟后（更紧急）
            if level == 2:
                event_dt = now + timedelta(minutes=15)
                reminders = [
                    {'method': 'popup', 'minutes': 10},
                    {'method': 'popup', 'minutes': 2},
                ]
                event_type = 'thesis_break_l2'
            else:
                event_dt = now + timedelta(minutes=30)
                reminders = [
                    {'method': 'popup', 'minutes': 15},
                    {'method': 'popup', 'minutes': 5},
                ]
                event_type = 'thesis_break_l1'

            # 条件摘要（截断到 50 字符）
            cond_summary = ", ".join(c.get("metric", "?")[:20] for c in conditions[:3])
            if len(cond_summary) > 50:
                cond_summary = cond_summary[:47] + "..."

            ah_id = f"alpha_hive_break_{ticker}_{level}_{date_str}"

            # 去重
            try:
                for ev in self._get_existing_alpha_hive_events():
                    ext = ev.get('extendedProperties', {}).get('private', {})
                    if ext.get('alpha_hive_id') == ah_id:
                        _log.debug("跳过已存在的 thesis break 提醒: %s", ah_id)
                        return ev.get('id', ah_id)
            except Exception:
                pass

            # 条件详情
            cond_details = []
            for c in conditions:
                cond_details.append(
                    f"- {c.get('metric', '?')}: {c.get('trigger', '?')} "
                    f"(当前: {c.get('current_value', 'N/A')})"
                )

            event_body = {
                'summary': f"\U0001f6a8 {ticker} L{level} {cond_summary}",
                'description': (
                    f"标的: {ticker}\n"
                    f"级别: Level {level} ({'预警' if level == 1 else '认输'})\n"
                    f"评分调整: {score_adjustment:+.2f}\n"
                    f"原始评分: {original_score:.1f}\n\n"
                    f"触发条件:\n" + "\n".join(cond_details) + "\n\n"
                    f"Alpha Hive Thesis Break 紧急提醒"
                ),
                'start': {
                    'dateTime': event_dt.isoformat(),
                    'timeZone': 'US/Eastern'
                },
                'end': {
                    'dateTime': (event_dt + timedelta(minutes=15)).isoformat(),
                    'timeZone': 'US/Eastern'
                },
                'reminders': {
                    'useDefault': False,
                    'overrides': reminders
                },
                'extendedProperties': {
                    'private': {
                        'alpha_hive_id': ah_id,
                        'ticker': ticker,
                        'event_type': event_type,
                        'source': 'alpha_hive',
                        'level': str(level),
                        'score_adjustment': str(score_adjustment),
                    }
                }
            }

            created = self.service.events().insert(
                calendarId=self.calendar_id,
                body=event_body
            ).execute()

            google_event_id = created.get('id', '')
            _log.info("已创建 Thesis Break 提醒: %s L%d (id=%s)", ticker, level, google_event_id)
            return google_event_id

        except Exception as e:
            _log.warning("创建 Thesis Break 提醒失败 %s L%d: %s", ticker, level, e)
            return None

    def get_upcoming_events(self, days_ahead: int = 7) -> List[Dict]:
        """
        从 Google Calendar 获取未来 N 天内的 Alpha Hive 事件

        Args:
            days_ahead: 查看未来天数

        Returns:
            [{'ticker', 'event', 'date', 'days_until'}, ...]
        """
        if not self.service:
            return self._get_upcoming_events_fallback(days_ahead)

        try:
            et = pytz.timezone('US/Eastern')
            now = datetime.now(et)
            time_min = now.isoformat()
            time_max = (now + timedelta(days=days_ahead)).isoformat()

            events_result = self.service.events().list(
                calendarId=self.calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy='startTime',
                privateExtendedProperty='source=alpha_hive',
            ).execute()

            events = events_result.get('items', [])
            result = []

            for event in events:
                ext_props = event.get('extendedProperties', {}).get('private', {})
                ticker = ext_props.get('ticker', '')

                if not ticker:
                    ticker = self._extract_ticker_from_summary(event.get('summary', ''))

                start = event.get('start', {}).get('dateTime', event.get('start', {}).get('date', ''))
                try:
                    event_dt = datetime.fromisoformat(start)
                    if event_dt.tzinfo is None:
                        event_dt = et.localize(event_dt)
                    days_until = (event_dt.date() - now.date()).days
                except (ValueError, TypeError):
                    days_until = 0

                result.append({
                    'ticker': ticker,
                    'event': event.get('summary', ''),
                    'date': start,
                    'days_until': max(0, days_until)
                })

            result.sort(key=lambda x: x['days_until'])
            return result

        except Exception as e:
            _log.warning("Calendar API 查询失败，降级到本地催化剂: %s", e)
            return self._get_upcoming_events_fallback(days_ahead)

    # ==================== 私有方法 ====================

    def _get_existing_alpha_hive_events(self) -> List[Dict]:
        """
        获取所有 Alpha Hive 创建的日历事件（用于去重）

        使用 privateExtendedProperty 过滤，只返回本模块创建的事件。
        """
        if not self.service:
            return []

        try:
            et = pytz.timezone('US/Eastern')
            now = datetime.now(et)
            time_min = (now - timedelta(days=7)).isoformat()
            time_max = (now + timedelta(days=60)).isoformat()

            events_result = self.service.events().list(
                calendarId=self.calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy='startTime',
                privateExtendedProperty='source=alpha_hive',
                maxResults=250,
            ).execute()

            return events_result.get('items', [])

        except Exception as e:
            _log.warning("获取现有 Alpha Hive 事件失败: %s", e)
            return []

    def _get_upcoming_events_fallback(self, days_ahead: int = 7) -> List[Dict]:
        """降级方案：从 config.CATALYSTS 读取催化剂事件（Calendar API 不可用时）"""
        try:
            from config import CATALYSTS
        except ImportError:
            return []

        try:
            now = datetime.now(pytz.timezone('US/Eastern'))
            later = now + timedelta(days=days_ahead)
            result = []

            for ticker, catalysts_list in CATALYSTS.items():
                for catalyst in catalysts_list:
                    try:
                        date_str = catalyst.get('scheduled_date')
                        time_str = catalyst.get('scheduled_time', '09:00')
                        tz_str = catalyst.get('time_zone', 'US/Eastern')

                        dt = datetime.fromisoformat(f"{date_str}T{time_str}:00")
                        tz = pytz.timezone(tz_str)
                        dt_with_tz = tz.localize(dt)
                        dt_et = dt_with_tz.astimezone(pytz.timezone('US/Eastern'))

                        if now <= dt_with_tz <= later:
                            days_until = (dt_et.date() - now.date()).days
                            result.append({
                                'ticker': ticker,
                                'event': f"\U0001f4c5 {ticker} - {catalyst['event']}",
                                'date': dt_with_tz.isoformat(),
                                'days_until': days_until
                            })
                    except (ValueError, KeyError, TypeError, pytz.exceptions.UnknownTimeZoneError):
                        continue

            result.sort(key=lambda x: x['days_until'])
            return result

        except (ValueError, KeyError, TypeError) as e:
            _log.warning("降级获取催化剂事件失败: %s", e)
            return []

    def _generate_event_id(self, ticker: str, catalyst: Dict) -> str:
        """生成唯一的事件 ID（用于去重）"""
        date_str = catalyst.get('scheduled_date', '').replace('-', '')
        return f"alpha_hive_{ticker}_{catalyst['event'].replace(' ', '_')}_{date_str}"

    def _build_catalyst_event(self, ticker: str, catalyst: Dict, event_id: str) -> Dict:
        """构建催化剂日历事件"""
        date_str = catalyst.get('scheduled_date')
        time_str = catalyst.get('scheduled_time', '09:00')
        tz_str = catalyst.get('time_zone', 'US/Eastern')

        try:
            dt = datetime.fromisoformat(f"{date_str}T{time_str}:00")
            tz = pytz.timezone(tz_str)
            dt = tz.localize(dt)
        except (ValueError, TypeError, pytz.exceptions.UnknownTimeZoneError) as e:
            _log.debug("Catalyst date parse fallback: %s", e)
            try:
                dt = datetime.now(pytz.timezone(tz_str))
            except (pytz.exceptions.UnknownTimeZoneError, KeyError):
                dt = datetime.now(pytz.timezone('US/Eastern'))

        return {
            'summary': f"\U0001f4c5 {ticker} - {catalyst['event']}",
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
                    {'method': 'popup', 'minutes': 1440},   # 1 天前
                    {'method': 'popup', 'minutes': 60}       # 1 小时前
                ]
            },
            'extendedProperties': {
                'private': {
                    'alpha_hive_id': event_id,
                    'ticker': ticker,
                    'event_type': 'catalyst',
                    'source': 'alpha_hive'
                }
            }
        }

    def _extract_ticker_from_summary(self, summary: str) -> str:
        """从事件摘要中提取 ticker"""
        try:
            parts = summary.split('-')
            if len(parts) >= 2:
                ticker_part = parts[0].strip()
                ticker = ticker_part.replace('\U0001f4c5', '').replace('\U0001f41d', '').strip()
                return ticker
        except (ValueError, IndexError, AttributeError):
            pass
        return ""


def test_calendar_integration():
    """测试 Calendar 集成"""
    print("Google Calendar 集成测试\n")

    try:
        integrator = CalendarIntegrator()

        if integrator.service is None:
            print("Calendar 服务未初始化，无法继续测试")
            return

        print("Calendar 服务已连接\n")

        # 1. 同步催化剂
        print("1. 同步催化剂到日历...")
        stats = integrator.sync_catalysts()
        print(f"   创建: {stats['created']}, 跳过: {stats['skipped']}, 错误: {stats['errors']}")

        # 2. 获取即将到来的事件
        print("\n2. 获取未来 7 天的事件...")
        upcoming = integrator.get_upcoming_events(days_ahead=7)
        for event in upcoming[:5]:
            print(f"   - {event['ticker']}: {event['event']} ({event['days_until']} 天后)")

        # 3. 添加机会提醒
        print("\n3. 添加高分机会提醒...")
        event_id = integrator.add_opportunity_reminder(
            ticker="TEST",
            score=8.5,
            direction="看多",
            discovery="集成测试 - 可手动从日历删除"
        )
        if event_id:
            print(f"   提醒已创建: {event_id}")
        else:
            print("   提醒创建失败或被跳过")

        print("\n测试完成")

    except (FileNotFoundError, OSError, ValueError, RuntimeError, ConnectionError) as e:
        print(f"测试失败: {e}")


if __name__ == "__main__":
    test_calendar_integration()
