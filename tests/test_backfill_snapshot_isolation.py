"""
补跑不得污染当日期权快照槽位的回归闸（v0.45.16）

2026-08-25 实测事故：`_snap_date = pdt_today()` 完全不看 `--date` 目标日，
于是「在 8/25 补跑 8/24」写出的是 `options_snapshot_{T}_2026-08-25.json`
——占的是**今天**的位置。两个方向都错：

  ① 往回污染：8/24 的报告拿到 8/25 早上现拉的期权数据冒充 8/24 的
     （实测 NVDA 两天的 iv_rank/pc_ratio/GEX/rv_30d 逐字段完全相同）
  ② 往前污染：06:33 的补跑占住槽位 → 当天 14:00 的正式定时扫描一进门
     就命中缓存，**从未拉取过属于自己的期权数据**；当天所有报告的期权
     部分都是继承来的，且是修复前的旧代码算的

注意：修法**不是**「改用目标日命名」。CBOE/yfinance 的期权接口只有实时
快照、没有历史，补跑拿到的必然是运行时的链，命名成 8/24 只是把谎换个说法。
正解是槽位隔离 + 显式标注口径不一致。
"""

import os

import pytest


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """快照目录重定向到 tmp_path，并**显式重新启用快照缓存**。

    ⚠️ `tests/conftest.py:27` 给全部测试设了 `OPTIONS_SNAPSHOT_DISABLE=1`
    （防止 mock 期权链写进生产 cache/ 被正式扫描复用）。本文件测的恰恰**是
    快照的读写行为**，不解开这个开关，"补跑不读当日槽位"会因为"根本没读任何
    快照"而通过——**测试通过但什么也没守住**。cache_dir 已指向 tmp_path，
    重新启用不会污染生产目录。
    """
    from options_analyzer import OptionsAgent

    monkeypatch.delenv("OPTIONS_SNAPSHOT_DISABLE", raising=False)
    a = OptionsAgent()
    monkeypatch.setattr(a.fetcher, "cache_dir", str(tmp_path))
    return a


def _snap_path_for(agent, ticker, monkeypatch, target=None):
    """只取快照路径，不发网络请求——把真实计算短路掉。"""
    import options_analyzer as oa

    if target is None:
        monkeypatch.delenv("ALPHA_HIVE_TARGET_DATE", raising=False)
    else:
        monkeypatch.setenv("ALPHA_HIVE_TARGET_DATE", target)

    captured = {}

    class _Boom(RuntimeError):
        pass

    def _stop(*a, **k):
        raise _Boom

    # fetch_options_chain 是快照未命中后的第一个动作，用它当探针
    monkeypatch.setattr(agent.fetcher, "fetch_options_chain", _stop)
    try:
        agent.analyze(ticker, stock_price=100.0)
    except _Boom:
        pass
    captured["cache_dir"] = agent.fetcher.cache_dir
    return captured


class TestBackfillUsesSeparateSlot:
    def test_today_run_uses_plain_key(self, agent, monkeypatch, tmp_path):
        """未设目标日（=正常当日扫描）时，文件名保持原样，向后兼容。"""
        from hive_logger import pdt_today

        _snap_path_for(agent, "NVDA", monkeypatch, target=None)
        expected = tmp_path / f"options_snapshot_NVDA_{pdt_today()}.json"
        # 未命中缓存不会创建文件，改为断言"该路径形状"由实现产生
        import options_analyzer as oa
        import inspect
        src = inspect.getsource(oa.OptionsAgent.analyze)
        assert 'f"options_snapshot_{ticker}_{_snap_today}.json"' in src
        assert expected.name.startswith("options_snapshot_NVDA_")

    def test_backfill_key_differs_from_today(self):
        """核心断言：补跑的槽位名必须与当日槽位名不同。"""
        import inspect

        import options_analyzer as oa

        src = inspect.getsource(oa.OptionsAgent.analyze)
        assert "_backfilled-" in src, "补跑未使用独立槽位后缀"
        assert "ALPHA_HIVE_TARGET_DATE" in src, "未读取目标日"
        # 绝不能再出现"只看今天"的旧写法
        assert "_snap_date = pdt_today()  #" not in src

    def test_backfill_never_reads_today_slot(self, agent, monkeypatch, tmp_path):
        """补跑不得命中当日快照——这是往前污染的直接闸门。"""
        import json

        from hive_logger import pdt_today

        today = pdt_today()
        # 预置一份"当日快照"，内容可识别
        poison = tmp_path / f"options_snapshot_NVDA_{today}.json"
        poison.write_text(json.dumps({
            "_snapshot_timestamp": f"{today}T06:33:00", "iv_rank": 999.0,
        }))

        monkeypatch.setenv("ALPHA_HIVE_TARGET_DATE", "2026-01-02")

        class _Boom(RuntimeError):
            pass

        monkeypatch.setattr(agent.fetcher, "fetch_options_chain",
                            lambda *a, **k: (_ for _ in ()).throw(_Boom()))
        with pytest.raises(_Boom):
            agent.analyze("NVDA", stock_price=100.0)
        # 走到 fetch 说明**没有**命中那份当日快照

    def test_today_run_still_reads_today_slot(self, agent, monkeypatch, tmp_path):
        """别把闸门写成"永远不命中"——正常当日扫描仍须复用快照。"""
        import json

        from hive_logger import pdt_today

        today = pdt_today()
        (tmp_path / f"options_snapshot_NVDA_{today}.json").write_text(json.dumps({
            "_snapshot_timestamp": f"{today}T06:33:00", "iv_rank": 42.0,
        }))
        monkeypatch.delenv("ALPHA_HIVE_TARGET_DATE", raising=False)
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain",
                            lambda *a, **k: pytest.fail("当日扫描应命中快照，不该重新取链"))
        out = agent.analyze("NVDA", stock_price=100.0)
        assert out["iv_rank"] == 42.0


class TestBackfillMarksAsOfMismatch:
    def test_mismatch_flag_is_written(self):
        """补跑结果必须显式标注"期权是运行时拉的"——CBOE 无历史期权，瞒不住。"""
        import inspect

        import options_analyzer as oa

        src = inspect.getsource(oa.OptionsAgent.analyze)
        assert "_options_as_of_mismatch" in src
        assert "_options_target_date" in src
        assert "_options_fetched_on" in src


class TestDailyReportWiresTargetDate:
    def test_date_flag_sets_env(self):
        """`--date` 必须把目标日写进环境变量，否则快照层收不到。"""
        import inspect

        import alpha_hive_daily_report as adr

        src = inspect.getsource(adr)
        assert 'os.environ["ALPHA_HIVE_TARGET_DATE"] = args.date' in src
