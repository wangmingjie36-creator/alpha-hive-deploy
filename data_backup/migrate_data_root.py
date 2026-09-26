"""数据根迁移 · 阶段 5：把生产数据从代码检出（`~/Desktop/Alpha Hive`）搬到 `~/alpha-hive-data`。

子命令（全部幂等；除 `retire` / `unretire` 外不改旧根）：

  plan      对旧根顶层逐项分类（MOVE / SKIP），**有任何一项归不了类就失败**（默认拒绝）。
  copy      按 plan 复制：SQLite 走在线备份 API（`sqlite_readonly.db_open_uri` 选打开方式，不 cp），
            其余逐文件复制保留 mtime；目标已存在且内容不同 ⇒ 在复制任何东西之前就中止。
  verify    逐库比表行数 + `integrity_check`；其余逐文件比 sha256。清单写进 `<新根>/_migration/`。
  retire    旧根里**未被 git 跟踪**的 MOVE 项整体挪进 `<旧根>/_retired_pre_phase5/` 并去写权限；
            **被跟踪的数据原地不动**——见下「为什么被跟踪的数据不挪」。同时记一份旧根指纹基线。
  unretire  retire 的逆操作（回退：去掉 `ALPHA_HIVE_HOME` 之前先跑它）。
  check-old 比对旧根指纹与 retire 时的基线：MOVE 类名字在旧根顶层重新出现、或被跟踪的数据文件
            内容变了 ⇒ 有写入方没跟着 `ALPHA_HIVE_HOME` 走（阶段 5 验收「旧位置零写入」）。

为什么被跟踪的数据不挪
----------------------
`report_deployer.auto_commit_and_notify` 用白名单 pathspec 提交（`git add report_snapshots/` 等）。
把被跟踪的目录挪走，git 看到的是删除，下一次日报提交就会把全部账本的**删除**提交并推上公开 main——
那是阶段 6（`git rm --cached` + `.gitignore`）该做、且该有意识地做的事。所以阶段 5 让它们原地冻结：
读写方全走 `PATHS.home`，冻结副本没人读；若有人写，`check-old` 会看到内容变化。

分类规则只在本文件维护（`MOVE_*` / `SKIP_*`）。新增产物若落在旧根顶层而这里没登记，
`plan` 会失败——这是有意的：归类是语义判断，不许默认成「搬」或「不搬」。
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from data_backup.sqlite_readonly import db_open_uri, sha256_file  # noqa: E402
else:
    from .sqlite_readonly import db_open_uri, sha256_file

RETIRE_DIRNAME = "_retired_pre_phase5"
MIGRATION_DIRNAME = "_migration"

# ── 分类规则（顺序：MOVE 先于 SKIP；同名只看第一条命中）──────────────────────
#: 在线活库：走 SQLite 在线备份 API，sidecar（-wal/-shm）不单独复制
MOVE_DBS = ("pheromone.db", "metrics.db", "sentiment_baseline.db", "hive_predictions.db")
#: 目录内若有 SQLite 活库，也走在线备份（其余文件逐个复制）
DIR_EMBEDDED_DBS = {"chroma_db": ("chroma.sqlite3",)}
MOVE_DIRS = (
    # 账本 / 状态（不可重取）
    "hedge_state", "paper_portfolio_state", "paper_portfolio_state_backup", "options_paper_state",
    "vrp_state", "probability_scorecard_state", "ml_model_history", "report_snapshots",
    "self_analysis_briefs", "db_snapshots", "reports",
    # v0.45.333 卖权行权价前向账本（月度 / 周度 jsonl 分片 + 冻结检验结果 + 本地报告）。当日 CBOE 双边报价
    # 与 OI 事后拿不回来 ⇒ 不可重取。不登记的话 plan 判 UNKNOWN、copy 拒绝整次迁移；情急改成 SKIP
    # 则新代码写新根、旧根里已记的行被留下，结算与预注册检验静默少几天。
    # 守卫：tests/test_migrate_data_root.py::test_every_backup_state_dir_is_moved
    "sell_strike_state",
    # 向量库 / 日志 / 备份轮转
    "chroma_db", "logs", "db_backups",
    # 缓存：可重建，但冷缓存会引发 yfinance 限流（头号数据丢失原因）⇒ 一并带走
    "cache", "data_cache", "earnings_cache", "finviz_cache", "sec_cache", "reddit_cache",
    ".factor_cache", ".risk_cache",
)
MOVE_GLOBS = (
    ".swarm_results_*.json", "analysis-*-ml-*.json", "alpha-hive-*-ml-enhanced-*.html",
    "alpha-hive-daily-*.json", "alpha-hive-daily-*.md", "alpha-hive-thread-*.txt",
    "deep-*.html", "*_raw.json",
)
MOVE_EXACT = (
    "index.html", "dashboard-data.json", "manifest.json", "sw.js", "rss.xml",
    "weight_history.jsonl", "pheromone_fallback.jsonl", "realtime_metrics.json",
    "ml_model.json", "ml_model_cache.json", "ml_model_extended.json",
    "paper_portfolio_card.html", "param_optimization_results.json", "param_optimization_report.html",
    "watchlist_override.yaml", "watchlist_override.json",
)

SKIP_EXACT = {
    # git / 工具 / 系统
    ".git": "git 元数据", ".claude": "Claude worktree 与设置", ".github": "CI 配置（代码）",
    "__pycache__": "字节码缓存", ".pytest_cache": "pytest 缓存", ".ruff_cache": "ruff 缓存",
    ".DS_Store": "Finder 元数据", RETIRE_DIRNAME: "阶段 5 自己的旧数据暂存区",
    # 代码目录
    "tests": "代码", "swarm_agents": "代码", "templates": "代码资源", "prompts": "代码资源",
    "experiments": "代码", "data_backup": "代码", "alpha_hive_bot": "代码", "marketing": "文档",
    "对比🆚": "文档",
    # 其它项目
    "alpha-hive-web": "独立前端项目", "mcp-servers": "独立 MCP 项目",
    # 随代码发布的静态资源 / 配置（阶段 4 部署从 git_repo_root 回落取）
    ".nojekyll": "随代码发布的静态资源", "chart.umd.min.js": "随代码发布的静态资源",
    "thesis_breaks_config.json": "代码配置（market_intelligence 按代码目录读）",
    "watchlist_events.md": "人工维护配置（watchlist_events 按代码目录读）",
    "DEEP_REPORT_CSS.txt": "代码资源", "alpha-hive-tutor.skill": "代码资源",
    "alpha-hive-landing.html": "静态页（不在 gh-pages 白名单）",
    "Dockerfile": "代码", "Makefile": "代码", "Procfile": "代码", ".gitignore": "代码",
    ".dockerignore": "代码", ".env.example": "代码", "pyproject.toml": "代码", "_config.yml": "代码",
    "requirements.txt": "代码",
    # 凭据：读取方全部去 ~/ 找（config.SECRET_FILES）；仓库根这两份只是旧兜底，不进数据根
    ".alpha_hive_av_key": "凭据（不搬）", ".alpha_hive_finnhub_key": "凭据（不搬）",
    ".anthropic_api_key": "凭据（不搬）",
    # 无读者的历史遗留（全仓零数据根读者，2026-09-23 普查）
    "output": "遗留（code_generator 写 /tmp）", "backups": "空目录遗留", "design_current": "设计稿遗留",
    "polymarket_cache": "功能已删除（v0.45.315）",
    "paper_portfolio_state.bak-nanfix-20260903": "人工备份", ".gh_pages_deploy_log.jsonl": "已迁到 logs_dir（v0.45.260）",
    "all_upgraded.json": "遗留", "final_optimal.json": "遗留", "final_upgraded.json": "遗留",
    "upgraded_50k.json": "遗留", "upgraded_v2.json": "遗留", "backtest_50k_result.json": "遗留",
    "data-NVDA-2026-03-11.json": "遗留", "strategy_backtest_report.txt": "遗留",
    "ML报告_2026-03-10.html": "遗留", "AppIcon.icns": "图标", "backup_status.json": "（旧根不应有）",
}
SKIP_GLOBS = (
    ("*.py", "代码"), ("*.md", "文档"), ("*.txt", "文档"),
    ("*.bak*", "人工备份"), (".fuse_hidden*", "FUSE 残留"), ("*.backup_*", "人工备份"),
    (".checkpoint_*", "扫描断点（只在当天有用）"), (".samples-only-*.json", "样本模式残留"),
    ("bee_icon_*.png", "图标"), ("chart_*_NVDA_*.png", "遗留"),
    ("ml_model.corrupted-*", "取证副本"), ("ml_model_cache.pre-restore-*", "取证副本"),
)


def classify(name: str, is_dir: bool) -> tuple[str, str]:
    """返回 (MOVE|MOVE_DB|SKIP|UNKNOWN, 理由)。"""
    if not is_dir:
        if name in MOVE_DBS:
            return "MOVE_DB", "在线活库"
        for db in MOVE_DBS:
            if name in (db + "-wal", db + "-shm", db + "-journal"):
                return "SKIP", f"{db} 的 sidecar（在线备份已含其内容）"
        if name in MOVE_EXACT:
            return "MOVE", "数据（PATHS.home 读写）"
        for g in MOVE_GLOBS:
            if fnmatch.fnmatchcase(name, g):
                return "MOVE", f"数据（{g}）"
    elif name in MOVE_DIRS:
        return "MOVE", "数据目录"
    if name in SKIP_EXACT:
        return "SKIP", SKIP_EXACT[name]
    for g, why in SKIP_GLOBS:
        if fnmatch.fnmatchcase(name, g):
            return "SKIP", why
    return "UNKNOWN", "未登记"


def plan(old: Path) -> dict:
    rows = []
    for name in sorted(os.listdir(old)):
        p = old / name
        kind, why = classify(name, p.is_dir() and not p.is_symlink())
        rows.append({"name": name, "kind": kind, "why": why, "is_dir": p.is_dir()})
    unknown = [r["name"] for r in rows if r["kind"] == "UNKNOWN"]
    return {"old": str(old), "rows": rows, "unknown": unknown}


# ── 复制 ──────────────────────────────────────────────────────────────────
def _iter_files(root: Path, rel: Path):
    """rel（相对旧根）下的全部普通文件，相对旧根的路径。符号链接不跟随、不复制（记下来）。"""
    p = root / rel
    if p.is_file():
        yield rel
        return
    for dp, dns, fns in os.walk(p):
        dns.sort()
        for fn in sorted(fns):
            yield Path(dp).relative_to(root) / fn


def _is_embedded_db(rel: Path) -> bool:
    top = rel.parts[0]
    return len(rel.parts) >= 2 and top in DIR_EMBEDDED_DBS and rel.name in DIR_EMBEDDED_DBS[top]


def _embedded_db_sidecar(rel: Path) -> bool:
    top = rel.parts[0]
    if top not in DIR_EMBEDDED_DBS:
        return False
    return any(rel.name == db + sfx for db in DIR_EMBEDDED_DBS[top] for sfx in ("-wal", "-shm", "-journal"))


def _file_items(old: Path, pl: dict):
    """(相对路径) 逐个列出 plan 里 MOVE 项的普通文件（不含内嵌库及其 sidecar）。"""
    for r in pl["rows"]:
        if r["kind"] != "MOVE":
            continue
        for rel in _iter_files(old, Path(r["name"])):
            if (old / rel).is_symlink():
                continue
            if _is_embedded_db(rel) or _embedded_db_sidecar(rel):
                continue
            yield rel


def _db_items(old: Path, pl: dict):
    for r in pl["rows"]:
        if r["kind"] == "MOVE_DB":
            yield Path(r["name"])
        elif r["kind"] == "MOVE" and r["name"] in DIR_EMBEDDED_DBS:
            for db in DIR_EMBEDDED_DBS[r["name"]]:
                if (old / r["name"] / db).exists():
                    yield Path(r["name"]) / db


def _table_counts(conn: sqlite3.Connection) -> dict:
    names = [x[0] for x in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in names}


def backup_db(src: Path, dst: Path) -> dict:
    """在线备份 API；读事务内前后行数一致才算数。目标先写临时名、成功再原子改名。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".migrating")
    if tmp.exists():
        tmp.unlink()
    uri, how = db_open_uri(src)
    s = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        s.execute("BEGIN")
        before = _table_counts(s)
        d = sqlite3.connect(str(tmp), isolation_level=None)
        try:
            s.backup(d)
            after_src = _table_counts(s)
            s.execute("COMMIT")
            journal = d.execute("PRAGMA journal_mode").fetchone()[0]
            integrity = [x[0] for x in d.execute("PRAGMA integrity_check").fetchall()]
            got = _table_counts(d)
        finally:
            d.close()
    finally:
        s.close()
    if before != after_src:
        raise RuntimeError(f"{src}：备份期间行数变了（有并发写者？停扫了吗？）")
    if integrity != ["ok"]:
        raise RuntimeError(f"{tmp}：integrity_check = {integrity[:5]}")
    if got != before:
        raise RuntimeError(f"{src} → {tmp}：行数不一致")
    os.replace(tmp, dst)
    return {"open": how, "journal_mode": journal, "tables": len(got), "rows": sum(got.values())}


