"""
report_deployer - 报告部署与通知模块

从 AlphaHiveDailyReporter 提取的部署和通知方法。
每个函数接收 reporter 实例（原 self）作为第一个参数。
"""

from typing import Dict, List, Optional, Tuple
from datetime import datetime
from hive_logger import get_logger

_log = get_logger("report_deployer")


def ghpages_tree_delta(repo: str, tree: str, parent: Optional[str]) -> Tuple[bool, int]:
    """比较新 tree 与 parent 提交的 tree，返回 (是否有变更, 变更文件数)。

    为什么需要这个：部署走的是 `git commit-tree` 这类**管道命令**，它不做
    `git commit` 的「无变更则拒绝提交」检查——tree 与父提交完全相同时照样
    生成一个 commit。实测 2026-08-15 连续三条
    `Deploy: ML reports 2026-08-15 (12 tickers)` 全部是 0 文件的空提交，
    message 里的数字是**声称值**（successful_count），与 tree 实际变更无关。

    变更文件数返回 -1 表示「无法判定」（无父提交，或 git 调用失败），
    调用方此时不应把它写进 commit message 冒充实测值。
    """
    import subprocess
    if not parent:
        return True, -1          # 首次提交，无从比对
    try:
        parent_tree = subprocess.check_output(
            ["git", "rev-parse", f"{parent}^{{tree}}"],
            cwd=repo, stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.CalledProcessError, OSError):
        return True, -1
    if parent_tree == tree:
        return False, 0
    try:
        out = subprocess.check_output(
            ["git", "diff-tree", "-r", "--name-only", parent_tree, tree],
            cwd=repo, stderr=subprocess.DEVNULL,
        ).decode()
        return True, len([ln for ln in out.splitlines() if ln.strip()])
    except (subprocess.CalledProcessError, OSError):
        return True, -1


