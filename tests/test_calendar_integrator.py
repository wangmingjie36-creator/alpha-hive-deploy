"""CalendarIntegrator 测试 - Google Calendar v3 集成（mock API 调用）"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
import pytz


def _make_integrator(**overrides):
    """创建带 mock service 的 CalendarIntegrator（跳过真实认证）"""
    from calendar_integrator import CalendarIntegrator
    ci = CalendarIntegrator.__new__(CalendarIntegrator)
    ci.service = overrides.get("service", MagicMock())
    ci.calendar_id = overrides.get("calendar_id", "primary")
    ci.credentials_file = ""
    ci.token_file = ""
    ci._score_threshold = overrides.get("score_threshold", 7.5)
    ci._reminder_minutes = overrides.get("reminder_minutes", 30)

    # 默认 mock: events().list() 返回空列表
    if ci.service and not isinstance(ci.service, type(None)):
        ci.service.events.return_value.list.return_value.execute.return_value = {'items': []}
        ci.service.events.return_value.insert.return_value.execute.return_value = {'id': 'mock_event_123'}

    return ci


# ==================== 初始化与降级 ====================

class TestCalendarInit:
    """初始化与降级测试"""

    def test_degrades_when_no_credentials(self, tmp_path):
        """凭证文件不存在时 service=None"""
        from calendar_integrator import CalendarIntegrator
        ci = CalendarIntegrator(credentials_file=str(tmp_path / "nonexistent.json"))
        assert ci.service is None

    @patch('calendar_integrator.discovery')
    @patch('calendar_integrator.InstalledAppFlow')
    def test_detects_gmail_scope_mismatch(self, mock_flow, mock_discovery, tmp_path):
        """旧 Gmail token scope 应被检测到并触发重新授权"""
        import json

        creds_file = tmp_path / "creds.json"
        creds_file.write_text('{"installed":{}}')
        token_file = tmp_path / "token.json"
        # 模拟旧 Gmail token 文件（包含 gmail.send scope）
        token_file.write_text(json.dumps({
            "token": "old_token",
            "refresh_token": "old_refresh",
            "client_id": "test_id",
            "client_secret": "test_secret",
            "scopes": ["https://www.googleapis.com/auth/gmail.send"]
        }))

        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.to_json.return_value = json.dumps({
            "scopes": ["https://www.googleapis.com/auth/calendar"]
        })
        mock_flow.from_client_secrets_file.return_value.run_local_server.return_value = mock_creds

        from calendar_integrator import CalendarIntegrator
        CalendarIntegrator(
            credentials_file=str(creds_file),
            token_file=str(token_file)
        )

        # 应触发全新 OAuth 流程（因为旧 token 被删除）
        mock_flow.from_client_secrets_file.assert_called()
        mock_discovery.build.assert_called_once_with('calendar', 'v3', credentials=mock_creds)

    @patch('calendar_integrator.discovery')
    @patch('calendar_integrator.InstalledAppFlow')
    def test_builds_calendar_v3_service(self, mock_flow, mock_discovery, tmp_path):
        """验证构建的是 Calendar v3 服务（非 Gmail）"""
        creds_file = tmp_path / "creds.json"
        creds_file.write_text('{"installed":{}}')

        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.to_json.return_value = '{}'
        mock_flow.from_client_secrets_file.return_value.run_local_server.return_value = mock_creds

        from calendar_integrator import CalendarIntegrator
        CalendarIntegrator(
            credentials_file=str(creds_file),
            token_file=str(tmp_path / "token.json")
        )

        mock_discovery.build.assert_called_once_with('calendar', 'v3', credentials=mock_creds)


# ==================== sync_catalysts ====================

class TestSyncCatalysts:
    """sync_catalysts() 测试"""

    def test_creates_events_for_catalysts(self):
        """有效催化剂应调用 insert() 创建事件"""
        ci = _make_integrator()
        catalysts = {
            "NVDA": [{"event": "Q4 Earnings", "scheduled_date": "2026-03-15",
                       "scheduled_time": "16:00", "time_zone": "US/Eastern"}]
        }
        stats = ci.sync_catalysts(catalysts=catalysts)
        assert stats['created'] == 1
        assert stats['errors'] == 0
        ci.service.events.return_value.insert.assert_called_once()

    def test_creates_multiple_events(self):
        """多个催化剂应分别创建"""
        ci = _make_integrator()
        catalysts = {
            "NVDA": [
                {"event": "Q4 Earnings", "scheduled_date": "2026-03-15",
                 "scheduled_time": "16:00", "time_zone": "US/Eastern"},
                {"event": "Computex", "scheduled_date": "2026-05-28",
                 "scheduled_time": "09:00", "time_zone": "Asia/Taipei"},
            ],
            "MSFT": [
                {"event": "Q3 Earnings", "scheduled_date": "2026-04-23",
                 "scheduled_time": "16:30", "time_zone": "US/Eastern"},
            ]
        }
        stats = ci.sync_catalysts(catalysts=catalysts)
        assert stats['created'] == 3

    def test_returns_zeros_when_no_service(self):
        """service=None 时返回全零"""
        ci = _make_integrator(service=None)
        stats = ci.sync_catalysts(catalysts={
            "NVDA": [{"event": "test", "scheduled_date": "2026-01-01"}]
        })
        assert stats == {'created': 0, 'skipped': 0, 'errors': 0}

    def test_skips_existing_events(self):
        """已存在 alpha_hive_id 的事件应跳过"""
        ci = _make_integrator()
        # Mock 返回已存在的事件
        ci.service.events.return_value.list.return_value.execute.return_value = {
            'items': [{
                'extendedProperties': {'private': {
                    'alpha_hive_id': 'alpha_hive_NVDA_Q4_Earnings_20260315',
                    'source': 'alpha_hive'
                }}
            }]
        }
        catalysts = {
            "NVDA": [{"event": "Q4 Earnings", "scheduled_date": "2026-03-15",
                       "scheduled_time": "16:00", "time_zone": "US/Eastern"}]
        }
        stats = ci.sync_catalysts(catalysts=catalysts)
        assert stats['skipped'] == 1
        assert stats['created'] == 0

    def test_handles_api_errors_gracefully(self):
        """单个事件创建失败不阻断其他事件"""
        ci = _make_integrator()
        ci.service.events.return_value.insert.return_value.execute.side_effect = [
            Exception("API error"),  # 第一个失败
            {'id': 'ok_event'},      # 第二个成功
        ]
        catalysts = {
            "NVDA": [
                {"event": "E1", "scheduled_date": "2026-03-15",
                 "scheduled_time": "16:00", "time_zone": "US/Eastern"},
                {"event": "E2", "scheduled_date": "2026-04-15",
                 "scheduled_time": "16:00", "time_zone": "US/Eastern"},
            ]
        }
        stats = ci.sync_catalysts(catalysts=catalysts)
        assert stats['created'] == 1
        assert stats['errors'] == 1


# ==================== add_opportunity_reminder ====================

class TestAddOpportunityReminder:
    """add_opportunity_reminder() 测试"""

    def test_returns_none_when_score_below_threshold(self):
        """score < 7.5 时返回 None"""
        ci = _make_integrator()
        result = ci.add_opportunity_reminder("NVDA", 5.0, "看多")
        assert result is None

    def test_returns_none_when_no_service(self):
        """service=None 时返回 None"""
        ci = _make_integrator(service=None)
        result = ci.add_opportunity_reminder("NVDA", 9.0, "看多")
        assert result is None

    def test_creates_event_when_score_meets_threshold(self):
        """score >= 7.5 时应创建事件并返回 ID"""
        ci = _make_integrator()
        result = ci.add_opportunity_reminder("NVDA", 8.5, "看多", "测试")
        assert result == 'mock_event_123'
        ci.service.events.return_value.insert.assert_called_once()

        # 验证事件体包含正确信息
        call_kwargs = ci.service.events.return_value.insert.call_args
        event_body = call_kwargs.kwargs.get('body') or call_kwargs[1].get('body')
        assert 'NVDA' in event_body['summary']
        assert event_body['extendedProperties']['private']['source'] == 'alpha_hive'
        assert event_body['extendedProperties']['private']['event_type'] == 'opportunity_reminder'

    def test_respects_custom_threshold(self):
        """自定义阈值应被遵守"""
        ci = _make_integrator(score_threshold=9.0)
        assert ci.add_opportunity_reminder("NVDA", 8.5, "看多") is None
        assert ci.add_opportunity_reminder("NVDA", 9.5, "看多") == 'mock_event_123'

    def test_returns_google_event_id_on_duplicate(self):
        """去重命中时应返回 Google event ID 而非 alpha_hive_id"""
        ci = _make_integrator()
        et = pytz.timezone('US/Eastern')
        tomorrow = (datetime.now(et) + timedelta(days=1)).strftime('%Y%m%d')
        expected_ah_id = f"alpha_hive_opp_NVDA_{tomorrow}"

        ci.service.events.return_value.list.return_value.execute.return_value = {
            'items': [{
                'id': 'google_cal_event_456',
                'extendedProperties': {'private': {
                    'alpha_hive_id': expected_ah_id,
                    'source': 'alpha_hive'
                }}
            }]
        }
        result = ci.add_opportunity_reminder("NVDA", 9.0, "看多")
        # 应返回 Google event ID，不是 alpha_hive_id
        assert result == 'google_cal_event_456'


# ==================== get_upcoming_events ====================

class TestGetUpcomingEvents:
    """get_upcoming_events() 测试"""

    def test_returns_events_from_api(self):
        """有 service 时应从 Calendar API 获取事件"""
        ci = _make_integrator()
        et = pytz.timezone('US/Eastern')
        future_dt = (datetime.now(et) + timedelta(days=2)).isoformat()

        ci.service.events.return_value.list.return_value.execute.return_value = {
            'items': [{
                'summary': 'NVDA - Q4 Earnings',
                'start': {'dateTime': future_dt},
                'extendedProperties': {'private': {
                    'ticker': 'NVDA', 'source': 'alpha_hive'
                }}
            }]
        }

        events = ci.get_upcoming_events(days_ahead=7)
        assert len(events) == 1
        assert events[0]['ticker'] == 'NVDA'

    def test_falls_back_when_no_service(self):
        """service=None 时应降级到 config 读取"""
        ci = _make_integrator(service=None)
        events = ci.get_upcoming_events(days_ahead=7)
        assert isinstance(events, list)

    def test_falls_back_on_api_error(self):
        """API 异常时应降级到 config 读取"""
        ci = _make_integrator()
        ci.service.events.return_value.list.return_value.execute.side_effect = Exception("API error")
        events = ci.get_upcoming_events(days_ahead=7)
        assert isinstance(events, list)

    def test_events_sorted_by_days_until(self):
        """返回事件应按 days_until 排序"""
        ci = _make_integrator()
        et = pytz.timezone('US/Eastern')
        dt_near = (datetime.now(et) + timedelta(days=1)).isoformat()
        dt_far = (datetime.now(et) + timedelta(days=5)).isoformat()

        ci.service.events.return_value.list.return_value.execute.return_value = {
            'items': [
                {'summary': 'B', 'start': {'dateTime': dt_far},
                 'extendedProperties': {'private': {'ticker': 'MSFT', 'source': 'alpha_hive'}}},
                {'summary': 'A', 'start': {'dateTime': dt_near},
                 'extendedProperties': {'private': {'ticker': 'NVDA', 'source': 'alpha_hive'}}},
            ]
        }

        events = ci.get_upcoming_events(days_ahead=7)
        assert events[0]['days_until'] <= events[1]['days_until']


# ==================== 辅助方法 ====================

class TestHelperMethods:
    """私有辅助方法测试"""

    def test_generate_event_id(self):
        """事件 ID 包含 ticker 和日期"""
        ci = _make_integrator()
        eid = ci._generate_event_id("NVDA", {
            "event": "Q4 Earnings", "scheduled_date": "2026-03-15"
        })
        assert "NVDA" in eid
        assert "20260315" in eid
        assert "alpha_hive" in eid

    def test_build_catalyst_event_structure(self):
        """构建的事件应包含必要字段"""
        ci = _make_integrator()
        event = ci._build_catalyst_event(
            "NVDA",
            {"event": "Q4 Earnings", "scheduled_date": "2026-03-15",
             "scheduled_time": "16:00", "time_zone": "US/Eastern"},
            "test_id"
        )
        assert 'summary' in event
        assert 'NVDA' in event['summary']
        assert 'start' in event
        assert 'end' in event
        assert 'reminders' in event
        assert 'extendedProperties' in event
        # 验证 source 标记
        assert event['extendedProperties']['private']['source'] == 'alpha_hive'
        assert event['extendedProperties']['private']['alpha_hive_id'] == 'test_id'
        assert event['extendedProperties']['private']['event_type'] == 'catalyst'
        # 验证 reminder 方法是 popup（非 notification）
        for override in event['reminders']['overrides']:
            assert override['method'] == 'popup'

    def test_build_catalyst_event_invalid_timezone(self):
        """无效时区不应抛出异常，应 fallback 到 US/Eastern"""
        ci = _make_integrator()
        event = ci._build_catalyst_event(
            "TEST",
            {"event": "Bad TZ Event", "scheduled_date": "2026-06-01",
             "scheduled_time": "10:00", "time_zone": "Invalid/Timezone"},
            "test_bad_tz"
        )
        # 应成功返回事件（fallback 到 US/Eastern），不抛异常
        assert 'summary' in event
        assert 'TEST' in event['summary']
        assert event['start']['timeZone'] == 'Invalid/Timezone'  # 原始值保留在 body 中

    def test_build_catalyst_event_missing_date(self):
        """缺少日期不应抛出异常"""
        ci = _make_integrator()
        event = ci._build_catalyst_event(
            "TEST",
            {"event": "No Date Event"},
            "test_no_date"
        )
        assert 'summary' in event
        assert 'start' in event

    def test_extract_ticker_from_summary(self):
        """从摘要中提取 ticker"""
        ci = _make_integrator()
        assert ci._extract_ticker_from_summary("\U0001f4c5 NVDA - Q4 Earnings") == "NVDA"
        assert ci._extract_ticker_from_summary("\U0001f41d Alpha Hive: TSLA") == ""  # 无 - 分隔符格式

    def test_get_existing_events_no_service(self):
        """service=None 时返回空列表"""
        ci = _make_integrator(service=None)
        assert ci._get_existing_alpha_hive_events() == []


# ==================== add_feedback_reminders ====================

class TestAddFeedbackReminders:
    """add_feedback_reminders() 测试 — T+1/T+7/T+30 回测提醒"""

    def test_returns_all_none_when_no_service(self):
        """service=None 时返回全 None"""
        ci = _make_integrator(service=None)
        result = ci.add_feedback_reminders("NVDA", 9.0, "看多")
        assert result == {"feedback_t1": None, "feedback_t7": None, "feedback_t30": None}

    def test_returns_all_none_below_threshold(self):
        """score < 7.5 时返回全 None"""
        ci = _make_integrator()
        result = ci.add_feedback_reminders("NVDA", 5.0, "看多")
        assert result == {"feedback_t1": None, "feedback_t7": None, "feedback_t30": None}

    def test_creates_three_events(self):
        """score >= 7.5 时应创建 3 个回测事件"""
        ci = _make_integrator()
        result = ci.add_feedback_reminders("NVDA", 8.5, "看多", "测试证据")
        # insert 应被调用 3 次（T+1, T+7, T+30）
        assert ci.service.events.return_value.insert.call_count == 3
        # 三个 event_id 都应有值
        assert result["feedback_t1"] == 'mock_event_123'
        assert result["feedback_t7"] == 'mock_event_123'
        assert result["feedback_t30"] == 'mock_event_123'

    def test_event_types_correct(self):
        """每个事件的 event_type 应分别为 feedback_t1/t7/t30"""
        ci = _make_integrator()
        ci.add_feedback_reminders("NVDA", 8.5, "看多")

        calls = ci.service.events.return_value.insert.call_args_list
        event_types = []
        for call in calls:
            body = call.kwargs.get('body') or call[1].get('body')
            et = body['extendedProperties']['private']['event_type']
            event_types.append(et)
            # 每个事件都应标记 source=alpha_hive
            assert body['extendedProperties']['private']['source'] == 'alpha_hive'
            # summary 应包含 🔁 前缀
            assert '\U0001f501' in body['summary']

        assert set(event_types) == {'feedback_t1', 'feedback_t7', 'feedback_t30'}

    def test_skips_existing_feedback(self):
        """已存在 T+1 的事件应被跳过，只创建 T+7 和 T+30"""
        ci = _make_integrator()
        et = pytz.timezone('US/Eastern')
        date_str = datetime.now(et).strftime('%Y%m%d')

        # Mock: T+1 已存在
        ci.service.events.return_value.list.return_value.execute.return_value = {
            'items': [{
                'id': 'existing_t1',
                'extendedProperties': {'private': {
                    'alpha_hive_id': f'alpha_hive_fb_NVDA_1d_{date_str}',
                    'source': 'alpha_hive'
                }}
            }]
        }

        result = ci.add_feedback_reminders("NVDA", 8.5, "看多")
        # T+1 被跳过 → insert 只调用 2 次
        assert ci.service.events.return_value.insert.call_count == 2
        # T+1 应为 None（未创建）
        assert result["feedback_t1"] is None
        # T+7 和 T+30 应有值
        assert result["feedback_t7"] == 'mock_event_123'
        assert result["feedback_t30"] == 'mock_event_123'


# ==================== sync_economic_calendar ====================

class TestSyncEconomicCalendar:
    """sync_economic_calendar() 测试 — 宏观经济事件同步"""

    def test_returns_zeros_no_service(self):
        """service=None 时返回全零"""
        ci = _make_integrator(service=None)
        stats = ci.sync_economic_calendar()
        assert stats == {'created': 0, 'skipped': 0, 'errors': 0}

    @patch('calendar_integrator.get_upcoming_events', create=True)
    def test_creates_macro_events(self, mock_get_events):
        """宏观事件应创建日历事件"""
        ci = _make_integrator()
        mock_get_events.return_value = [
            {'event': 'FOMC 利率决议', 'date': '2026-04-15', 'days_until': 36,
             'type': 'fomc', 'severity': 'high'},
            {'event': 'CPI 数据', 'date': '2026-04-10', 'days_until': 31,
             'type': 'cpi', 'severity': 'high'},
        ]

        # 使用 patch 注入 economic_calendar 模块到 import 系统
        import sys
        mock_module = MagicMock()
        mock_module.get_upcoming_events = mock_get_events
        with patch.dict(sys.modules, {'economic_calendar': mock_module}):
            stats = ci.sync_economic_calendar(days_ahead=60)

        assert stats['created'] == 2
        assert stats['errors'] == 0
        assert ci.service.events.return_value.insert.call_count == 2

        # 验证事件内容
        calls = ci.service.events.return_value.insert.call_args_list
        body0 = calls[0].kwargs.get('body') or calls[0][1].get('body')
        assert '\U0001f3db\ufe0f' in body0['summary']  # 🏛️ 前缀
        assert body0['extendedProperties']['private']['event_type'] == 'macro_event'
        assert body0['extendedProperties']['private']['macro_type'] == 'fomc'
        assert body0['extendedProperties']['private']['source'] == 'alpha_hive'

    @patch('calendar_integrator.get_upcoming_events', create=True)
    def test_dedup_skips_existing(self, mock_get_events):
        """已存在的宏观事件应被跳过"""
        ci = _make_integrator()

        # Mock: FOMC 已存在
        ci.service.events.return_value.list.return_value.execute.return_value = {
            'items': [{
                'id': 'existing_fomc',
                'extendedProperties': {'private': {
                    'alpha_hive_id': 'alpha_hive_macro_fomc_20260415',
                    'source': 'alpha_hive'
                }}
            }]
        }

        mock_get_events.return_value = [
            {'event': 'FOMC 利率决议', 'date': '2026-04-15', 'days_until': 36,
             'type': 'fomc', 'severity': 'high'},
            {'event': 'CPI 数据', 'date': '2026-04-10', 'days_until': 31,
             'type': 'cpi', 'severity': 'high'},
        ]

        import sys
        mock_module = MagicMock()
        mock_module.get_upcoming_events = mock_get_events
        with patch.dict(sys.modules, {'economic_calendar': mock_module}):
            stats = ci.sync_economic_calendar(days_ahead=60)

        assert stats['skipped'] == 1
        assert stats['created'] == 1


# ==================== add_thesis_break_alert ====================

class TestAddThesisBreakAlert:
    """add_thesis_break_alert() 测试 — Thesis Break 紧急提醒"""

    def test_returns_none_no_service(self):
        """service=None 时返回 None"""
        ci = _make_integrator(service=None)
        result = ci.add_thesis_break_alert(
            "NVDA", 1,
            [{"metric": "revenue_decline", "trigger": ">15%", "current_value": "18%"}],
            -2.0, 8.0
        )
        assert result is None

    def test_creates_l1_warning(self):
        """L1 预警应创建正确的事件"""
        ci = _make_integrator()
        conditions = [
            {"metric": "数据中心收入下滑", "trigger": ">15%", "current_value": "18%"},
        ]
        result = ci.add_thesis_break_alert("NVDA", 1, conditions, -1.5, 8.5)

        assert result == 'mock_event_123'
        ci.service.events.return_value.insert.assert_called_once()

        # 验证事件体
        call_kwargs = ci.service.events.return_value.insert.call_args
        body = call_kwargs.kwargs.get('body') or call_kwargs[1].get('body')
        assert '\U0001f6a8' in body['summary']  # 🚨
        assert 'NVDA' in body['summary']
        assert 'L1' in body['summary']
        assert body['extendedProperties']['private']['event_type'] == 'thesis_break_l1'
        assert body['extendedProperties']['private']['source'] == 'alpha_hive'
        assert body['extendedProperties']['private']['level'] == '1'

        # L1 提醒：15 分 + 5 分
        overrides = body['reminders']['overrides']
        minutes = sorted([o['minutes'] for o in overrides])
        assert minutes == [5, 15]

    def test_creates_l2_stop_urgent(self):
        """L2 认输应使用更紧急的提醒时间"""
        ci = _make_integrator()
        conditions = [
            {"metric": "earnings_miss", "trigger": ">20%", "current_value": "25%"},
            {"metric": "guidance_cut", "trigger": "True", "current_value": "True"},
        ]
        result = ci.add_thesis_break_alert("TSLA", 2, conditions, -3.0, 7.0)

        assert result == 'mock_event_123'

        call_kwargs = ci.service.events.return_value.insert.call_args
        body = call_kwargs.kwargs.get('body') or call_kwargs[1].get('body')
        assert body['extendedProperties']['private']['event_type'] == 'thesis_break_l2'
        assert body['extendedProperties']['private']['level'] == '2'

        # L2 提醒：10 分 + 2 分（更紧急）
        overrides = body['reminders']['overrides']
        minutes = sorted([o['minutes'] for o in overrides])
        assert minutes == [2, 10]

    def test_dedup_returns_existing_id(self):
        """去重命中时应返回已有 Google event ID"""
        ci = _make_integrator()
        et = pytz.timezone('US/Eastern')
        date_str = datetime.now(et).strftime('%Y%m%d')

        # Mock: L1 已存在
        ci.service.events.return_value.list.return_value.execute.return_value = {
            'items': [{
                'id': 'existing_break_event_789',
                'extendedProperties': {'private': {
                    'alpha_hive_id': f'alpha_hive_break_NVDA_1_{date_str}',
                    'source': 'alpha_hive'
                }}
            }]
        }

        conditions = [{"metric": "test", "trigger": ">10%", "current_value": "15%"}]
        result = ci.add_thesis_break_alert("NVDA", 1, conditions, -1.0, 8.0)

        # 应返回已有事件的 Google ID
        assert result == 'existing_break_event_789'
        # insert 不应被调用
        ci.service.events.return_value.insert.assert_not_called()
