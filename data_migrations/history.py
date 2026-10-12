"""补记的历史手工修复（阶段 7，v0.45.419）。

2026-10-06 盘点：运行器出现之前，生产数据被手工修复过约 13 次，**只留下备份文件 / CHANGELOG 文字，库里查不到**。
这里把它们登记成 `kind="historical"` 的记录，第一次 `run_pending(apply=True)` 时写进 `applied.jsonl`（按 id 幂等）。

⚠️ 诚实边界：
  - 这些**不是**迁移，没有可重放的脚本；记录只说「发生过、证据在哪」，不假装可回滚。
  - 日期 / 行数取自 CHANGELOG 文字与盘点子任务，**未逐条回溯核对**；唯一当场核过的是备份文件是否还在
    （`backup_main_file_present`，2026-10-06 现场 `ls`）。三份早期备份只剩 `-wal`/`-shm` 孤儿、主文件不在
    （这正是 v0.45.233 修的清理 bug 的后果）——它们已不能用于恢复。
  - 加新条目 = 追加到 `HISTORICAL` 末尾；已登记的 id 不许改号（`applied.jsonl` 是只追加的）。
"""
from __future__ import annotations

HISTORY_SOURCE = "盘点子任务 + CHANGELOG 文字（2026-10-06）；日期/行数未逐条回溯核对，仅备份文件存在性当场核过"

HISTORICAL: list[dict] = [
    {"id": "H001", "date": "2026-08-27", "what": "08-26 的 30 行 price_at_predict 从冻结快照恢复",
     "evidence": ["db_backups/pheromone_pre_price_restore_20260827_002008.db", "logs/price_close_backfill.log"],
     "backup_main_file_present": False},
    {"id": "H002", "date": "2026-08-25", "what": "migrate_ambiguous_backfill 首次真跑（ambiguous_* 列 + 重算 correct_*）",
     "evidence": ["db_backups/pheromone_pre_P0_tolerance_fix_2026-08-25.db"], "backup_main_file_present": False},
    {"id": "H003", "date": "2026-08-26", "what": "close_correction 相关手工写入（备份在；确切动作未查）",
     "evidence": ["db_backups/pheromone_pre_close_correction_20260826_1456.db"], "backup_main_file_present": False},
    {"id": "H004", "date": "2026-09-03", "what": "纸面组合回滚到 08-27 并重放 08-28~09-02（NaN 修复）",
     "evidence": ["paper_portfolio_state.bak-nanfix-20260903（代码检出根目录）"], "backup_main_file_present": True},
    {"id": "H005", "date": "2026-09-07", "what": "ML 模型文件损坏后手工恢复",
     "evidence": ["ml_model.corrupted-2026-09-07T0130.json", "ml_model_cache.pre-restore-2026-09-07.json（代码检出根目录）"],
     "backup_main_file_present": True},
    {"id": "H006", "date": "2026-09-09", "what": "一次性脚本把 08-28 批 30 行重置为 checked_t7=0（脚本未保存）",
     "evidence": ["db_backups/pheromone_pre_v0.45.173_forming_bar_reset_20260909_090711.db"], "backup_main_file_present": True},
    {"id": "H007", "date": "2026-09-15", "what": "fund.* 信号 signal_archive 回填（约 2149 行 REPLACE）",
     "evidence": ["db_backups/pheromone_pre_v0.45.250_fund_backfill_20260915_101954.db"], "backup_main_file_present": True},
    {"id": "H008", "date": "2026-09-15", "what": "signal_archive 口径搬迁（加 90 行、删 90 行；一次性脚本未保存，回滚 SQL 只在 CHANGELOG）",
     "evidence": ["_manual_backups/pheromone.db.bak_v0.45.256_census_move_20260915_102010"], "backup_main_file_present": True},
    {"id": "H009", "date": "2026-09-18", "what": "fear_greed 历史回填（约 1541 + 40 行；脚本 backfill_fear_greed_legacy.py 在）",
     "evidence": ["backfill_fear_greed_legacy.py"], "backup_main_file_present": None},
    {"id": "H010", "date": "2026-09-17", "what": "swarm 顺序重放后 UPDATE predictions 30 行（v0.45.271；scratchpad 脚本已丢）",
     "evidence": ["CHANGELOG v0.45.271"], "backup_main_file_present": None},
    {"id": "H011", "date": "2026-09-18", "what": "快照隔离（v0.45.266）前的整库备份；对应脚本未找到",
     "evidence": ["db_backups/pheromone_pre_v0.45.266_snapshot_quarantine_20260918_025745.db"], "backup_main_file_present": True},
    {"id": "H012", "date": "2026-09-28", "what": "BRK-B 两行 entry_price_backfill --apply（逐行来源记在 close_correction_source 列）",
     "evidence": ["_manual_backups/pheromone.db.bak-entry-backfill-20260928-091413"], "backup_main_file_present": True},
    {"id": "H013", "date": "2026-09-23", "what": "09-23 期权快照 JSON 手工改写（v0.45.372 / 379 / 387；旧值记在各文件 _iv_rank_recheck）",
     "evidence": ["_manual_backups/options_snapshot_*_2026-09-23.pre_v0.45.*.json"], "backup_main_file_present": True},
]
