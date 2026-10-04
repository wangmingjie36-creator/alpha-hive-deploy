"""`GitHubTool.commit(paths=...)` / `status()` 测试用的「日报产物」路径表（测试夹具数据，不是生产配置）。

v0.45.402 前这份表是 `report_deployer.REPORT_ARTIFACT_PATHS`（日报自动提交的白名单）；阶段 6 随日报提交 / 推送链
一起从生产代码里退役。`GitHubTool.commit` 的 pathspec 行为（逐条 add、未匹配容忍、锁错误不许冒充未匹配、
iCloud 副本名）仍有测试价值，所以保留一份**冻结的**夹具表，别再把它当「现行白名单」去核对。
"""
import fnmatch

ARTIFACT_PATHS = [
    "alpha-hive-daily-*.json", "alpha-hive-daily-*.md", "alpha-hive-thread-*.txt",
    "alpha-hive-*-ml-enhanced-*.html", "analysis-*-ml-*.json",
    "index.html", "dashboard-data.json", "rss.xml", "sw.js",
    "report_snapshots/", "paper_portfolio_state/", ".factor_cache/", "weight_history.jsonl",
    "hedge_state/", "options_paper_state/", "vrp_state/", "probability_scorecard_state/", "ml_model_history/",
]

_PREFIXES = tuple(p for p in ARTIFACT_PATHS if p.endswith("/"))
_GLOBS = tuple(p for p in ARTIFACT_PATHS if "*" in p)
_EXACT = tuple(p for p in ARTIFACT_PATHS if not p.endswith("/") and "*" not in p)


def is_artifact(path: str) -> bool:
    p = path.strip()
    return p in _EXACT or p.startswith(_PREFIXES) or any(fnmatch.fnmatch(p, g) for g in _GLOBS)
