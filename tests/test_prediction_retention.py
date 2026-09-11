"""predictions 保留期守卫（v0.45.178）。

治的形状：`Backtester.cleanup_old_predictions()` 是一条**不可逆、不备份**的
`DELETE FROM predictions`，每次日报扫描跑一次。它曾在调用点硬编码 180 天，
于是自 2026-08-25 起每天从库头永久删掉一个扫描日 —— 到 2026-09-10 已销毁 115 条
已完整回填 T+7 的样本（2026-02-25 ~ 2026-03-12），并让网站"累计收益"变成一个
180 天滚动窗口（看起来像"越改越低"）。

⚠️ **为什么守卫打在调用点而不是函数默认值上**：
仓里原有的两条测试（`tests/test_backtester.py::TestBacktesterCleanupPredictions`、
`tests/test_pipeline.py::TestBacktesterCleanup`）都**显式传 `days=180`**。
它们钉住的是"函数收到 180 时会不会删"，而生产的命运由调用点那一行决定 ——
把默认值改回 180，那两条测试一条都不会红。所以本文件扫的是调用点的 AST。

每条断言都附了能让它变红的变异（纪律：举不出变异就别加断言）。
"""

from __future__ import annotations

import ast
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent

# 生产调用点：文件 → 期望调用 `cleanup_old_predictions` 的模块
_CALL_SITES = ["alpha_hive_daily_report.py"]

# 保留期下限（天）。低于它就等于"回到滚动窗口"，样本量被结构性封顶。
# 3650 是现值；730 是"两年"这个宽松底线，留出未来调参空间又能挡住 180。
_MIN_RETENTION_DAYS = 730


def _find_cleanup_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "cleanup_old_predictions":
            out.append(node)
    return out


class TestProductionCallSiteDoesNotShrinkRetention:
    """生产调用点不许把保留期写小（变异：把调用改回 `cleanup_old_predictions(180)` ⇒ 红）"""

    def test_call_sites_exist(self):
        """先证明这个文件扫得到东西 —— 否则下一条断言是空转的恒真。

        变异：把 `_CALL_SITES` 改成一个不含该调用的文件 ⇒ 红。
        """
        total = sum(len(_find_cleanup_calls(_ROOT / f)) for f in _CALL_SITES)
        assert total >= 1, (
            f"在 {_CALL_SITES} 里没找到 cleanup_old_predictions 调用 —— "
            "调用点被挪走了？挪到哪就把哪加进 _CALL_SITES，别让本文件空转变绿。"
        )

    @pytest.mark.parametrize("fname", _CALL_SITES)
    def test_no_short_literal_retention(self, fname):
        """调用点不许传一个小于下限的字面量保留期。

        变异：`Backtester().cleanup_old_predictions(180)` ⇒ 红。
        """
        for call in _find_cleanup_calls(_ROOT / fname):
            literals = [a for a in call.args if isinstance(a, ast.Constant)]
            literals += [kw.value for kw in call.keywords
                         if kw.arg == "days" and isinstance(kw.value, ast.Constant)]
            for lit in literals:
                assert not isinstance(lit.value, (int, float)) or lit.value >= _MIN_RETENTION_DAYS, (
                    f"{fname}:{lit.lineno} 给 cleanup_old_predictions 传了 "
                    f"days={lit.value}，低于下限 {_MIN_RETENTION_DAYS} 天。"
                    "这条删除不可逆且不备份 —— 它每天从库头销毁一个扫描日，"
                    "并把网站'累计收益'悄悄变成滚动窗口。"
                )


    @pytest.mark.parametrize("fname", _CALL_SITES)
    def test_no_fraction_override_at_call_site(self, fname):
        """生产调用点不许传 `max_fraction=` —— 那等于把安全闸拆了。

        这个 override 是给一次性维护脚本和测试用的（那里"我知道要删很多"是真话）。
        变异：把调用改成 `cleanup_old_predictions(max_fraction=1.0)` ⇒ 红。
        """
        for call in _find_cleanup_calls(_ROOT / fname):
            names = {kw.arg for kw in call.keywords}
            assert "max_fraction" not in names, (
                f"{fname}:{call.lineno} 在生产调用点传了 max_fraction —— "
                "安全闸是这条不可逆删除唯一会红的观测点，不许在扫描路径上绕过它。"
            )


