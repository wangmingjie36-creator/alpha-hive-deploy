#!/usr/bin/env python3
"""薄入口：给编排器 `run_step` 用。v0.45.264 起已接入生产编排器 Step 14。

`~/.claude/scripts/alpha-hive-orchestrator.sh` 的 `run_step` 只接受**脚本文件路径**
（内部 `[ -f "$script" ]` + `"$PYTHON3" "$script" "$@"`），不支持 `python3 -m
包.模块` 调用形式——所以 `data_backup/run_backup.py` 不能直接被 `run_step` 指到，
需要这一层瘦包装。真正的逻辑都在 `data_backup.run_backup`，本文件只转发 argv。

用法（与 `python3 -m data_backup.run_backup` 完全一致）：
    /usr/local/bin/python3 run_data_backup.py --src "..." --backup-dir "..." \
        --remote origin --branch main --status-file "..."
"""
import sys

from data_backup.run_backup import main

if __name__ == "__main__":
    sys.exit(main())
