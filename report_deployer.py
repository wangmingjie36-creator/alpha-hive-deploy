"""
report_deployer - 报告部署与通知模块

从 AlphaHiveDailyReporter 提取的部署和通知方法。
每个函数接收 reporter 实例（原 self）作为第一个参数。
"""

from typing import Dict
from datetime import datetime
from hive_logger import get_logger

_log = get_logger("report_deployer")


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
        _log.debug("读取本地 dashboard-data.json 失败，跳过验证: %s", e)
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
    }
    _ml_pat = _re_deploy.compile(
        r"^alpha-hive-\w+-ml-enhanced-\d{4}-\d{2}-\d{2}\.html$"
    )
    files = []
    for f in os.listdir(repo):
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
    for f in sorted(files):
        blob = subprocess.check_output(
            ["git", "hash-object", "-w", f], cwd=repo
        ).decode().strip()
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", "100644", blob, f],
            env=env, cwd=repo, check=True
        )
    tree = subprocess.check_output(
        ["git", "write-tree"], env=env, cwd=repo
    ).decode().strip()
    # 获取 gh-pages 父提交（若存在）
    parent_args = []
    try:
        parent = subprocess.check_output(
            ["git", "rev-parse", "gh-pages"], cwd=repo, stderr=subprocess.DEVNULL
        ).decode().strip()
        parent_args = ["-p", parent]
    except subprocess.CalledProcessError:
        pass
    commit = subprocess.check_output(
        ["git", "commit-tree", tree] + parent_args +
        ["-m", f"Deploy: Alpha Hive static {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
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
    if _push_ok:
        _log.info(
            "gh-pages 部署成功 (%d 静态文件, attempt %d/%d)",
            len(files), _push_attempt + 1, _PUSH_MAX_RETRIES + 1,
        )
        # ── D4: 部署后 CDN 验证 ──
        verify_cdn_deployment(reporter, repo)
    else:
        _log.warning(
            "gh-pages push 失败 (所有 %d 次尝试用尽): %s",
            _PUSH_MAX_RETRIES + 1, r.stderr,
        )


def auto_commit_and_notify(reporter, report: Dict) -> Dict:
    """
    自动提交报告到 Git + Slack 通知（Agent Toolbox 演示）

    新功能：使用 AgentHelper 自动执行 Git 提交和通知
    """
    _log.info("Auto-commit & Notify 启动")

    results = {}

    # 1. Git 提交报告（始终新 commit，不 amend，避免 GitHub Pages 部署冲突）
    #
    # ⚠️ 架构说明：
    #   - LLM 模式：commit 所有变更 → git push origin main → 生产页面更新
    #   - 测试模式：commit 所有变更 → 仅推 test remote（临时分支）→ git reset --hard origin/main
    #              local main 完全回滚，origin/main 不受任何影响
    #   - 禁止在测试模式外手动 `git add index.html && git push origin main`，
    #     生成物（index.html / md / json / ML html）只能通过 LLM 扫描进入 origin
    from datetime import datetime as _dt2
    import llm_service as _llm_check
    # 只有 LLM 模式才推送 gh-pages（生产网站），--no-llm 蜂群仅 commit + push main
    _is_swarm = bool(report.get("swarm_metadata") or "蜂群" in report.get("system_status", ""))
    _using_llm = _llm_check.is_available()
    _deploy_production = _using_llm or _is_swarm
    _deploy_ghpages = _using_llm  # gh-pages 仅 LLM 模式更新
    timestamp = _dt2.now().strftime("%H:%M")
    today_commit_msg = f"Alpha Hive 蜂群日报 {reporter.date_str} {timestamp}"
    _log.info("Git commit... (mode: new)")
    status = reporter.agent_helper.git.status()
    if status.get("modified_files"):
        commit_result = reporter.agent_helper.git.commit(today_commit_msg)
        results["git_commit"] = commit_result
        if commit_result["success"]:
            _log.info("Git commit 成功（new）")
        else:
            _log.warning("Git commit 失败：%s", commit_result.get('message'))
    else:
        _log.info("无需提交（工作目录干净）")

    # 2. Git 推送：LLM 模式 → 生产（origin main），规则模式 → 测试（test remote）
    #    规则模式使用临时分支，不污染本地 main，推完即删除
    env_label = "🧠 生产" if _deploy_production else "🔧 测试（规则引擎）"
    _log.info("Git push → [%s] (LLM=%s, Swarm=%s)", env_label, _using_llm, _is_swarm)

    if _deploy_production:
        # 生产模式：推送 origin main
        r = reporter.agent_helper.git.run_git_cmd("git push origin main")
        push_result = {"success": r["success"], "remote": "origin",
                       "output": r.get("stdout", "") or r.get("stderr", "")}
        # gh-pages 仅在 LLM 模式下更新（避免 --no-llm 测试覆盖生产数据）
        if _deploy_ghpages:
            try:
                deploy_static_to_ghpages(reporter)
            except Exception as e:
                _log.warning("gh-pages 部署失败: %s", e)
        else:
            _log.info("跳过 gh-pages（非 LLM 模式）")
    else:
        # 测试模式：临时分支 → test remote → 删除临时分支 → 本地 main 回滚到 origin/main
        _remote_check = reporter.agent_helper.git.run_git_cmd("git remote")
        if "test" not in _remote_check.get("stdout", ""):
            _log.warning("test remote 不存在，跳过推送")
            push_result = {"success": False, "error": "test remote not configured"}
        else:
            _tmp = "_test_snapshot"
            # 从当前 HEAD 创建临时分支并推送到 test:main
            reporter.agent_helper.git.run_git_cmd(f"git branch -D {_tmp}")
            reporter.agent_helper.git.run_git_cmd(f"git checkout -b {_tmp}")
            r = reporter.agent_helper.git.run_git_cmd(f"git push test {_tmp}:main --force")
            push_result = {"success": r["success"], "remote": "test",
                           "output": r.get("stdout", "") or r.get("stderr", "")}
            # 回到 main 并删除临时分支，本地 main 恢复干净状态
            reporter.agent_helper.git.run_git_cmd("git checkout main")
            reporter.agent_helper.git.run_git_cmd(f"git branch -D {_tmp}")
            # 重置本地 main 到 origin/main，撤销测试数据对本地 main 的污染
            reporter.agent_helper.git.run_git_cmd("git fetch origin")
            reporter.agent_helper.git.run_git_cmd("git reset --hard origin/main")
            _log.info("本地 main 已恢复至 origin/main（测试数据不污染生产）")

    results["git_push"] = push_result
    results["deploy_env"] = "production" if _deploy_production else "test"
    if push_result["success"]:
        _log.info("Git push 成功 → %s", push_result.get("remote"))
    else:
        _log.warning("Git push 失败：%s", push_result.get("error") or push_result.get("output", ""))

    # 3. Slack 通知（由 Claude Code MCP 工具推送，不用 webhook bot）
    _log.info("Slack 推送由 Claude Code 负责（用户账号）")
    results["slack_notification"] = {"skipped": "handled_by_claude_mcp"}

    _log.info("Auto-commit & Notify 完成")
    return results

