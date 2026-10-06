"""`/usr/local/bin/python3 -m data_migrations [--dry-run] [--out FILE]`——编排器与手工共用的入口。

退出码：0 = status ok；1 = 其它（红）。**无论如何都把结果 JSON 写到 stdout，`--out` 给了也写文件。**
"""
import argparse
import json
import sys

from data_migrations.runner import run_pending


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报待办，不备份不写库不写记录")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    try:
        rep = run_pending(apply=not a.dry_run, log=lambda s: print(s, file=sys.stderr))
    except Exception as e:  # noqa: BLE001
        rep = {"status": "crash", "error": f"{type(e).__name__}: {e}"}
    text = json.dumps(rep, ensure_ascii=False)
    print(text)
    if a.out:
        try:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError as e:
            print(f"⚠️ 结果写不进 {a.out}: {e}", file=sys.stderr)
    return 0 if rep.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
