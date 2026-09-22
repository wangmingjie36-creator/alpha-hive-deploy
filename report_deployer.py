"""
report_deployer - 报告部署与通知模块

从 AlphaHiveDailyReporter 提取的部署和通知方法。
每个函数接收 reporter 实例（原 self）作为第一个参数。
"""

from typing import Dict, List, Optional, Tuple
from datetime import datetime
from hive_logger import get_logger
import production_sync

_log = get_logger("report_deployer")


#: 随代码发布、不由任何 Python 代码写进 data_root 的静态资源——物理上只活在
#: git 仓库工作区（`PATHS.git_repo_root`），跟着代码仓库走，不跟数据根走。
#:
#: 数据根迁移阶段 4 二次检查发现的缺陷（v0.45.305）：`deploy_static_to_ghpages`
#: 与 `generate_ml_report._sync_ghpages` 此前各写了一份内容相同的"部署文件
#: 白名单"，都用 `os.listdir(data_root)` 选文件——今天 data_root 与
#: git_repo_root 恰好同目录，看不出问题；阶段 5 把 `ALPHA_HIVE_HOME` 改指
#: `~/alpha-hive-data` 后，`.nojekyll`（仓库根的部署标记文件）与
#: `chart.umd.min.js`（随 Chart.js 版本升级由人工提交更新，v0.41.0 起自托管）
#: 永远不会出现在 data_root 里——gh-pages 每次都整棵重建，于是第一次部署
#: 就会把它们从线上删掉：Chart.js 脚本 404、Service Worker `cache.addAll`
#: 因清单里的文件取不到而失败。
#:
#: 两条部署路径改共用这一份定义 + `resolve_code_shipped_asset_sources`，
#: 不再各写各的白名单（此前改一处漏一处）。
CODE_SHIPPED_STATIC_ASSETS = frozenset({".nojekyll", "chart.umd.min.js"})


def resolve_code_shipped_asset_sources(data_root: str, repo: str,
                                        already_covered) -> Dict[str, str]:
    """`CODE_SHIPPED_STATIC_ASSETS` 里还没被 `already_covered`（调用方已经从
    `os.listdir(data_root)` 扫到、判定走 data_root 的文件名集合）覆盖的那些，
    退回 `repo`（git 仓库根）找。

    返回 `{文件名: 该从哪个目录读字节}`，**只含确实能找到的**——data_root 优先
    （万一将来某天改成运行时生成，数据根有就不必读仓库），两处都没有的
    不出现在返回值里，调用方据此判断该不该 warning。
    """
    import os
    sources: Dict[str, str] = {}
    for asset in CODE_SHIPPED_STATIC_ASSETS:
        if asset in already_covered:
            continue
        if os.path.exists(os.path.join(repo, asset)):
            sources[asset] = repo
    return sources


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