def conflicts(old: Path, new: Path, pl: dict) -> list[str]:
    """目标已存在且内容不同的文件（DB 已存在也算冲突）。"""
    out = []
    for rel in _db_items(old, pl):
        if (new / rel).exists():
            out.append(f"{rel}（库已存在）")
    for rel in _file_items(old, pl):
        dst = new / rel
        if dst.exists() and (not dst.is_file() or sha256_file(dst) != sha256_file(old / rel)):
            out.append(str(rel))
    return out


def copy(old: Path, new: Path, pl: dict) -> dict:
    if pl["unknown"]:
        raise RuntimeError(f"plan 有未登记项，拒绝复制：{pl['unknown']}")
    bad = conflicts(old, new, pl)
    if bad:
        raise RuntimeError(f"目标已存在且内容不同（一个都没复制）：{bad[:20]}"
                           + (f" …共 {len(bad)} 项" if len(bad) > 20 else ""))
    dbs = {str(rel): backup_db(old / rel, new / rel) for rel in _db_items(old, pl)}
    n = 0
    for rel in _file_items(old, pl):
        dst = new / rel
        if dst.exists():
            continue  # conflicts() 已证明内容相同
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old / rel, dst)
        n += 1
    return {"dbs": dbs, "files_copied": n}


def verify(old: Path, new: Path, pl: dict) -> dict:
    problems, db_report = [], {}
    for rel in _db_items(old, pl):
        o, n = old / rel, new / rel
        if not n.exists():
            problems.append(f"缺库 {rel}")
            continue
        co = sqlite3.connect(db_open_uri(o)[0], uri=True)
        cn = sqlite3.connect(db_open_uri(n)[0], uri=True)
        try:
            a, b = _table_counts(co), _table_counts(cn)
            ok = [x[0] for x in cn.execute("PRAGMA integrity_check").fetchall()] == ["ok"]
        finally:
            co.close()
            cn.close()
        db_report[str(rel)] = {"tables": len(a), "rows_old": sum(a.values()), "rows_new": sum(b.values()),
                               "match": a == b, "integrity_ok": ok}
        if a != b:
            diff = {t: (a.get(t), b.get(t)) for t in set(a) | set(b) if a.get(t) != b.get(t)}
            problems.append(f"{rel} 行数不一致：{diff}")
        if not ok:
            problems.append(f"{rel} integrity_check 不是 ok")
    n_files, total = 0, 0
    for rel in _file_items(old, pl):
        n_files += 1
        o, n = old / rel, new / rel
        if not n.is_file():
            problems.append(f"缺文件 {rel}")
            continue
        if sha256_file(o) != sha256_file(n):
            problems.append(f"sha256 不同 {rel}")
        total += o.stat().st_size
    return {"ok": not problems, "problems": problems, "dbs": db_report,
            "files_checked": n_files, "bytes_checked": total}


