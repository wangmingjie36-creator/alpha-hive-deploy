#!/usr/bin/env python3
"""薄入口：给编排器 `run_step` 用（同 `run_data_backup.py` 的理由：`run_step` 只接受脚本文件路径）。

用法：`/usr/local/bin/python3 run_data_migrations.py [--dry-run] [--out FILE]`；逻辑全在 `data_migrations`。
"""
import sys

from data_migrations.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
