"""`deep_analysis.py` 的报告落数据根，不落代码目录（v0.45.322，数据根迁移阶段 5 前置）。

旧实现 `main()` 先 `chdir(脚本目录)` 再按相对文件名写 `deep-*.html` ⇒ 报告进代码仓库。
阶段 5 后 gh-pages 只从数据根（`PATHS.home`）取文件，写进代码目录的报告永远上不了网站。
这里把 `ALPHA_HIVE_HOME` 指向 tmp，走一遍 `--json` 模式，断言文件出现在 tmp、没出现在 cwd。
"""
import json
import os
import sys

import deep_analysis


def test_json_mode_writes_report_under_data_root(tmp_path, monkeypatch):
    data_root = tmp_path / "data_root"
    data_root.mkdir()
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    (cwd_dir / "in.json").write_text(json.dumps({"ticker": "zzz"}), encoding="utf-8")

    monkeypatch.setenv("ALPHA_HIVE_HOME", str(data_root))
    monkeypatch.chdir(cwd_dir)
    # main() 会 chdir 到脚本目录（CLI 语义，另有测试锁住）——这里截掉，让相对的 --json 在 cwd_dir 解析，
    # 同时保证：若报告仍按相对路径写，它会落在 cwd_dir，下面第二条断言就会红。
    monkeypatch.setattr(os, "chdir", lambda _p: None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(deep_analysis, "generate_html", lambda _d: "<html></html>")
    monkeypatch.setattr(sys, "argv", ["deep_analysis.py", "--json", "in.json"])

    deep_analysis.main()

    written = list(data_root.glob("deep-ZZZ-*.html"))
    assert len(written) == 1, f"数据根里应恰有一份报告，实际：{sorted(p.name for p in data_root.iterdir())}"
    assert not list(cwd_dir.glob("deep-*.html")), "报告不许再按相对路径写进当前目录"
