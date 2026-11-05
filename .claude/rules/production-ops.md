---
paths:
  - "production_clone.py"
  - "production_sync.py"
  - "deploy_orchestrator.py"
  - "scripts/**"
  - "tests/_orchestrator.py"
  - "tests/test_orchestrator*.py"
  - "tests/test_production_*.py"
  - "tests/test_deploy_orchestrator.py"
  - "tests/test_dev_checkout_sync.py"
---

# 生产克隆与编排器部署 · 按路径加载

> 读 / 改上面 `paths:` 匹配的文件时 Claude Code 自动加载。v0.45.727 从根目录 `CLAUDE.md` **逐字**搬来（「核心组件指针」两条全文）。不许改生产克隆 / 部署副本、判版本命令、回滚方法在根目录也留了。

- **生产代码 = 独立克隆 `~/alpha-hive-prod`（阶段 8，v0.45.431）**，不是 `~/Desktop/Alpha Hive`（那里是**开发检出**，worktree 都挂在它下面）。克隆只由扫描前 `production_sync` 快进；它的 `.git/hooks` 拒绝一切让 main 偏离 origin/main 的写入（`production_clone.py`，守卫每轮自动补齐、不合格进 P1）。⚠️ **不要在生产克隆里开会话、改文件、提交**；改代码 = 开发检出开 worktree → 推 origin/main → 下一个扫描日自动进生产。判「下一轮生产跑哪版」：`git -C ~/alpha-hive-prod fetch -q origin && git -C ~/alpha-hive-prod rev-list --left-right --count main...origin/main`（**refs 不再与开发仓库共享**，在开发 worktree 里查到的 main 不是生产的）。体检：`/usr/local/bin/python3 production_clone.py check`。守卫 `tests/test_production_clone.py`、`tests/test_orchestrator_runs_production_clone.py`。开发检出根启动的会话（含周 / 月定时任务）由 SessionStart 钩子先快进 main（`scripts/dev_checkout_sync.py`，配置 `.claude/settings.json`；干净且只落后才动、merge 从不被杀，给出 CLAUDE.md / `.claude/` 的变化，v0.45.467）；克隆里的 `origin/cloud-snapshots` 由读者自己 `cloud_snapshot_loader.refresh()`，每日扫描后台观测落后交易日数 → P2（v0.45.466）
- **编排器（v0.45.353 起受版本控制）**：唯一真相 = 仓库 `scripts/alpha-hive-orchestrator.sh`；launchd 跑的是**部署副本** `~/.claude/scripts/alpha-hive-orchestrator.sh`（路径不变：Desktop 有 TCC 限制）。⚠️ **不许直接改部署副本**——改仓库那份 → 合入 origin/main → **下一个扫描日自动部署**（v0.45.370 阶段 3：production_sync 结局 OK 后调 `deploy_orchestrator.py --ref HEAD`；关卡 / 漂移拒绝 / 原子替换在工具里；同步没成功那轮 skipped；结局在 status.json `steps_result.orchestrator_deploy`，记录 `~/.claude/logs/orchestrator_deploy.json`）。⚠️ 部署那轮仍跑旧编排器——新编排器从合入后**第二个**扫描日起执行，与 Python 耦合的改动要兼容一轮。首次上线 / 急用才手动：`/usr/local/bin/python3 deploy_orchestrator.py --ref origin/main --dry-run`，再去掉 `--dry-run` 加 `--out ~/.claude/logs/orchestrator_deploy.json`。**回滚 = 在 main 上 revert 那次合入**（下一扫描日自动部署）；手动部署旧提交只管一轮、下一轮会被自动部署盖回 main 最新版。⚠️ 坏编排器若在部署块**之前**就崩（如同步前的裸变量），它部署不了自己的修复 ⇒ 手动部署修好的 main。一致性守卫（漂移 / 该部署没部署；合入后待下一轮且 ≤6 天不算红）= `tests/test_orchestrator_deployed_matches_repo.py`；读编排器原文的测试一律经 `tests/_orchestrator.py`，CI 上也跑
