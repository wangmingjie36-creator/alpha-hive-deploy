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

import sqlite3



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
    """queen_distiller.py 的历史胜率折扣块：`_snap_dir` 与 close_t7 库
    必须落到**同一个** `ALPHA_HIVE_HOME`（v0.45.260 起经 `hive_logger.PATHS`
    解析），不能各自独立冻结、靠"巧合一致"对齐。

    v0.45.260（数据根迁移阶段 2）以前，这里各自独立算 `_project_root_ta =
    Path(__file__).resolve().parent.parent` 喂给 `_snap_dir` 与
    `close_t7_db_path` 两处，本条测试当时验的正是"两者用了同一个
    `__file__` 基准"（靠伪造 `qd_module.__file__` 驱动）。现在两处都改读
    `PATHS.home`/`feedback_loop._db_path()`（= `PATHS.db`）——同一个
    `ALPHA_HIVE_HOME` 环境变量，结构上不可能再分叉，所以改为驱动
    `ALPHA_HIVE_HOME` 而不是伪造 `__file__`；场景与断言（"两者必须指向
    同一目录"）不变。

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
        import feedback_loop
        from feedback_loop import ReportSnapshot

        monkeypatch.setattr(config, "TICKER_ACCURACY_FEEDBACK", {
            "enabled": True, "min_samples": 1,
            "discount_threshold": 0.99, "min_reliability": 0.5,
        })
        # v0.45.260 起 `_snap_dir` 与 close_t7 库都经 `hive_logger.PATHS`
        # 解析同一个 `ALPHA_HIVE_HOME`——不再需要伪造 `__file__`。
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path))
        monkeypatch.delenv("ALPHA_HIVE_DB_PATH", raising=False)
        # autouse fixture `_isolate_feedback_loop_close_t7_db` 无条件把
        # `feedback_loop.PHEROMONE_DB_PATH` 指向一个不存在的临时路径（防止
        # 别的测试意外打开生产库）——它不看 `ALPHA_HIVE_HOME`，所以本条要验的
        # "queen_distiller 不传 close_t7_db_path、让 feedback_loop 走自己的
        # 默认解析" 会被这个隔离钩子挡住。按该 fixture 文档自己写的做法：
        # 显式指回本测试真正要用的库，覆盖/绕开它。
        monkeypatch.setattr(feedback_loop, "PHEROMONE_DB_PATH", tmp_path / "pheromone.db")

        snap_dir = tmp_path / "report_snapshots"
        snap_dir.mkdir(parents=True)
        snap = ReportSnapshot("AAA", "2026-08-14")
        snap.direction = "Long"
        snap.entry_price = 100.0
        snap.actual_price_t7 = 999.0  # 脏值：100% 胜率，不该触发折扣
        snap.save_to_json(str(snap_dir))

        _make_pheromone_db(tmp_path / "pheromone.db",
                           [("AAA", "2026-08-14", 50.0)])  # 干净价：亏损

        # ⚠️ v0.45.176：这里必须用一个**权重非零**的维度当载体。
        # 原本写的是 "signal"——而 v0.45.172 已把 signal 归零，v0.45.176 又修掉了
        # `RegimeWeightAdjuster` 里 `max(0.02,·)` 把零复活成 2% 的地板，于是
        # 「唯一可用维度权重为 0」⇒ `weight_total==0` ⇒ base_score 退回中性 5.0
        # ⇒ 折扣恒为 0，本条与 close_t7 接线毫无关系地变红。
        # 本条的主题是 close_t7_db_path 接到哪个项目根，不是权重方案，
        # 故换成 sentiment（当前 0.325）——**不要**为了让它变绿去动地板。
        # 「只剩零权重维度 ⇒ 中性」这个行为本身另有守卫：
        # tests/test_zero_weight_invariant.py::TestZeroWeightDownstreamBehaviour
        results = [self._make_result("sentiment", 7.0)]
        out = queen.distill("AAA", results)

        assert out["ticker_accuracy_discount"] > 0, (
            "close_t7_db_path 未接到 queen_distiller.py 自己的项目根目录——"
            f"折扣应因干净价（亏损）触发，实际 out={out.get('ticker_accuracy_discount')}"
        )
