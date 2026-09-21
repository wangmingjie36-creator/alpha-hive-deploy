"""health_check 对周度权重诊断的评级（v0.45.299）

起因：v0.45.172 归零 signal/risk_adj 之后，`weekly_optimizer` 连续两周（09-14、09-20）被
`infeasible_bounds` 阻断、没有产出任何建议权重，而 `health_check.check_weekly_optimizer`
只按「最后一条记录的年龄」评级——**跑了、有记录，就显示 ok ✓**。
独立审查（v0.45.295 后）指出：这是本项目「谁会红？」硬检查的缺口——阻断是**每周同样地**发生的，
恰恰是最需要有东西变红的那种失败。

这里用真实的 `check_weekly_optimizer`（不桩掉评级逻辑），只把 weight_history.jsonl 放进
conftest 隔离出来的 `ALPHA_HIVE_HOME`。
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import health_check as hc


def _write_history(records):
    home = Path(os.environ["ALPHA_HIVE_HOME"])
    (home / "weight_history.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def _record(skip_reason, days_ago=1):
    return {
        "timestamp": (datetime.now() - timedelta(days=days_ago)).isoformat(),
        "schema_version": 2, "action": "diagnose", "dry_run": True, "applied": False,
        "skip_reason": skip_reason, "method": "wls_time_decay", "n_samples": 1000,
    }


def _last_write_check():
    results = hc.check_weekly_optimizer()
    by_name = {r.name: r for r in results}
    return by_name["weekly-optimizer: 上次 weight_history 写入"]


def test_sandbox_actually_isolates_the_history_file():
    """夹具自证：check_weekly_optimizer 读的确实是我写进沙箱的那份，而不是真实 weight_history.jsonl。"""
    from hive_logger import PATHS
    assert str(PATHS.home) == os.environ["ALPHA_HIVE_HOME"]
    _write_history([_record("read_only_default")])
    assert _last_write_check().severity == "ok"


@pytest.mark.parametrize("reason", ["infeasible_bounds", "config_unparseable"])
def test_blocked_weekly_run_is_a_fail_even_when_the_record_is_fresh(reason):
    _write_history([_record(reason, days_ago=1)])
    r = _last_write_check()
    assert r.severity == "fail", "被阻断的周诊断没产出建议权重，不能因为『记录很新』就报 ok"
    assert reason in r.message
    assert r.detail["skip_reason"] == reason


@pytest.mark.parametrize("reason", ["read_only_default", "below_min_change", None])
def test_normal_read_only_runs_stay_ok(reason):
    """负对照：正常的只读/无显著变化不该被误伤。"""
    _write_history([_record(reason, days_ago=1)])
    assert _last_write_check().severity == "ok"


def test_blocked_only_matters_for_the_latest_record():
    """曾经被阻断、之后恢复了：最后一条正常就是 ok（评级看最新一条，不背历史包袱）。"""
    _write_history([_record("infeasible_bounds", days_ago=8), _record("read_only_default", days_ago=1)])
    assert _last_write_check().severity == "ok"