def verify_cdn_deployment(reporter, repo: str,
                           max_wait: int = 180, poll_interval: int = 15) -> bool:
    """Push 成功后轮询 CDN，验证 dashboard-data.json 已更新。

    纯 advisory — 超时只记 WARNING，不回滚/阻塞。
    """
    import json as _json_v
    import time as _time_v
    import urllib.request

    try:
        import os as _os_v
        dj_path = _os_v.path.join(repo, "dashboard-data.json")
        with open(dj_path, encoding="utf-8") as _f:
            expected_ts = _json_v.load(_f).get("_generated_at", "")
        if not expected_ts:
            _log.debug("dashboard-data.json 无 _generated_at，跳过 CDN 验证")
            return True
    except (OSError, _json_v.JSONDecodeError) as e:
        # v0.45.54：读不到本地文件 → 返回 True =「CDN 部署已验证」，
        # 与真正轮询成功的返回值完全同形。改为 None ＝「未验证」。
        _log.warning("读取本地 dashboard-data.json 失败，**CDN 验证未执行**"
                     "（不报告为已验证）: %s", e)
        return None

    # ── 快速连通性探测：如果网络出口无法访问 github.io，直接跳过，避免浪费 3 分钟 ──
    _deploy_host = reporter._DEPLOY_BASE_URL.split("/")[2]  # e.g. "wangmingjie36-creator.github.io"
    try:
        import socket as _sock
        _sock.setdefaulttimeout(5)
        _sock.getaddrinfo(_deploy_host, 443)
    except OSError:
        _log.info(
            "CDN 验证跳过：无法解析 %s（沙箱网络限制），gh-pages 已推送成功",
            _deploy_host,
        )
        return True

    _log.info("验证 CDN 部署... (期望: %s, 最长等待 %ds)", expected_ts, max_wait)
    start = _time_v.monotonic()
    attempt = 0
    while _time_v.monotonic() - start < max_wait:
        attempt += 1
        try:
            req_url = f"{reporter._DEPLOY_BASE_URL}/dashboard-data.json?_verify={int(_time_v.time())}"
            req = urllib.request.Request(req_url, headers={
                "Cache-Control": "no-cache", "Pragma": "no-cache",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json_v.loads(resp.read().decode("utf-8"))
                live_ts = data.get("_generated_at", "")
                if live_ts == expected_ts:
                    _log.info(
                        "✅ CDN 验证通过 (attempt %d, %.0fs)",
                        attempt, _time_v.monotonic() - start,
                    )
                    return True
                _log.debug(
                    "CDN 仍旧: live='%s' expected='%s' (attempt %d)",
                    live_ts, expected_ts, attempt,
                )
        except Exception as e:
            _log.debug("CDN 验证请求失败: %s (attempt %d)", e, attempt)
        _time_v.sleep(poll_interval)

    _log.warning(
        "⚠️ CDN 验证超时 (%ds): live 数据未更新到 '%s'，可能需要手动刷新",
        max_wait, expected_ts,
    )
    return False


def deploy_static_to_ghpages(reporter):
    """用 git plumbing 构建仅含静态文件的 gh-pages 提交并推送。"""
    import subprocess
    import os
    import resource as _resource
    # 预防 Too many open files：确保 fd 上限至少 2048
    try:
        _soft, _hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
        if _soft < 2048:
            _resource.setrlimit(_resource.RLIMIT_NOFILE, (min(2048, _hard), _hard))
    except (ValueError, OSError):
        pass
    repo = reporter.agent_helper.git.repo_path or "."
    idx = os.path.join(repo, ".git", "gh-pages-index")
    if os.path.exists(idx):
        os.remove(idx)
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = idx
    # ── D2: 部署文件白名单 ──
    import re as _re_deploy
    _CORE_FILES = {
        "index.html", "dashboard-data.json", "manifest.json",
        "sw.js", "rss.xml", ".nojekyll",
        "chart.umd.min.js",  # v0.41.0: Chart.js 自托管
    }
    # v0.45.15: 原 `\w+` 不含连字符 ⇒ alpha-hive-BRK-B-ml-enhanced-*.html 永远
    # 匹配不上，报告生成了却从不部署，而 index.html 照常链接它 → 线上 404。
    # 与 v0.45.2（Agent 校验层）、v0.45.8（CBOE 取数层）是同一个类份额连字符问题。
    # 收尾 `-ml-enhanced-\d{4}-\d{2}-\d{2}\.html$` 已锁死范围，放宽字符集安全。
    _ml_pat = _re_deploy.compile(
        r"^alpha-hive-[\w.-]+-ml-enhanced-\d{4}-\d{2}-\d{2}\.html$"
    )
    try:
        from is_trading_day import filename_is_nontrading_day as _fnt_dep
    except Exception:
        def _fnt_dep(_n):
            return False  # fail-safe：导入失败则不过滤，不误删
    files = []
    for f in os.listdir(repo):
        # 非交易日（周末/假日）幽灵报告不部署（_CORE 文件无日期，永不被过滤）
        if f not in _CORE_FILES and _fnt_dep(f):
            continue
        if f in _CORE_FILES:
            files.append(f)
        elif _ml_pat.match(f):
            # 所有 ML 增强报告（不再限制天数，index.html 历史板块需要全部文件）
            files.append(f)
        elif f.startswith("alpha-hive-daily-") and f.endswith((".json", ".md")):
            # 当日+历史 daily 报告（JSON + MD）
            files.append(f)
    if not files:
        _log.warning("无静态文件可部署")
        return
    # 批量写入 blob + index（逐个 hash-object，但用 stdin 批量 update-index）
    cache_entries = []
    for f in sorted(files):
        try:
            blob = subprocess.check_output(
                ["git", "hash-object", "-w", f], cwd=repo
            ).decode().strip()
            cache_entries.append(f"100644 {blob}\t{f}")
        except (subprocess.CalledProcessError, OSError) as _e_blob:
            _log.warning("hash-object 失败 (%s): %s", f, _e_blob)
    if cache_entries:
        # 用 --index-info 批量更新 index（一次 subprocess 代替 N 次）
        _idx_input = "\n".join(cache_entries) + "\n"
        subprocess.run(
            ["git", "update-index", "--add", "--index-info"],
            input=_idx_input, env=env, cwd=repo, check=True, text=True
        )
    tree = subprocess.check_output(
        ["git", "write-tree"], env=env, cwd=repo
    ).decode().strip()
    # 获取 gh-pages 父提交（若存在）
    parent_args = []
    parent = None
    try:
        parent = subprocess.check_output(
            ["git", "rev-parse", "gh-pages"], cwd=repo, stderr=subprocess.DEVNULL
        ).decode().strip()
        parent_args = ["-p", parent]
    except subprocess.CalledProcessError:
        pass
    # v0.45.2: 空提交闸。tree 与父提交相同就不再造 commit，
    # 否则 gh-pages 会积累一串「message 声称 N 份报告、实际 0 文件」的假记录。
    _has_change, _n_changed = ghpages_tree_delta(repo, tree, parent)
    if not _has_change:
        _log.error(
            "🚨 gh-pages 无变更：新 tree 与父提交 %s 完全相同，跳过 commit"
            "（本次待部署 %d 个文件）。若本应有新报告，说明报告文件根本没重新生成。",
            (parent or "")[:7], len(files),
        )
    else:
        _msg = f"Deploy: Alpha Hive static {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        if _n_changed >= 0:
            _msg += f" [{_n_changed} files changed]"
        commit = subprocess.check_output(
            ["git", "commit-tree", tree] + parent_args + ["-m", _msg],
            cwd=repo
        ).decode().strip()
        subprocess.run(
            ["git", "update-ref", "refs/heads/gh-pages", commit],
            cwd=repo, check=True
        )
    # ── D3: Push 重试（指数退避，最多 3 次重试） ──
    import time as _time_push
    _PUSH_MAX_RETRIES = 3
    _push_ok = False
    for _push_attempt in range(_PUSH_MAX_RETRIES + 1):
        r = subprocess.run(
            ["git", "push", "origin", "gh-pages", "--force"],
            cwd=repo, capture_output=True, text=True
        )
        if r.returncode == 0:
            _push_ok = True
            break
        if _push_attempt < _PUSH_MAX_RETRIES:
            _delay = min(2.0 * (2 ** _push_attempt), 16.0)
            _log.warning(
                "gh-pages push attempt %d/%d failed (%s), retrying in %.0fs",
                _push_attempt + 1, _PUSH_MAX_RETRIES + 1,
                r.stderr.strip()[:120], _delay,
            )
            _time_push.sleep(_delay)
    if os.path.exists(idx):
        os.remove(idx)
    # 修复 Bug #21：gh-pages push 成功/失败都记录到持久化 queue，
    # 防止"连续网络差时中间几天的 dashboard 永久丢失"
    _ghp_queue = os.path.join(repo, ".gh_pages_deploy_log.jsonl")
    try:
        import json as _json_q
        import datetime as _dt_q
        _status = {
            "timestamp": _dt_q.datetime.utcnow().isoformat() + "Z",
            "date_str": reporter.date_str,
            "file_count": len(files),
            "changed_files": _n_changed,   # v0.45.2: 实测值（-1=无法判定）
            "tree_unchanged": not _has_change,
            "status": "success" if _push_ok else "failed",
            "attempts": _push_attempt + 1,
            "last_error": (r.stderr or "")[:300] if not _push_ok else "",
        }
        with open(_ghp_queue, "a", encoding="utf-8") as _qf:
            _qf.write(_json_q.dumps(_status, ensure_ascii=False) + "\n")
    except Exception as _qe:
        _log.debug("gh-pages deploy log write failed: %s", _qe)

    if _push_ok:
        _log.info(
            "gh-pages 部署成功 (%d 静态文件, attempt %d/%d)",
            len(files), _push_attempt + 1, _PUSH_MAX_RETRIES + 1,
        )
        # ── D4: 部署后 CDN 验证 ──
        verify_cdn_deployment(reporter, repo)
    else:
        _log.error(
            "gh-pages push 失败 (所有 %d 次尝试用尽): %s\n"
            "→ 失败已记录到 %s，下次扫描会自动覆盖为新 commit（因为 gh-pages 是 --force push）\n"
            "→ 如需紧急修复：检查 %s 确认 pending 日，必要时手动重跑扫描",
            _PUSH_MAX_RETRIES + 1, r.stderr, _ghp_queue, _ghp_queue,
        )


#: 日报自动提交的**白名单**（v0.43.4）
#
# 只有这些路径会被定时任务自动提交。代码文件一律交给人工提交。
#
# 为什么是白名单而非黑名单：自动化系统的失败模式必须是可发现的。
# 白名单漏一项 → 该产物不进 git → 下次运行/看网站立刻发现；
# 黑名单漏一项 → 半成品代码被自动提交并推上生产 → 无人知晓。
#
# 清单来源：commit 68aad61（2026-07-30 日报）里的全部非代码文件，
# 加上 ML 报告与 analysis JSON（那次恰好没生成）。
REPORT_ARTIFACT_PATHS: List[str] = [
    # 日报正文与分享版
    "alpha-hive-daily-*.json",
    "alpha-hive-daily-*.md",
    "alpha-hive-thread-*.txt",
    # ML 增强报告与其数据快照
    "alpha-hive-*-ml-enhanced-*.html",
    "analysis-*-ml-*.json",
    # 站点资产
    "index.html",
    "dashboard-data.json",
    "rss.xml",
    "sw.js",
    # 状态与缓存
    "report_snapshots/",
    "paper_portfolio_state/",
    ".factor_cache/",
    "weight_history.jsonl",
    # v0.45.115：期权路线图三本账（v0.45.101~103 建，2026-09-04 首次落盘）。
    # 不加进来的话，扫描每天改它们、白名单每天跳过它们，于是永远挂在工作区
    # ——2026-09-04 一次 `git reset --hard` 就把这类未提交状态清掉了。
    # 这三本装的是**攒数期数据，丢了无法回溯重取**：历史 CBOE 双边报价、
    # 逐日 iv−rv 记账、已开跨式腿的成本价。与 paper_portfolio_state/ 同类。
    "hedge_state/",
    "options_paper_state/",
    "vrp_state/",
    # v0.45.134：概率记分账本。装的是**当天真正印出去的那个概率**——
    # 回溯记分能重算估计量，但重算不出「那天实际印的是什么」，
    # 丢了无法回溯重取。与 paper_portfolio_state/ 同类。
    "probability_scorecard_state/",
    # v0.45.145：ML 模型版本快照。装的是**当天真正做出预测的那个模型**——
    # 2026-09-04 模型退化成常数函数无法事后归因，就是因为 ml_model_cache.json
    # 原地覆盖、没有任何历史版本可捞出来重放。丢了无法回溯重取。
    # 与 paper_portfolio_state/ 同类。
    "ml_model_history/",
]

#: 与上表对应的匹配规则（用于"哪些被跳过"的提示）
#: ⚠️ 必须与 REPORT_ARTIFACT_PATHS 同向——只改一处会让「跳过了哪些」的提示说谎
#:    （提示说被跳过、实际被提交，或反之）。tests/test_report_deployer_whitelist.py
#:    有一条断言盯着这两处的一致性。
_ARTIFACT_PREFIXES = ("report_snapshots/", "paper_portfolio_state/", ".factor_cache/",
                      "hedge_state/", "options_paper_state/", "vrp_state/",
                      "probability_scorecard_state/", "ml_model_history/")
_ARTIFACT_GLOBS = (
    "alpha-hive-daily-*.json", "alpha-hive-daily-*.md", "alpha-hive-thread-*.txt",
    "alpha-hive-*-ml-enhanced-*.html", "analysis-*-ml-*.json",
)
_ARTIFACT_EXACT = ("index.html", "dashboard-data.json", "rss.xml", "sw.js",
                   "weight_history.jsonl")


def _is_report_artifact(path: str) -> bool:
    """该文件是否属于日报产物（即会被自动提交）"""
    import fnmatch
    p = path.strip()
    if p in _ARTIFACT_EXACT:
        return True
    if any(p.startswith(pre) for pre in _ARTIFACT_PREFIXES):
        return True
    return any(fnmatch.fnmatch(p, g) for g in _ARTIFACT_GLOBS)


def _git_modified_files(git) -> Optional[List[str]]:
    """工作区改动清单；`git status` 失败时返回 None 并打 warning。

    ⚠️ 必须把「失败」与「干净」分开。v0.45.210 前调用方写的是
    `if status.get("modified_files"): … else: log("工作目录干净")`，而
    `GitHubTool.status()` 失败时返回的是 `{"error": …}` —— 于是
    `git status` 挂了会被报成「无需提交（工作目录干净）」。
    """
    status = git.status()
    if "modified_files" not in status:
        _log.warning("git status 失败，无法判断工作区改动：%s",
                     status.get("error") or "（无错误输出）")
        return None
    return status["modified_files"]


def auto_commit_and_notify(reporter, report: Dict) -> Dict:
    """
    日报产物提交 + 推送 origin main + 同步 gh-pages —— **只对生产扫描生效**。

    生产 = 蜂群扫描，或实际用了 LLM 蒸馏。非生产扫描（`run_daily_scan`
    规则引擎路径，即不带 `--swarm` 跑 `alpha_hive_daily_report.py`）
    **不提交、不推送**，返回 `deploy_env="none"`。

    v0.45.210 撤掉了原先的「测试推送分支」（本地提交 → 临时分支推 test remote →
    `checkout main` → `reset --hard origin/main`）。取证：
      - 自 2026-03-01 `GitHubTool` 白名单引入起，`checkout`/`reset` 就被静默拒绝：
        临时分支建不出来、推送必失败、回滚从未执行，却无条件 log「本地 main 已恢复」。
        test remote 最后一次收到推送是 2026-03-01T03:47Z，白名单提交前 4.5 小时。
      - 回滚不执行 ⇒ 测试提交留在本地 main ⇒ 下一次生产推送把它一起送上 origin/main。
        白名单之后这条分支共触发 7 次（`reasoning_sessions.run_mode='daily_scan'`
        2026-03-04~03-13），origin/main 上恰好 7 个无 `swarm_metadata` 的日报提交；
        2026-03-13 的蜂群日报至今仍被规则引擎版本顶着（main 与 gh-pages 都是）。
      - 03-13 之后零触发（台账同窗口蜂群会话 172 条作正对照）。
    为什么不是往白名单加 `checkout`/`reset`：原设计的「本地 main 不被污染」
    **只能靠 `reset --hard` 实现**，而它会连带清掉工作区里未提交的代码与
    丢了无法回溯重取的账本（见 `REPORT_ARTIFACT_PATHS` 注释）。
    为什么不是 `git push test HEAD:main`：提交已经落在本地 main 上，不 reset 撤不掉。

    本函数管不到的残留：`save_report` 在本函数**之前**已把产物写进工作区。
    非生产报告不提交，但那些文件仍在；下一次生产扫描的白名单提交会把其中
    没被覆盖的一并提交。本函数把它们列进 warning 与
    `results["uncommitted_report_artifacts"]`，让它可见。
    **上游已于 v0.45.213 根治**：非蜂群扫描整条退役，`alpha_hive_daily_report.main()`
    不带 `--swarm` 在构造 reporter 之前就退出（它在扫描中途还会写概率账本，
    在 save_report 之前短路管不到，见该处注释）。CLI 与 GUI 此后都只递蜂群报告进来，
    非生产分支留作纵深防御——走到它说明有调用方递了一份不认得的报告，要出声。
    """
    _log.info("Auto-commit & Notify 启动")

    results = {}
    git = reporter.agent_helper.git

    from datetime import datetime as _dt2
    # 生产模式判定（修复 #3）：只看"实际是否使用 LLM"（distill_mode==llm_enhanced）
    # 或是否是蜂群扫描。不再用 `api_key is_available()` 这种"key 存在即生产"反模式
    _is_swarm = bool(report.get("swarm_metadata") or "蜂群" in report.get("system_status", ""))
    _using_llm = bool(
        report.get("distill_mode") == "llm_enhanced"
        or any(
            (r or {}).get("distill_mode") == "llm_enhanced"
            for r in (report.get("swarm_results") or {}).values()
        )
    )
    _deploy_production = _using_llm or _is_swarm

    if not _deploy_production:
        # 判定必须在提交**之前**：旧实现先提交再分流，非生产数据就此落在本地 main 上。
        modified = _git_modified_files(git)
        left = [f for f in (modified or []) if _is_report_artifact(f)]
        _log.warning(
            "非生产扫描（非蜂群、未用 LLM）：不提交、不推送。"
            "save_report 已写入工作区的日报产物 %s 个（不会被回滚，下一次生产扫描会把"
            "没被覆盖的一并提交）：%s",
            "未知（git status 失败）" if modified is None else len(left),
            ", ".join(left[:10]) + (" …" if len(left) > 10 else ""),
        )
        results["git_commit"] = {"success": False, "skipped": "non_production"}
        results["git_push"] = {"success": False, "skipped": "non_production", "remote": None}
        results["deploy_env"] = "none"
        results["uncommitted_report_artifacts"] = left
        results["slack_notification"] = {"skipped": "handled_by_claude_mcp"}
        _log.info("Auto-commit & Notify 完成（非生产扫描，未部署）")
        return results

    # 1. Git 提交报告（始终新 commit，不 amend，避免 GitHub Pages 部署冲突）
    timestamp = _dt2.now().strftime("%H:%M")
    today_commit_msg = f"Alpha Hive 蜂群日报 {reporter.date_str} {timestamp}"
    _log.info("Git commit... (mode: new)")
    modified = _git_modified_files(git)
    if modified is None:
        results["git_commit"] = {"success": False, "error": "git status failed"}
    elif modified:
        # v0.43.4：白名单提交。此前走 `git add -A` 全量，会把工作区里
        # 任何进行中的代码改动一并卷进"日报"提交（2026-07-30 实际发生：
        # 10 个版本的代码改动混进 commit 68aad61）。详见 AgentHelper.commit 注释。
        _skipped = [f for f in modified
                    if not _is_report_artifact(f)]
        if _skipped:
            _log.warning(
                "白名单提交：跳过 %d 个非日报产物（不会被自动提交）：%s",
                len(_skipped), ", ".join(_skipped[:10]),
            )
            print(f"   ℹ️  跳过 {len(_skipped)} 个非日报文件（需手动提交）："
                  f"{', '.join(_skipped[:5])}"
                  + (" …" if len(_skipped) > 5 else ""))

        commit_result = git.commit(
            today_commit_msg, paths=REPORT_ARTIFACT_PATHS)
        results["git_commit"] = commit_result
        results["skipped_non_artifacts"] = _skipped
        if commit_result["success"]:
            _log.info("Git commit 成功（new，白名单）")
        else:
            _log.warning("Git commit 失败：%s", commit_result.get('message'))
    else:
        _log.info("无需提交（工作目录干净）")

    # 2. Git 推送 origin main
    _log.info("Git push → [🧠 生产] (LLM=%s, Swarm=%s)", _using_llm, _is_swarm)
    r = git.run_git_cmd("git push origin main")
    # `run_git_cmd` 有两种失败形状：git 非零退出带 stdout/stderr；白名单拒绝或
    # subprocess 自己炸只有 `error`。只读 stderr 会把后一种的原因丢成空串。
    push_result = {"success": r["success"], "remote": "origin",
                   "output": r.get("stdout", "") or r.get("stderr", ""),
                   "error": r.get("error")}
    results["git_push"] = push_result
    results["deploy_env"] = "production"
    if push_result["success"]:
        _log.info("Git push 成功 → %s", push_result.get("remote"))
    else:
        _log.warning("Git push 失败：%s", push_result.get("error")
                     or push_result.get("output") or "（git 无输出）")

    # gh-pages 与 main 同步（生产模式 = LLM 或蜂群）
    try:
        deploy_static_to_ghpages(reporter)
    except Exception as e:
        _log.warning("gh-pages 部署失败: %s", e)

    # 3. Slack 通知（由 Claude Code MCP 工具推送，不用 webhook bot）
    _log.info("Slack 推送由 Claude Code 负责（用户账号）")
    results["slack_notification"] = {"skipped": "handled_by_claude_mcp"}

    _log.info("Auto-commit & Notify 完成")
    return results

