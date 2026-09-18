"""BearBee 读板 miss 率审计（v0.45.288 的复量尺）—— 只读，不写任何产物。

口径：`agent_details.BearBeeContrarian.details.data_sources` 是 Bear 自记录的读板标记。
  · ml / guard / catalyst：读到才写键 ⇒ 键缺席 = 没读到
  · options：读到写 "real"；走回落写 "options_api"
只统计「上游确实发布过」的行（agent_details 里该蜂无 error 且 discovery 非空）。

⚠️ insider / news **不在此表**：它们按设计也会回落（Scout 没有内幕金额时本来就走 SEC；
news 在 v0.40.0 前的旧标签是 finviz_api），回落 ≠ 读丢，会把 miss 率虚高。
所以另给一个下界：上游方向 bearish 时，读板路径必然给出 ≥ 阈值的分（Oracle 5.0），
记录值低于阈值 ⇒ 必然没读到。

用法（默认从 PATHS.home 找 `.swarm_results_*.json`；生产快照在主 checkout）：
    /usr/local/bin/python3 experiments/bear_read_miss_audit.py --root "/Users/igg/Desktop/Alpha Hive"

修复前（v0.45.288 之前）默认口径（08-24 起满名单日 540 行）的参照值：options 21.7% /
ml 18.0% / guard 32.6% / catalyst 38.0%，方向 bearish 的 Oracle 下界 65.4%（34/52）。
修复后满名单日应降到 ≈0；仍非 0 就去查是不是
上游根本没发布（本脚本已排除）或读到了陈货（>3600s 被拒）。
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict

# v0.45.294：按 `python3 experiments/bear_read_miss_audit.py` 跑时 sys.path[0] 是 experiments/ 而
# 不是仓库根。v0.45.288 新写本脚本时在 `main()` 里惰性 `from hive_logger import PATHS`，没有本行；
# `--help` 与文档示例（显式给 `--root`）都正常，只有缺省 root 才 ModuleNotFoundError。
# （代码位置，`__file__` 正确。）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

FIX_DATE ="2026-09-18"   # v0.45.288 首个可能生效的扫描日（09-18 14:00 PDT 扫描前上线）
KEYS = (("options", "OracleBeeEcho"), ("ml", "RivalBeeVanguard"),
        ("guard", "GuardBeeSentinel"), ("catalyst", "ChronosBeeHorizon"))


def _published(ad, agent):
    a = ad.get(agent)
    return isinstance(a, dict) and not a.get("error") and bool(a.get("discovery"))


def _is_miss(key, ds):
    return ds.get(key) != "real"


def collect(root, since, min_tickers):
    rows = []   # (date, n_tickers, {key: miss?}, oracle_bearish_below_floor?)
    for f in sorted(glob.glob(os.path.join(root, ".swarm_results_*.json"))):
        date = re.search(r"(\d{4}-\d{2}-\d{2})", f).group(1)
        if date < since:
            continue
        d = json.load(open(f, encoding="utf-8"))
        if not isinstance(d, dict) or len(d) < min_tickers:
            continue
        for v in d.values():
            ad = (v or {}).get("agent_details") or {}
            bear = ad.get("BearBeeContrarian") or {}
            det = bear.get("details") or {}
            ds = det.get("data_sources")
            if bear.get("error") or not isinstance(ds, dict):
                continue
            miss = {k: _is_miss(k, ds) for k, agent in KEYS if _published(ad, agent)}
            orc = ad.get("OracleBeeEcho") or {}
            floor = None
            if _published(ad, "OracleBeeEcho") and orc.get("direction") == "bearish" and "options_bear" in det:
                floor = (det.get("options_bear") or 0.0) < 5.0 - 1e-9
            rows.append((date, len(d), miss, floor))
    return rows


def _fmt(rows):
    out = {}
    for k, _ in KEYS:
        t = sum(1 for r in rows if k in r[2])
        m = sum(1 for r in rows if r[2].get(k))
        out[k] = f"{m}/{t}" + (f" ({m / t * 100:4.1f}%)" if t else "")
    fl = [r[3] for r in rows if r[3] is not None]
    out["oracle_bearish_floor"] = f"{sum(fl)}/{len(fl)}" + (f" ({sum(fl) / len(fl) * 100:4.1f}%)" if fl else "")
    return out


def main():
    ap = argparse.ArgumentParser(description="BearBee 读板 miss 率审计（只读）")
    ap.add_argument("--root", default=None, help="放 .swarm_results_*.json 的目录（默认 PATHS.home）")
    ap.add_argument("--since", default="2026-08-24", help="起始日期（默认 08-24：满名单 30 只的口径）")
    ap.add_argument("--min-tickers", type=int, default=25, help="只看当日标的数 ≥ 此值的日子（默认 25）")
    args = ap.parse_args()
    root = args.root
    if root is None:
        from hive_logger import PATHS
        root = str(PATHS.home)

    rows = collect(root, args.since, args.min_tickers)
    print(f"root={root}  since={args.since}  min_tickers={args.min_tickers}  rows={len(rows)}"
          f"  days={len({r[0] for r in rows})}")
    if not rows:
        print("无可用行（目录不对、或该区间没有满名单日）——无法判定，不是「没有 miss」")
        return 3

    for label, sel in (("修复前", [r for r in rows if r[0] < FIX_DATE]),
                       ("修复后", [r for r in rows if r[0] >= FIX_DATE])):
        print(f"\n=== {label}（{'< ' if label == '修复前' else '>= '}{FIX_DATE}）行 {len(sel)}"
              f"，日 {len({r[0] for r in sel})} ===")
        if not sel:
            print("  （暂无——等首个满名单扫描日落盘后再跑）")
            continue
        for k, v in _fmt(sel).items():
            print(f"  {k:22s} miss {v}")

    print("\n=== 逐日（近 14 个日子）===")
    byday = defaultdict(list)
    for r in rows:
        byday[r[0]].append(r)
    for date in sorted(byday)[-14:]:
        f = _fmt(byday[date])
        print(f"  {date}  标的{byday[date][0][1]:>3d}  "
              + "  ".join(f"{k}={f[k].split(' ')[0]}" for k, _ in KEYS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