def resolve_gh_pages_parent(repo: str) -> Tuple[Optional[str], bool]:
    """gh-pages 新提交该接在谁后面。

    ⚠️ 2026-09-11 实测过的 bug（见 auto-memory `alpha-hive-ops-info.md`）：
    旧实现用**本地** `gh-pages` ref 当父提交（`git rev-parse gh-pages`），
    再 `--force` 推。本地 ref 不会因为别的 session/进程推过 gh-pages 而
    自动更新——用它当父提交、再强推，等于把远端在这期间新增的提交从
    "有 ref 指着"变成"没有任何 ref 指着"，一次 force-push 就静默把它们
    挤成不可达对象（早晚被 gc），且没有任何东西会红。

    修法：**先 fetch，用 fetch 到的 `origin/gh-pages` 当父**，不用本地 ref。
    这样新提交的第一父永远是 push 前一刻的远端真头，force-push（本函数配
    `commit_and_push_gh_pages` 之后其实改用了**非** force 推送，见那里）
    不会再把远端历史挤成孤儿。

    ⚠️ 2026-09-21 二次检查发现的第二个 bug：`git fetch origin gh-pages`
    **不带显式目标 refspec**——它是否会更新本地的 `origin/gh-pages` ref，
    取决于这个 checkout 的 `remote.origin.fetch` 配置是否覆盖 `gh-pages`。
    `--single-branch --branch main` 克隆（阶段 8 的新 clone 就是这样）的
    默认 refspec 只有 `+refs/heads/main:refs/remotes/origin/main`：跑
    `git fetch origin gh-pages` 照样以 exit 0 收场（内容写进了 `FETCH_HEAD`），
    但**不会**创建/更新 `refs/remotes/origin/gh-pages`——随后的
    `rev-parse origin/gh-pages` 必然失败。旧代码把「fetch 成功 + rev-parse
    失败」直接读成"远端还没有这个分支：真·首次部署"，这个判断从一开始就
    错了：一个真正不存在的远端分支会让 `git fetch origin <branch>` 本身
    以非零退出失败（"couldn't find remote ref"），走不到这个分支——这里
    实际触发的从来只是"我们的 fetch 没把它拉过来"，不是"它不存在"。放行
    的后果是首次部署被判定为已验证，随后 `commit-tree` 建了个无父提交，
    非 force push 必然因非快进被拒，4 次重试全部失败，gh-pages 永久停更，
    且 `parent_verified: true` 全程是假的。

    修法：① fetch 显式点名目标 refspec
    `+refs/heads/gh-pages:refs/remotes/origin/gh-pages`——不管这个 checkout
    的默认 fetch 配置是什么，成功就必然更新了 `origin/gh-pages`；远端真没有
    这个分支时，`git fetch` 点名一个不存在的远端 ref 本身就会失败（非零退出），
    不会再落进"成功但拿不到"的暧昧地带。② fetch 失败时用只读的
    `git ls-remote --heads origin gh-pages` 正面核实——只有它明确说远端没有
    这个分支（成功且输出为空）才判定"真·首次部署"；ls-remote 本身失败
    （网络/权限）或说分支其实存在，一律退回未经校验的本地 ref 兜底，不冒充
    已验证。

    返回 `(parent_sha, verified)`：
      verified=True   要么 fetch 成功拿到了 `origin/gh-pages` 的真头；要么
                       `ls-remote` 正面核实远端确实没有这个分支（真·首次部署，
                       `parent_sha=None`）。
      verified=False  两者都做不到（网络/权限问题）。`parent_sha` 退回本地
                       `gh-pages` ref 尽力而为——**不可信**，调用方必须把这次
                       「未经校验」喊出来，不能悄悄当成正常路径处理。
    """
    import subprocess
    fetch = subprocess.run(
        ["git", "fetch", "origin", "+refs/heads/gh-pages:refs/remotes/origin/gh-pages"],
        cwd=repo, capture_output=True, text=True,
    )
    if fetch.returncode == 0:
        try:
            remote = subprocess.check_output(
                ["git", "rev-parse", "origin/gh-pages"],
                cwd=repo, stderr=subprocess.DEVNULL,
            ).decode().strip()
            return remote, True
        except subprocess.CalledProcessError:
            # 显式目标 refspec 的 fetch 已经成功——理论上这里不会走到
            # （成功就必然更新了 origin/gh-pages）。万一真出现，别冒充"已校验"，
            # 落到下面的 ls-remote 核实分支。
            pass
    ls = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", "gh-pages"],
        cwd=repo, capture_output=True, text=True,
    )
    if ls.returncode == 0 and not ls.stdout.strip():
        return None, True   # ls-remote 正面核实：远端确实没有 gh-pages 分支，真·首次部署
    try:
        local = subprocess.check_output(
            ["git", "rev-parse", "gh-pages"], cwd=repo, stderr=subprocess.DEVNULL,
        ).decode().strip()
        return local, False
    except subprocess.CalledProcessError:
        return None, False


