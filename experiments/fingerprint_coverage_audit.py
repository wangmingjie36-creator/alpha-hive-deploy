#!/usr/bin/env python3
"""输入层指纹覆盖扩大（v0.45.671）的窗口审计：新圈进来的依赖在维度 IC 窗口里变过没有。

v0.45.671 起 `signal_code_fingerprints` 的单元指纹 = 定位代码 + 依赖闭包（config 项 / 别的模块 / 同文件辅助函数）。
旧守卫只看定位代码，依赖在窗口（2026-10-12 起）里变了不会红——H1 可能已经吸收了没人判过的输入层改动。
本脚本在每个扫描日实际跑的提交上（编排器日志「生产代码同步」反推，见 `SCAN_DAY_COMMITS`）用**现行定义**重算
各单元的依赖，逐日比对，列出每一处变化及其间的提交，供人逐条判（与 ACKS 里已有的确认对账）。

用法：/usr/local/bin/python3 experiments/fingerprint_coverage_audit.py [--out 证据.json]
只读 git 历史；不碰数据根。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import signal_code_fingerprints as S  # noqa: E402

#: 扫描日 → 生产实际跑的提交（`~/.claude/logs/orchestrator-<日期>.log` 的「生产代码同步」行；10-15 为 up_to_date 沿用前一天）
SCAN_DAY_COMMITS = {
    "2026-10-12": "0993efd", "2026-10-13": "c39baa2", "2026-10-14": "fb3a8d4", "2026-10-15": "fb3a8d4",
    "2026-10-16": "54c3d41", "2026-10-19": "4745d85", "2026-10-20": "b362d55", "2026-10-21": "58a9fe5",
    "2026-10-22": "78af0d0", "2026-10-23": "00d4f5b", "2026-10-26": "07356f3", "2026-10-27": "ddc6229",
}


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout


def _reader(sha: str):
    cache: dict = {}

    def read(rel: str) -> str:
        if rel not in cache:
            r = subprocess.run(["git", "-C", str(ROOT), "show", f"{sha}:{rel}"], capture_output=True, text=True)
            if r.returncode != 0:
                cache[rel] = None
            else:
                cache[rel] = r.stdout
        if cache[rel] is None:
            raise S.FingerprintError(f"{sha} 里没有 {rel}")
        return cache[rel]
    return read


def deps_at(sha: str) -> dict:
    """该提交上各单元的依赖：{单元: {标签: 文本哈希}}；定位代码在那时还不存在的文件跳过（那些根的变化已由当时的 ACK 判过）。"""
    read = _reader(sha)
    src = S._Sources(read)
    saved = S.UNITS
    try:
        S.UNITS = {u: {**spec, "code": tuple((rel, loc) for rel, loc in spec["code"] if src.exists(rel))}
                   for u, spec in saved.items()}
        out = {}
        for unit in S.UNITS:
            try:
                deps = S.unit_dependencies(unit, _src=src)
                out[unit] = {k: hashlib.sha256(v.encode()).hexdigest()[:12] for k, v in deps.items()}
            except S.FingerprintError as e:
                out[unit] = {"<error>": str(e)}
        return out
    finally:
        S.UNITS = saved


def commits_between(a: str, b: str, path: str) -> list:
    rel = path.split("::")[0]
    log = _git("log", "--format=%h %s", f"{a}..{b}", "--", rel)
    return [ln[:160] for ln in log.splitlines()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    days = list(SCAN_DAY_COMMITS)
    snaps = {d: deps_at(_git("rev-parse", SCAN_DAY_COMMITS[d]).strip()) for d in days}
    changes = []
    for prev, cur in zip(days, days[1:]):
        a, b = SCAN_DAY_COMMITS[prev], SCAN_DAY_COMMITS[cur]
        if a == b:
            continue
        for unit in S.UNITS:
            pa, pb = snaps[prev].get(unit, {}), snaps[cur].get(unit, {})
            for label in sorted(set(pa) | set(pb)):
                if pa.get(label) == pb.get(label):
                    continue
                kind = "added" if label not in pa else "removed" if label not in pb else "changed"
                changes.append({"unit": unit, "from_day": prev, "to_day": cur, "from": a, "to": b,
                                "label": label, "kind": kind, "commits": commits_between(a, b, label)})
    errors = {d: {u: v["<error>"] for u, v in s.items() if "<error>" in v} for d, s in snaps.items()}
    res = {"generated_by": "experiments/fingerprint_coverage_audit.py", "scan_day_commits": SCAN_DAY_COMMITS,
           "n_deps_last_day": {u: len(v) for u, v in snaps[days[-1]].items()},
           "errors": {d: e for d, e in errors.items() if e}, "changes": changes}
    text = json.dumps(res, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    for c in changes:
        print(f"{c['unit']:<16} {c['from_day']}→{c['to_day']} {c['kind']:<8} {c['label']}")
        for ln in c["commits"][:6]:
            print(f"      {ln}")
    print("errors:", res["errors"] or "无")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
