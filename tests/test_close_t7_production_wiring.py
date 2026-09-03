"""close_t7 干净口径在三处生产消费者里的路径接线（v0.45.98）

背景：v0.45.87 把 generate_deep_v2.py / alpha_hive_daily_report.py /
swarm_agents/queen_distiller.py 接入 BacktestAnalyzer(clean_t7=True)，但三处
都没有显式传 close_t7_db_path，于是默认落到 feedback_loop.py 自己的
`Path(__file__).parent / "pheromone.db"`——这只反映"这份 feedback_loop.py
副本在哪"，与真实 pheromone.db 所在目录在 worktree/多 checkout 场景下不是
恒等的（复查时在本 worktree 里实测验证：本地空库 vs. ~/Desktop/Alpha Hive
下的真实生产库）。v0.45.98 让 generate_deep_v2.py / queen_distiller.py
改为显式传各自已经算好的项目根目录，与读取 report_snapshots 用的是同一个
基准，不再依赖 feedback_loop.py 的隐式缺省值。

本文件只覆盖这两处（有现成、低成本的测试入口）；alpha_hive_daily_report.py
那处需要构造完整 AlphaHiveDailyReporter 才能触发，仓库里从未有过该路径的
独立测试，超出本次改动应有的测试投入，未补——该处改动本身只是把
`self.report_dir`（已经是 Path，且与同一行 _snap_dir 用的是同一个变量）
传给 close_t7_db_path，属于同类型的机械改动。
"""

import json
import sqlite3

import pytest


def _make_pheromone_db(db_path, rows):
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE predictions (ticker TEXT, date TEXT, close_t7 REAL)")
    con.executemany("INSERT INTO predictions VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()


class TestGenerateDeepV2ClosePathWiring:
    """_load_ticker_accuracy 必须从 ALPHAHIVE_DIR（而非 feedback_loop.py 的
    __file__ 相对缺省值）读取 close_t7——out_dir（深度报告输出目录）与
    ALPHAHIVE_DIR 本来就不是同一个目录，这正是本 fix 要处理的错位场景。
    """

    def test_uses_alphahive_dir_for_close_t7_not_feedback_loop_default(
        self, tmp_path, monkeypatch,
    ):
        import generate_deep_v2 as g
        from feedback_loop import ReportSnapshot

        real_home = tmp_path / "real_alphahive"
        real_home.mkdir()
        _make_pheromone_db(real_home / "pheromone.db",
                           [("AAA", "2026-08-14", 50.0)])
        monkeypatch.setattr(g, "ALPHAHIVE_DIR", real_home)

        # out_dir 故意用另一个、与 ALPHAHIVE_DIR 无关的目录
        out_dir = tmp_path / "deep_output"
        snap_dir = out_dir / "report_snapshots"
        snap_dir.mkdir(parents=True)
        snap = ReportSnapshot("AAA", "2026-08-14")
        snap.direction = "Long"
        snap.entry_price = 100.0
        snap.actual_price_t7 = 999.0  # 脏值：若接线错了会被误用，制造 100% 胜率
        snap.save_to_json(str(snap_dir))

        result = g._load_ticker_accuracy("AAA", out_dir)

        assert result, f"应有结果，实际: {result}"
        # 干净价 50.0：收益 (50-100)/100*100% = -50%，方向 Long → 亏损
        assert result["win_rate"] == 0.0, (
            f"应使用 close_t7=50.0（亏损）而非脏值 999.0（盈利），实际: {result}"
        )
        assert abs(result["avg_ret_7d"] - (-50.0)) < 0.01, f"实际: {result}"

    def test_falls_back_to_dirty_value_when_alphahive_dir_has_no_db(
        self, tmp_path, monkeypatch,
    ):
        """对照组：ALPHAHIVE_DIR 下没有 pheromone.db 时必须保留旧行为
        （不覆盖），不能把样本清零——回归 v0.45.86 的既有契约。"""
        import generate_deep_v2 as g
        from feedback_loop import ReportSnapshot

        monkeypatch.setattr(g, "ALPHAHIVE_DIR", tmp_path / "no_such_home")

        out_dir = tmp_path / "deep_output"
        snap_dir = out_dir / "report_snapshots"
        snap_dir.mkdir(parents=True)
        snap = ReportSnapshot("AAA", "2026-08-14")
        snap.direction = "Long"
        snap.entry_price = 100.0
        snap.actual_price_t7 = 110.0
        snap.save_to_json(str(snap_dir))

        result = g._load_ticker_accuracy("AAA", out_dir)

        assert result and result.get("win_rate") == 100.0, f"实际: {result}"


class TestQueenDistillerClosePathWiring:
    """queen_distiller.py 的历史胜率折扣块：close_t7_db_path 必须与同一行
    _snap_dir 用同一个基准目录（_project_root_ta，即 queen_distiller.py 自己
    __file__ 的 project root），不能落到 feedback_loop.py 的隐式缺省值。

    该功能默认关闭（config.TICKER_ACCURACY_FEEDBACK["enabled"]=False），
    这里显式打开来验证接线，不代表建议在生产启用它。
    """

    def _make_result(self, dim, score, direction="bullish", confidence=0.8,
                     source="TestAgent"):
        return {
            "score": score, "direction": direction, "confidence": confidence,
            "discovery": f"test {dim}", "source": source, "dimension": dim,
            "data_quality": {"test": "real"},
        }

    def test_uses_own_project_root_for_close_t7(self, queen, tmp_path, monkeypatch):
        import config
        import swarm_agents.queen_distiller as qd_module
        from feedback_loop import ReportSnapshot

        monkeypatch.setattr(config, "TICKER_ACCURACY_FEEDBACK", {
            "enabled": True, "min_samples": 1,
            "discount_threshold": 0.99, "min_reliability": 0.5,
        })
        # 伪造 queen_distiller.py 自己的 __file__，让它的
        # Path(__file__).resolve().parent.parent 落在 tmp_path 下
        fake_file = tmp_path / "swarm_agents" / "queen_distiller.py"
        monkeypatch.setattr(qd_module, "__file__", str(fake_file))

        snap_dir = tmp_path / "report_snapshots"
        snap_dir.mkdir(parents=True)
        snap = ReportSnapshot("AAA", "2026-08-14")
        snap.direction = "Long"
        snap.entry_price = 100.0
        snap.actual_price_t7 = 999.0  # 脏值：100% 胜率，不该触发折扣
        snap.save_to_json(str(snap_dir))

        _make_pheromone_db(tmp_path / "pheromone.db",
                           [("AAA", "2026-08-14", 50.0)])  # 干净价：亏损

        results = [self._make_result("signal", 7.0)]
        out = queen.distill("AAA", results)

        assert out["ticker_accuracy_discount"] > 0, (
            "close_t7_db_path 未接到 queen_distiller.py 自己的项目根目录——"
            f"折扣应因干净价（亏损）触发，实际 out={out.get('ticker_accuracy_discount')}"
        )
