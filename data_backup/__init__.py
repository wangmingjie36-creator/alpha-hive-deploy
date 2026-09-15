"""数据根迁移 · 阶段 3 —— 备份上线。

这个包是「代码」，不是「数据」：它操作的对象是 `~/alpha-hive-data`（数据根）
与生产仓库根下的若干 SQLite 库/状态目录，但脚本本体随代码仓库
`alpha-hive-deploy` 走版本控制、可被 `tests/` 覆盖（判据见项目 CLAUDE.md
「这个路径指向代码还是数据」一节）。

模块：
- `sqlite_readonly`：源库只读打开策略（WAL+有 -wal → mode=ro；WAL 无 -wal →
  mode=ro&immutable=1；rollback → mode=ro），照抄
  `~/alpha-hive-data/_pre_migration_snapshots/20260914_055601/make_pre_migration_snapshot.py`
  的 `db_open_uri()` 实现，不重新发明。
- `export`：按表把 SQLite 库导成 SQL 文本 + 原样拷贝 `*_state/` 等文本状态文件。
- `scan_secrets`：导出后、提交前的密钥字面量扫描（命中即拒绝提交）。
- `restore`：从导出产物重建一个新的 SQLite 库 + 还原状态文件目录（恢复演练/异地恢复用）。
- `run_backup`：编排导出→扫描→提交→推送，是阶段 3.5「编排器集成设计」的可运行原型——
  **本次只手动调用它跑通本地裸仓库演练，未接入 `alpha-hive-orchestrator.sh`**。
"""
