"""关注事项通道：只进报告、永不进评分（v0.45.32）。

本文件最重要的一条是 TestNeverReachesScoring —— 前身 catalysts.json /
catalyst_refinement 正是因为直接喂 catalyst 维度（final_score 的 18.78%），
才让「腐烂」和「编造」都变成了静默改分。守住这条边界比守住解析正确更重要。
"""

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watchlist_events as we  # noqa: E402

_TABLE = """# 关注事项

| 标的 | 日期 | 事件 | 类型 | 来源 | 核实状态 |
|------|------|------|------|------|----------|
| RKLB | {future} | Neutron 首飞窗口 | product | https://example.com/a | 待验证 |
| NVDA | {past} | 已经过去的发布会 | conference | https://example.com/b | 已核实 |
| BAD  | 2026-13-45 | 日期写坏的条目 | other | https://example.com/c | 待验证 |
"""


@pytest.fixture
def events_file(tmp_path):
    def _make(days_ahead=30, days_ago=30):
        today = dt.date(2026, 8, 26)
        p = tmp_path / "watchlist_events.md"
        p.write_text(_TABLE.format(
            future=(today + dt.timedelta(days=days_ahead)).isoformat(),
            past=(today - dt.timedelta(days=days_ago)).isoformat()), encoding="utf-8")
        return str(p)
    return _make


class TestNeverReachesScoring:
    """边界守卫：本模块不得提供任何可直接用于评分的出口。"""

    def test_module_exposes_no_scoring_shaped_api(self):
        forbidden = {"catalysts_found", "get_catalysts", "score", "as_catalysts",
                     "to_catalysts", "build_catalysts"}
        public = {n for n in dir(we) if not n.startswith("_")}
        assert not (public & forbidden), (
            f"出现了评分形状的出口：{public & forbidden} —— "
            "关注事项一旦能喂评分，就退回 catalysts.json 的老路")

    def test_no_scoring_module_imports_it(self):
        """评分路径上的模块绝不能 import 本模块。

        用 AST 查真实 import，不做字符串匹配 —— 注释里提到模块名是正当的
        （chronos_bee 里就有一段说明为什么人工事件被移走了），
        字符串匹配会把文档也判成违规。
        """
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        scoring_modules = [
            os.path.join("swarm_agents", "chronos_bee.py"),
            os.path.join("swarm_agents", "queen_distiller.py"),
            os.path.join("swarm_agents", "scout_bee.py"),
            "catalyst_exit_planner.py",
        ]
        for rel in scoring_modules:
            path = os.path.join(root, rel)
            if not os.path.exists(path):
                continue
            tree = ast.parse(open(path, encoding="utf-8").read())
            imported = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    imported.update(a.name.split(".")[0] for a in n.names)
                elif isinstance(n, ast.ImportFrom) and n.module:
                    imported.add(n.module.split(".")[0])
            assert "watchlist_events" not in imported, (
                f"{rel} import 了关注事项模块 —— 人工事件又能改分了")

    def test_removed_sources_stay_removed(self):
        """catalysts.json / catalyst_refinement 不得复活。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        assert not os.path.exists(os.path.join(root, "catalysts.json")), \
            "catalysts.json 又出现了（人工前瞻日历不得回到评分路径）"
        assert not os.path.exists(os.path.join(root, "catalyst_refinement.py")), \
            "catalyst_refinement.py 又出现了"


class TestParsing:
    def test_upcoming_and_expired_classified(self, events_file):
        d = we.load_events(events_file(), today="2026-08-26")
        by = {e["ticker"]: e for e in d["events"]}
        assert by["RKLB"]["status"] == "upcoming"
        assert by["RKLB"]["days_until"] == 30
        assert by["NVDA"]["status"] == "expired"
        assert by["NVDA"]["days_until"] == -30

    def test_bad_date_is_surfaced_not_dropped(self, events_file):
        """日期写坏必须看得见 —— 静默丢弃正是前身腐烂到没人发现的机制。"""
        d = we.load_events(events_file(), today="2026-08-26")
        bad = [e for e in d["events"] if e.get("status") == "bad_date"]
        assert len(bad) == 1 and bad[0]["ticker"] == "BAD"

    def test_missing_file_is_legal_empty_not_error(self, tmp_path):
        d = we.load_events(str(tmp_path / "nope.md"))
        assert d["available"] is False and d["events"] == []


class TestAntiRot:
    def test_stale_file_flagged_in_report(self, events_file, monkeypatch):
        """喂退化：文件很久没动 → 报告必须出现陈旧警告。"""
        p = events_file()
        old = dt.datetime(2026, 1, 1).timestamp()
        os.utime(p, (old, old))
        d = we.load_events(p, today="2026-08-26")
        assert d["is_stale"] is True
        md = "\n".join(we.format_for_report(d))
        assert "未更新" in md and "⚠️" in md, "陈旧文件没有在报告里报警"

    def test_fresh_file_not_flagged(self, events_file):
        d = we.load_events(events_file(), today="2026-08-26")
        assert d["is_stale"] is False
        assert "已超过" not in "\n".join(we.format_for_report(d))

    def test_report_states_it_does_not_affect_scoring(self, events_file):
        """报告里必须写明「不参与评分」—— 这是给读者的边界声明。"""
        md = "\n".join(we.format_for_report(we.load_events(events_file(), today="2026-08-26")))
        assert "不参与评分" in md or "不影响任何评分" in md

    def test_expired_entries_stay_visible(self, events_file):
        md = "\n".join(we.format_for_report(we.load_events(events_file(), today="2026-08-26")))
        assert "已过期" in md, "过期条目被静默隐藏"


class TestShippedFileIsValid:
    def test_repo_file_parses_and_every_row_has_source_and_status(self):
        d = we.load_events()
        if not d["available"]:
            pytest.skip("仓库内无 watchlist_events.md")
        for e in d["events"]:
            assert e["source"], f"{e['ticker']} 缺来源 URL（CLAUDE.md 不编数据）"
            assert e["verified"] in ("已核实", "待验证"), \
                f"{e['ticker']} 核实状态非法：{e['verified']!r}"