# ── 旧根处置 ────────────────────────────────────────────────────────────────
def _git_tracked_tops(old: Path) -> set[str]:
    r = subprocess.run(["git", "-C", str(old), "ls-files", "-z"], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git ls-files 失败（rc={r.returncode}）：{r.stderr.decode()[:200]}")
    return {f.split("/", 1)[0] for f in r.stdout.decode().split("\0") if f}


def _chmod_readonly(p: Path) -> None:
    targets = [p] if p.is_file() else [Path(dp) / x for dp, dns, fns in os.walk(p) for x in fns + dns] + [p]
    for t in targets:
        if t.is_symlink():
            continue
        m = t.stat().st_mode
        t.chmod(m & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _chmod_writable(p: Path) -> None:
    targets = [p] + ([Path(dp) / x for dp, dns, fns in os.walk(p) for x in fns + dns] if p.is_dir() else [])
    for t in targets:
        if not t.is_symlink():
            t.chmod(t.stat().st_mode | stat.S_IWUSR)


def fingerprint_tracked_data(old: Path, pl: dict, tracked_tops: set[str]) -> dict:
    """被跟踪（原地冻结）的 MOVE 项逐文件 sha256。"""
    fp = {}
    for r in pl["rows"]:
        if r["kind"] in ("MOVE", "MOVE_DB") and r["name"] in tracked_tops:
            for rel in _iter_files(old, Path(r["name"])):
                if (old / rel).is_file() and not (old / rel).is_symlink():
                    fp[str(rel)] = sha256_file(old / rel)
    return fp


def retire(old: Path, new: Path, pl: dict) -> dict:
    tracked = _git_tracked_tops(old)
    hold = old / RETIRE_DIRNAME
    hold.mkdir(exist_ok=True)
    moved, frozen = [], []
    names = []
    for r in pl["rows"]:
        if r["kind"] not in ("MOVE", "MOVE_DB"):
            continue
        names.append(r["name"])
        if r["kind"] == "MOVE_DB":
            names += [r["name"] + s for s in ("-wal", "-shm") if (old / (r["name"] + s)).exists()]
    for name in names:
        if name in tracked:
            frozen.append(name)
            continue
        src = old / name
        if not src.exists():
            continue
        dst = hold / name
        if dst.exists():
            raise RuntimeError(f"暂存区已有 {name}，拒绝覆盖（上一次 retire 没清理？）")
        os.rename(src, dst)
        moved.append(name)
    record = {"retired_at": dt.datetime.now().isoformat(timespec="seconds"), "moved": moved,
              "frozen_tracked": frozen,
              "tracked_fingerprint": fingerprint_tracked_data(old, pl, tracked)}
    (hold / "RETIRE_RECORD.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    _chmod_readonly(hold)
    return {"moved": len(moved), "frozen_tracked": frozen, "hold": str(hold)}


def unretire(old: Path) -> dict:
    hold = old / RETIRE_DIRNAME
    rec_path = hold / "RETIRE_RECORD.json"
    if not rec_path.exists():
        raise RuntimeError(f"没有 {rec_path}，无从回退")
    _chmod_writable(hold)
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    back = []
    for name in rec["moved"]:
        if (old / name).exists():
            raise RuntimeError(f"旧根已有 {name}（retire 之后有人写回来了？），拒绝覆盖——先人工核对")
    for name in rec["moved"]:
        os.rename(hold / name, old / name)
        back.append(name)
    rec_path.rename(hold / f"RETIRE_RECORD.undone-{dt.datetime.now():%Y%m%d_%H%M%S}.json")
    return {"restored": len(back)}


def check_old(old: Path) -> dict:
    """旧位置零写入：MOVE 类名字不许重新出现；被跟踪的冻结数据内容不许变。"""
    rec = json.loads((old / RETIRE_DIRNAME / "RETIRE_RECORD.json").read_text(encoding="utf-8"))
    pl = plan(old)
    reappeared = [r["name"] for r in pl["rows"] if r["kind"] in ("MOVE", "MOVE_DB")
                  and r["name"] not in rec["frozen_tracked"]]
    changed = []
    for rel, sha in rec["tracked_fingerprint"].items():
        p = old / rel
        if not p.is_file() or sha256_file(p) != sha:
            changed.append(rel)
    now_tracked = fingerprint_tracked_data(old, pl, set(rec["frozen_tracked"]))
    added = sorted(set(now_tracked) - set(rec["tracked_fingerprint"]))
    return {"ok": not (reappeared or changed or added or pl["unknown"]), "reappeared": reappeared,
            "tracked_changed": changed, "tracked_added": added, "unknown": pl["unknown"]}


# ── CLI ─────────────────────────────────────────────────────────────────────
def _write_report(new: Path, name: str, obj: dict) -> Path:
    d = new / MIGRATION_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["plan", "copy", "verify", "retire", "unretire", "check-old"])
    ap.add_argument("--old", required=True, type=Path, help="旧根（代码检出）")
    ap.add_argument("--new", type=Path, help="新数据根")
    a = ap.parse_args(argv)
    old = a.old.expanduser().resolve()
    new = a.new.expanduser().resolve() if a.new else None
    if a.cmd not in ("unretire", "check-old") and new is None:
        ap.error("--new 必填")

    if a.cmd == "unretire":
        print(json.dumps(unretire(old), ensure_ascii=False, indent=1))
        return 0
    if a.cmd == "check-old":
        res = check_old(old)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0 if res["ok"] else 1

    pl = plan(old)
    if a.cmd == "plan":
        for r in pl["rows"]:
            print(f"{r['kind']:8s} {r['name']}{'/' if r['is_dir'] else ''}  — {r['why']}")
        print(f"\n未登记：{pl['unknown'] or '无'}")
        return 1 if pl["unknown"] else 0
    if a.cmd == "copy":
        res = copy(old, new, pl)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        print("报告：", _write_report(new, "copy", {"plan": pl, **res}))
        return 0
    if a.cmd == "verify":
        res = verify(old, new, pl)
        print(json.dumps({k: v for k, v in res.items() if k != "problems"}, ensure_ascii=False, indent=1))
        for p in res["problems"][:50]:
            print("❌", p)
        print("报告：", _write_report(new, "verify", res))
        return 0 if res["ok"] else 1
    if a.cmd == "retire":
        v = verify(old, new, pl)
        if not v["ok"]:
            print("verify 未通过，拒绝 retire：", v["problems"][:20])
            return 1
        res = retire(old, new, pl)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        print("报告：", _write_report(new, "retire", res))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