def commit_and_push_gh_pages(repo: str, tree: str, message_fn, max_attempts: int = 4) -> Dict:
    """把 `tree` 提交到 gh-pages 并推送——每次重试都重新 fetch 校验父提交。

    与旧实现（"建一次 commit、force-push 重试 3 次"）的关键差异：
      1. 父提交**每次重试都重新解析**（`resolve_gh_pages_parent`），不是只在
         最开始取一次——重试之间远端可能又被推过。
      2. **不用 `--force`**：既然父提交就是 push 前一刻的远端真头，普通推送
         天然是快进；如果 push 与 fetch 之间又有人推了（竞态窗口缩小到毫秒级），
         git 自己会因为非快进拒绝——这正是我们想要的"冲突探测"，不需要
         自己实现 diff/合并逻辑去判断"冲突"（gh-pages 树本来就是每次从
         数据根整棵重建，不存在逐文件合并这回事，真正要防的只是"别把
         没同步到的远端提交挤成不可达对象"）。git 拒绝后重试循环会重新
         fetch、拿到最新父提交、重新 commit-tree、再推——不是重复同一次
         必然还会被拒的推送。
      3. tree 与刚 fetch 到的父提交的 tree 完全相同 ⇒ 远端已经是我们要发布
         的状态，直接判成功、不新建提交也不推送（比"仍然尝试推一次"更准确，
         也避免了对一个不存在实际变化的 ref 做推送）。

    返回 dict：{success, commit, parent, parent_verified, tree_unchanged,
                n_changed, attempts, last_error}。
    """
    import subprocess
    import time as _t
    result: Dict = {
        "success": False, "commit": None, "parent": None, "parent_verified": None,
        "tree_unchanged": None, "n_changed": None, "attempts": 0, "last_error": None,
    }
    for attempt in range(1, max_attempts + 1):
        result["attempts"] = attempt
        parent, verified = resolve_gh_pages_parent(repo)
        result["parent"], result["parent_verified"] = parent, verified
        if not verified:
            _log.error(
                "🚨 gh-pages fetch 失败（attempt %d/%d），父提交退回本地 ref（未经校验）："
                "若远端此刻领先本地，本次仍可能把对方的提交挤成不可达对象",
                attempt, max_attempts,
            )
        has_change, n_changed = ghpages_tree_delta(repo, tree, parent)
        result["tree_unchanged"], result["n_changed"] = not has_change, n_changed
        if not has_change:
            # tree 与此刻的远端真头完全一致：远端已经是我们要发布的状态。
            result["success"] = True
            result["commit"] = parent
            return result
        parent_args = ["-p", parent] if parent else []
        try:
            commit = subprocess.check_output(
                ["git", "commit-tree", tree] + parent_args + ["-m", message_fn(n_changed)],
                cwd=repo,
            ).decode().strip()
        except subprocess.CalledProcessError as e:
            result["last_error"] = f"commit-tree 失败: {e}"
            return result
        subprocess.run(["git", "update-ref", "refs/heads/gh-pages", commit],
                        cwd=repo, check=True)
        result["commit"] = commit
        # 非 force：父提交就是 push 前一刻的远端真头，正常情况下天然快进；
        # 竞态时 git 自己会因非快进拒绝，落入下面的重试分支重新 fetch。
        r = subprocess.run(
            ["git", "push", "origin", f"{commit}:refs/heads/gh-pages"],
            cwd=repo, capture_output=True, text=True,
        )
        if r.returncode == 0:
            result["success"] = True
            return result
        result["last_error"] = (r.stderr or "").strip()[:300]
        if attempt < max_attempts:
            delay = min(2.0 * (2 ** (attempt - 1)), 16.0)
            _log.warning(
                "gh-pages push 被拒 (attempt %d/%d)：%s —— %.0fs 后重新 fetch + 提交 + 推送",
                attempt, max_attempts, result["last_error"], delay,
            )
            _t.sleep(delay)
    return result


