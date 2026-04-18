# Alpha Hive DB Snapshots

用于保存 **里程碑级别** 的 `pheromone.db` 基线，供未来回溯验证。

## 为什么存在这个目录

`pheromone.db` 在 `.gitignore` 里（`*.db`），因为日常扫描会高频增长。
但某些关键版本（如 bug 修复后）的 DB 状态值得冻结作为"可复现基线"。

## 命名约定

```
pheromone_v<VERSION>_<DESCRIPTOR>_<YYYY-MM-DD>.db
```

- `<VERSION>` — 对应 CHANGELOG.md 的版本号（如 `v0.23.2`）
- `<DESCRIPTOR>` — 短名词描述本次快照的含义（如 `gap_aware`、`sprint1_backfill`）
- `<YYYY-MM-DD>` — 快照日期（ISO-8601）

## 如何创建新快照

```bash
# 1. 复制当前 DB
cp pheromone.db "db_snapshots/pheromone_vX.Y.Z_<descriptor>_YYYY-MM-DD.db"

# 2. Force-add（绕过 .gitignore 的 *.db 规则）
git add -f "db_snapshots/pheromone_vX.Y.Z_<descriptor>_YYYY-MM-DD.db"

# 3. 在 commit message 里记录：样本数、关键修复、验证指标
git commit -m "DB snapshot vX.Y.Z: <descriptor>"
```

## 如何从快照恢复

```bash
# 查看所有快照
ls -lh db_snapshots/

# 恢复到某个基线（会覆盖当前 DB！先备份）
cp pheromone.db pheromone.db.backup
cp db_snapshots/pheromone_v0.23.2_gap_aware_2026-04-17.db pheromone.db
```

## 当前快照清单

| 文件 | 版本 | 说明 | 样本数 |
|------|------|------|--------|
| `pheromone_v0.23.2_gap_aware_2026-04-17.db` | v0.23.2 | v0.21 gap-aware backtester + per-ticker SL + neutral 15% 保护 全量回填后的基线。SL 触发时能正确反映开盘跳空（min -15.5%），而非钉在 -5% 阈值。 | 280 total / 210 checked_t7 / 76 checked_t30 |

## 保留策略

- 只保留**里程碑版本**（大 bug 修复后、重要统计验证前）
- 过期快照（>6 个月且被后续快照覆盖）可删除
- 禁止每天自动快照（会让 repo 膨胀）