class TestConfiguredRetentionIsGenerous:
    """配置值本身不许被改小（变异：`PREDICTION_RETENTION_DAYS = 180` ⇒ 红）"""

    def test_default_retention_above_floor(self):
        import config

        assert config.PREDICTION_RETENTION_DAYS >= _MIN_RETENTION_DAYS, (
            f"config.PREDICTION_RETENTION_DAYS={config.PREDICTION_RETENTION_DAYS} "
            f"< {_MIN_RETENTION_DAYS}。predictions 表约 1,200 行、不到 1MB，"
            "从来没有存储上的理由 —— 改小它只会重新封顶 ic_rerun_readiness 的样本量。"
        )

    def test_resolved_at_call_time_not_frozen_at_import(self, tmp_path, monkeypatch):
        """保留期必须在**函数体内**解析，不能冻在 import 期或函数默认值里。

        变异：把 `days: Optional[int] = None` 改成 `days: int = config.PREDICTION_RETENTION_DAYS`
        （import 期求值）⇒ monkeypatch 失效 ⇒ 红。
        """
        import config
        from backtester import Backtester, PredictionStore

        db = str(tmp_path / "t.db")
        bt = Backtester(db_path=db)
        old = (datetime.now() - timedelta(days=900)).strftime("%Y-%m-%d")
        with sqlite3.connect(db) as conn:
            for i in range(100):
                conn.execute(
                    f"INSERT INTO {PredictionStore.TABLE} (date,ticker,final_score,direction)"
                    f" VALUES (?,?,5.0,'neutral')", (old, f"T{i}"))
            conn.commit()

        # 默认保留期（3650）下这 900 天前的行应当活着
        assert bt.cleanup_old_predictions() == 0

        # 把配置临时调到 30 天后再调用：若解析发生在调用时，这次就该删
        monkeypatch.setattr(config, "PREDICTION_RETENTION_DAYS", 30)
        monkeypatch.setattr(config, "PREDICTION_CLEANUP_MAX_FRACTION", 1.0)
        assert bt.cleanup_old_predictions() == 100


class TestCleanupSafetyValve:
    """一次删掉过大比例时必须拒绝并打 error（变异：删掉安全闸那段 ⇒ 红）"""

    def _seed(self, db, n_old, n_new):
        from backtester import PredictionStore
        old = (datetime.now() - timedelta(days=900)).strftime("%Y-%m-%d")
        new = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(db) as conn:
            for i in range(n_old):
                conn.execute(
                    f"INSERT INTO {PredictionStore.TABLE} (date,ticker,final_score,direction)"
                    f" VALUES (?,?,5.0,'neutral')", (old, f"O{i}"))
            for i in range(n_new):
                conn.execute(
                    f"INSERT INTO {PredictionStore.TABLE} (date,ticker,final_score,direction)"
                    f" VALUES (?,?,5.0,'neutral')", (new, f"N{i}"))
            conn.commit()

    def test_refuses_oversized_delete(self, tmp_path, caplog):
        """删除量 > 5% 时拒绝执行、返回 0、且行还在。"""
        from backtester import Backtester, PredictionStore

        db = str(tmp_path / "t.db")
        bt = Backtester(db_path=db)
        self._seed(db, n_old=50, n_new=50)  # 50% 会被删 → 超过 5% 上限

        with caplog.at_level("ERROR"):
            deleted = bt.cleanup_old_predictions(days=180)

        assert deleted == 0, "安全闸没拦住：一次删掉了半张表"
        with sqlite3.connect(db) as conn:
            assert conn.execute(
                f"SELECT COUNT(*) FROM {PredictionStore.TABLE}").fetchone()[0] == 100
        # 用 getMessage()：日志正文里含 "%"（"50.0% > 上限 5.0%"），
        # 手写 `r.message % r.args` 会抛 ValueError。
        assert any("拒绝执行" in r.getMessage() for r in caplog.records), \
            "拒绝时必须打 error —— 否则又是一次静默不作为"

    def test_small_delete_still_works_and_warns(self, tmp_path, caplog):
        """占比小的正常清理仍然放行，且打 warning（不是 info）让它在扫描日志里可见。"""
        from backtester import Backtester, PredictionStore

        db = str(tmp_path / "t.db")
        bt = Backtester(db_path=db)
        self._seed(db, n_old=2, n_new=100)  # ~2% < 5%

        with caplog.at_level("WARNING"):
            deleted = bt.cleanup_old_predictions(days=180)

        assert deleted == 2
        with sqlite3.connect(db) as conn:
            assert conn.execute(
                f"SELECT COUNT(*) FROM {PredictionStore.TABLE}").fetchone()[0] == 100
        assert any("清理旧预测" in r.getMessage() for r in caplog.records), (
            "真删了必须打 warning：这是不可逆的数据销毁，info 级在生产日志里等于没有")