def verify_cdn_deployment(reporter, data_root: str,
                           max_wait: int = 180, poll_interval: int = 15) -> bool:
    """Push 成功后轮询 CDN，验证 dashboard-data.json 已更新。

    纯 advisory — 超时只记 WARNING，不回滚/阻塞。

    数据根迁移阶段 4：`data_root` 现在是**数据根**（`PATHS.home`），不再是
    git 仓库根。此前这里读 `dashboard-data.json` 走的是 `reporter.agent_helper.
    git.repo_path`（git 仓库根）——它与数据根今天恰好同目录（`ALPHA_HIVE_HOME`
    未设，`GitHubTool.repo_path` 与 `PATHS.home` 兜底到同一个 `__file__`），
    掩盖了"报告数据所在目录"和"git plumbing 的仓库根"是两个不同概念这件事。
    阶段 5 把 `ALPHA_HIVE_HOME` 改指 `~/alpha-hive-data` 后两者会分叉——
    `dashboard-data.json` 跟数据走，读它必须走 `PATHS.home`，不能再读
    git 仓库根（那里阶段 5 之后不会再有这份文件）。
    """
    import json as _json_v
    import time as _time_v
    import urllib.request

    try:
        import os as _os_v
        dj_path = _os_v.path.join(data_root, "dashboard-data.json")
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
    """用 git plumbing 构建仅含静态文件的 gh-pages 提交并推送。

    数据根迁移阶段 4：`repo`（git 仓库根，= `reporter.agent_helper.git.
    repo_path`，经 `agent_toolbox.GitHubTool` 已改读 `PATHS.git_repo_root`）
    与 `data_root`（数据根，`PATHS.home`，找待部署报告文件）自本版起是**两个
    独立变量**。今天两者仍可能同目录（`ALPHA_HIVE_HOME`/`ALPHA_HIVE_GIT_REPO`
    都未设时，两者各自兜底到 `__file__` 派生的同一个仓库根），阶段 5 之后
    `data_root` 会搬到 `~/alpha-hive-data`、`repo` 留在原检出位置——git
    plumbing（`hash-object`/`write-tree`/`commit-tree`/`push`）全部在 `repo`
    执行（需要 `.git/`），但 `os.listdir` 找文件、`hash-object` 读文件内容
    都改用 `data_root`（`git hash-object -w <绝对路径>` 不要求文件在仓库
    工作区内，只是读字节写 blob，`cwd=repo` 只决定写进哪个仓库的对象库）——
    **例外**：`CODE_SHIPPED_STATIC_ASSETS` 里的文件本就不会出现在 `data_root`，
    找不到时退回 `repo` 读（见该常量定义处的注释）。
    """
    import subprocess
    import os
    import resource as _resource
    from hive_logger import PATHS as _PATHS_ghp
    # 预防 Too many open files：确保 fd 上限至少 2048
    try:
        _soft, _hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
        if _soft < 2048:
            _resource.setrlimit(_resource.RLIMIT_NOFILE, (min(2048, _hard), _hard))
    except (ValueError, OSError):
        pass
    repo = reporter.agent_helper.git.repo_path or "."
    data_root = str(_PATHS_ghp.home)
    idx = os.path.join(repo, ".git", "gh-pages-index")
    if os.path.exists(idx):
        os.remove(idx)
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = idx
    # ── D2: 部署文件白名单 ──
    import re as _re_deploy
    _CORE_FILES = {
        "index.html", "dashboard-data.json", "manifest.json",
        "sw.js", "rss.xml",
    } | CODE_SHIPPED_STATIC_ASSETS
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
    file_source: Dict[str, str] = {}
    for f in os.listdir(data_root):
        # 非交易日（周末/假日）幽灵报告不部署（_CORE 文件无日期，永不被过滤）
        if f not in _CORE_FILES and _fnt_dep(f):
            continue
        if f in _CORE_FILES:
            files.append(f)
            file_source[f] = data_root
        elif _ml_pat.match(f):
            # 所有 ML 增强报告（不再限制天数，index.html 历史板块需要全部文件）
            files.append(f)
            file_source[f] = data_root
        elif f.startswith("alpha-hive-daily-") and f.endswith((".json", ".md")):
            # 当日+历史 daily 报告（JSON + MD）
            files.append(f)
            file_source[f] = data_root
    # ⚠️ v0.45.310 修复：「无静态文件可部署」的守卫必须在这里、在随代码发布的
    # 静态资源并入之前判——`.nojekyll`/`chart.umd.min.js` 几乎总能在 `repo`
    # 里找到（它们是随仓库提交的），若把守卫挪到并入之后，data_root 整个空掉
    # （配置错误 / 阶段 5 迁移中 / 上游扫描没写出任何东西）时 `files` 也不会
    # 是空的——guard 形同虚设，会把 gh-pages 整棵重建成只剩这两个文件，等于
    # 清空线上网站（v0.45.305 review 实测复现：确认此前版本会造成这个后果）。
    if not files:
        _log.warning("无静态文件可部署（数据根 %s 未发现任何报告文件）", data_root)
        return
    # 随代码发布的静态资源：data_root 里没找到的，退回 git 仓库根读
    # （v0.45.305：此前这里只看 data_root，阶段 5 后 `.nojekyll`/
    # `chart.umd.min.js` 会从此在线上消失，见 CODE_SHIPPED_STATIC_ASSETS 注释）。
    _repo_fallback = resolve_code_shipped_asset_sources(data_root, repo, file_source)
    for asset, src in _repo_fallback.items():
        files.append(asset)
        file_source[asset] = src
    _missing_code_assets = CODE_SHIPPED_STATIC_ASSETS - file_source.keys()
    if _missing_code_assets:
        # v0.45.310：升级到 error + 🚨——纯 warning 在这个仓库的 Slack 精简
        # 规则下不会触达任何人，`verify_cdn_deployment` 也只查
        # dashboard-data.json 的时间戳、不查静态资源存不存在，等于没有任何
        # 「谁会红」的观测点（见 CLAUDE.md「这个失败，下游怎么知道？」一节）。
        _log.error(
            "🚨 随代码发布的静态资源缺失，本次部署不含 %s（data_root=%s 与 "
            "git_repo_root=%s 都没有）——线上可能出现 Chart.js 脚本 404 / "
            "Service Worker cache.addAll 失败",
            sorted(_missing_code_assets), data_root, repo,
        )
    # 批量写入 blob + index（逐个 hash-object，但用 stdin 批量 update-index）。
    # hash-object 读的是 file_source[f] 下的绝对路径（内容可能不在 repo 工作区
    # 内），写进树里的路径名（index-info 第三列）仍是裸文件名 f——发布出去的
    # gh-pages 树结构不变，只是内容的物理来源换了。
    cache_entries = []
    for f in sorted(files):
        try:
            blob = subprocess.check_output(
                ["git", "hash-object", "-w", os.path.join(file_source[f], f)], cwd=repo
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

    def _msg_fn(n_changed: int) -> str:
        _msg = f"Deploy: Alpha Hive static {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        if n_changed >= 0:
            _msg += f" [{n_changed} files changed]"
        return _msg

    # v0.45.268：父提交/推送改经 `commit_and_push_gh_pages`——每次重试都重新
    # fetch `origin/gh-pages` 当父提交、非 force 推送，取代旧的
    # 「本地 ref 当父 + --force」（见该函数 docstring 的实测 bug 记录）。
    _push = commit_and_push_gh_pages(repo, tree, _msg_fn)
    if os.path.exists(idx):
        os.remove(idx)
    # 修复 Bug #21：gh-pages push 成功/失败都记录到持久化 queue，
    # 防止"连续网络差时中间几天的 dashboard 永久丢失"
    # v0.45.260（数据根迁移阶段 2）：此前落在 `repo`（git 仓库根），
    # 而它是纯本地审计日志（`.gitignore` 已忽略，从不参与 git 提交）——
    # 与 git plumbing 无关，理应跟 `PATHS.logs_dir` 走。
    _ghp_queue = str(_PATHS_ghp.logs_dir / ".gh_pages_deploy_log.jsonl")
    try:
        import json as _json_q
        import datetime as _dt_q
        _status = {
            "timestamp": _dt_q.datetime.utcnow().isoformat() + "Z",
            "date_str": reporter.date_str,
            "file_count": len(files),
            "changed_files": _push["n_changed"],   # v0.45.2: 实测值（-1=无法判定）
            "tree_unchanged": _push["tree_unchanged"],
            "status": "success" if _push["success"] else "failed",
            "attempts": _push["attempts"],
            "parent_verified": _push["parent_verified"],  # v0.45.268: fetch 校验过父提交与否
            "last_error": _push["last_error"] or "",
        }
        with open(_ghp_queue, "a", encoding="utf-8") as _qf:
            _qf.write(_json_q.dumps(_status, ensure_ascii=False) + "\n")
    except Exception as _qe:
        _log.debug("gh-pages deploy log write failed: %s", _qe)

    if _push["success"]:
        _log.info(
            "gh-pages 部署成功 (%d 静态文件, attempt %d, commit %s)",
            len(files), _push["attempts"], (_push["commit"] or "")[:7],
        )
        # ── D4: 部署后 CDN 验证 ──
        verify_cdn_deployment(reporter, data_root)
    else:
        _log.error(
            "gh-pages push 失败 (所有 %d 次尝试用尽): %s\n"
            "→ 失败已记录到 %s，下次扫描会重新 fetch 最新远端头再提交（不再是无脑 --force）\n"
            "→ 如需紧急修复：检查 %s 确认 pending 日，必要时手动重跑扫描",
            _push["attempts"], _push["last_error"], _ghp_queue, _ghp_queue,
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


def _git_modified_files(git) -> Tuple[Optional[List[str]], Optional[str]]:
    """`(工作区改动清单, None)`；`git status` 失败时 `(None, 原因)` 并打 warning。

    ⚠️ 必须把「失败」与「干净」分开。v0.45.210 前调用方写的是
    `if status.get("modified_files"): … else: log("工作目录干净")`，而
    `GitHubTool.status()` 失败时返回的是 `{"error": …}` —— 于是
    `git status` 挂了会被报成「无需提交（工作目录干净）」。
    v0.45.225 起原因一并返回：此前它只进 warning 日志，`results["git_commit"]`
    （⇒ status.json ⇒「日报提交失败」告警的原因栏）里只有一句 "git status failed"。
    """
    status = git.status()
    if "modified_files" not in status:
        reason = status.get("error") or "（无错误输出）"
        _log.warning("git status 失败，无法判断工作区改动：%s", reason)
        return None, reason
    return status["modified_files"], None


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
        modified, _ = _git_modified_files(git)
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
    modified, status_error = _git_modified_files(git)
    if modified is None:
        results["git_commit"] = {"success": False, "error": f"git status 失败：{status_error}"}
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
        # v0.45.223：提交前工作区里有几个日报产物待提交。`commit()` 对「没东西可提交」也回
        # success=False，靠它区分「无害」与「产物留在工作区没进 git」（如残留 .git/index.lock
        # 让 add 全部失败）。后者此前不可见；v0.45.214 起本地落后时推送还会报 nothing_to_push 成功。
        commit_result["pending_artifacts"] = len(modified) - len(_skipped)
        # v0.45.227：提交完再看一次工作区。「提交成功」≠「日报产物全进了 git」——别的进程短暂占着
        # 索引锁时只挂一条 add，提交照样成功（当天 index.html 没进 git、零告警）。判结果而不是列举原因：
        # 哪种原因漏的都会留在这里。None = 这次 git status 失败，不知道（告警侧记为未执行的检查）。
        after, _ = _git_modified_files(git)
        if after is None:
            commit_result["left_artifacts"] = None
        else:
            left = [f for f in after if _is_report_artifact(f)]
            commit_result["left_artifacts"] = len(left)
            if left:
                commit_result["left_sample"] = left[:5]
                _log.warning("提交后仍有 %d 个日报产物没进 git：%s", len(left), ", ".join(left[:10]))
        results["git_commit"] = commit_result
        results["skipped_non_artifacts"] = _skipped
        if commit_result["success"]:
            _log.info("Git commit 成功（new，白名单）")
        else:
            _log.warning("Git commit 失败：%s", commit_result.get('message'))
    else:
        _log.info("无需提交（工作目录干净）")

    # 2. Git 推送 origin main
    # v0.45.214：本地 main 落后 origin/main（各 session 从 worktree 直推）时，
    # 在对象层合并后再推，不动工作区——部署之后编排器还要跑别的 Python 步骤。
    # 此前直推 `git push origin main`，2026-09-01~11 六次 non-fast-forward 被拒。
    _log.info("Git push → [🧠 生产] (LLM=%s, Swarm=%s)", _using_llm, _is_swarm)
    push_result = production_sync.push_main(git, merge_label=today_commit_msg)
    results["git_push"] = push_result
    results["deploy_env"] = "production"
    if push_result["success"]:
        _log.info("Git push 成功 → %s（%s%s）", push_result.get("remote"),
                  push_result.get("integration"),
                  f"，合并 {push_result['merge_commit'][:7]}，本地 main 落后 {push_result.get('behind')}"
                  if push_result.get("merge_commit") else "")
    else:
        # `error` 与 `output` 对应 run_git_cmd 的两种失败形状，只读一个会把另一种的原因丢成空串
        _log.warning("Git push 失败（%s）：%s%s", push_result.get("integration"),
                     push_result.get("error") or push_result.get("output") or "（git 无输出）",
                     f"\n  （推送前 fetch 也失败：{push_result['fetch_error']}）"
                     if push_result.get("fetch_error") else "")

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

