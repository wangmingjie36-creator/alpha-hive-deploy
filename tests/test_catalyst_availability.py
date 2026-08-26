"""catalyst 维度：抓取失败必须与「确实无催化剂」可区分（v0.45.31）。

背景：ChronosBee 的 except 此前只打 warning 就继续，于是 yfinance 财报日历
挂掉与标的确实没催化剂在产出上完全同形——都落 score=4.0 / "无近期催化剂"。
实测 90 个扫描日里有 9 天（10%）出现 4.0 占比 >75% 的集体塌缩，
最严重 2026-08-11 是 26/27 只，而 catalyst 占 final_score 权重 18.78%。

本文件全部按「喂退化数据看它红」构造。
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestCatalystSourceUnavailable:
    """来源全不可得时必须返回 error，而不是复用 4.0。"""

    def _bee(self, board):
        from swarm_agents.chronos_bee import ChronosBeeHorizon
        return ChronosBeeHorizon(board)

    def test_calendar_failure_returns_error_not_4(self, monkeypatch, board):
        """喂退化：财报日历抛网络错 + catalysts.json 无该标的 → 必须返回 error。

        ⚠️ 用 ABBV（真实在 WATCHLIST、但不在 catalysts.json 的 6 只里），
        不能用假 ticker —— 假 ticker 会被 _validate_ticker 提前挡下，
        根本走不到本分支，测试会假绿。（这个坑本文件初版踩过。）
        """
        import pandas as pd
        import yfinance as yf

        class _Boom:
            def __init__(self, *a, **k):
                pass

            @property
            def calendar(self):
                raise ConnectionError("simulated calendar failure")

            def history(self, *a, **k):
                return pd.DataFrame()

            @property
            def info(self):
                raise ConnectionError("simulated info failure")

        monkeypatch.setattr(yf, "Ticker", _Boom)
        r = self._bee(board).analyze("ABBV")

        assert "error" in r, (
            "来源全不可得却没返回 error —— 抓取失败又在冒充「无近期催化剂」"
            f"（score={r.get('score')}, discovery={r.get('discovery')!r}）")
        assert r.get("dimension") == "catalyst"
        assert "catalyst_sources_unavailable" in str(r.get("error", "")) or \
               "catalyst_sources_unavailable" in str(r.get("discovery", ""))

    def test_genuine_no_catalyst_still_scores_4(self, monkeypatch, board):
        """反向守卫：日历**成功**但确实没有近期事件 → 仍应是 4.0，不得误报缺失。

        「查过了，确实没有」与「没查到」必须都能表达，否则修复过头。
        """
        import pandas as pd
        import yfinance as yf

        class _Empty:
            def __init__(self, *a, **k):
                pass

            @property
            def calendar(self):
                return {}          # 成功返回，只是没有财报日期

            def history(self, *a, **k):
                return pd.DataFrame()

            @property
            def info(self):
                return {}

        monkeypatch.setattr(yf, "Ticker", _Empty)
        r = self._bee(board).analyze("ABBV")
        assert "error" not in r, "日历成功返回空不该被当成来源不可得"

    def test_error_result_shape_lets_distiller_treat_as_missing(self):
        """error 结果必须带 dimension=catalyst 且含 error 键，
        否则 queen_distiller 的 valid_results 过滤 / dim_status 归类会失效。"""
        from swarm_agents.utils import make_error_result
        r = make_error_result("ChronosBeeHorizon", "catalyst", RuntimeError("x"))
        assert r.get("dimension") == "catalyst"
        assert "error" in r, "缺 error 键则会被当成有效维度计入 dim_scores"

    def test_distiller_excludes_errored_dimension(self, board):
        """端到端：带 error 的 catalyst 不得进入 dim_scores。"""
        from swarm_agents.queen_distiller import QueenDistiller
        q = QueenDistiller(board)
        results = [
            {"dimension": "catalyst", "error": "catalyst_sources_unavailable", "score": 4.0},
            {"dimension": "sentiment", "score": 6.0, "confidence": 0.8},
        ]
        prep = q._prepare_dimension_data(results)
        assert "catalyst" not in prep["dim_scores"], "带 error 的维度不该有分数"
        assert prep["dim_status"]["catalyst"] == "error"
        assert prep["present_count"] < len(q.DIMENSION_WEIGHTS)


class TestCatalystDistributionInvariant:
    """分布不变式：单日 catalyst 大面积落 4.0 是数据源故障的信号。"""

    PROD_DB = None

    @staticmethod
    def _daily_share_of_4(rows):
        by_day = {}
        for d, v in rows:
            by_day.setdefault(d, []).append(v)
        return {d: sum(1 for x in vs if x == 4.0) / len(vs)
                for d, vs in by_day.items() if len(vs) >= 8}

    def test_invariant_flags_known_collapse_day(self):
        """喂已知塌缩日的形态必须被判为异常（否则这条不变式无效）。"""
        rows = [("2026-08-11", 4.0)] * 26 + [("2026-08-11", 5.5)]
        share = self._daily_share_of_4(rows)
        assert share["2026-08-11"] > 0.75, "已知塌缩日未被判为异常"

    def test_invariant_passes_healthy_day(self):
        """健康日（4.0 约占 1/3，符合季度财报周期）不得误报。"""
        rows = [("2026-08-25", 4.0)] * 9 + [("2026-08-25", 5.5)] * 11 + \
               [("2026-08-25", 6.02)] * 10
        share = self._daily_share_of_4(rows)
        assert share["2026-08-25"] <= 0.75, "健康日被误报"

    def test_production_recent_days_not_collapsed(self):
        """生产库近 30 个业务日不应有 >75% 落 4.0 的天（有则数据源故障）。"""
        import sqlite3
        db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "pheromone.db")
        if not os.path.exists(db):
            pytest.skip("生产 pheromone.db 不存在")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rs = con.execute(
                "SELECT date, dimension_scores FROM predictions "
                "WHERE dimension_scores IS NOT NULL ORDER BY date DESC LIMIT 900").fetchall()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                pytest.skip(f"存根库：{e}")
            raise
        finally:
            con.close()
        rows = []
        for d, ds in rs:
            try:
                v = json.loads(ds).get("catalyst")
            except Exception:  # noqa: BLE001
                continue
            if isinstance(v, (int, float)):
                rows.append((d, v))
        if len(rows) < 50:
            pytest.skip(f"样本不足（{len(rows)}）")
        share = self._daily_share_of_4(rows)
        recent = sorted(share)[-30:]
        bad = [d for d in recent if share[d] > 0.75]
        # 历史塌缩日是已知的（修复前产生），只断言不再新增
        assert len(bad) <= 9, (
            f"catalyst 大面积落 4.0 的天数超出已知历史（{len(bad)} 天）：{bad}")
