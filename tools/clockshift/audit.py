"""拨钟审计：同一台机器上先跑真实钟基线、再跑拨钟，只报「拨钟红而基线绿」的差集（v0.45.495）。

    /usr/local/bin/python3 tools/clockshift/audit.py --days 7 --days 45 -- -m "not integration and not network"

`--` 之后原样传给 pytest（选择哪些测试由调用方决定）。本脚本自己追加 `--maxfail=0 -rfE -p no:cacheprovider`。

**为什么是差分**：基线里本来就红的（如 `TestCoverageHorizon` 的到期告警、环境性失败）在拨钟下照样红，
它们不是「到某天才会炸」的东西；只有「今天绿、未来某天红」才是时间炸弹。差集里的每一条再各重跑一次
（拨钟下仍红 **且** 真实钟下仍绿）才算确认，其余记为 flaky、只列出不判红。

**谁会红**：任何一轮 pytest 跑崩（退出码不是 0/1：收集错误、中断、内部错误、零用例），或一条用例都没跑，
审计直接失败——不允许把「没跑成」报成「0 条新红」。

退出码：0 = 无确认的新红；1 = 有确认的新红；2 = 某一轮没跑成（审计本身无效）。
"""
import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_RESULT_LINE = re.compile(r"^(FAILED|ERROR) (\S.*?)(?: - .*)?$")
_SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$")
_COUNTS = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)")


class RunInvalid(Exception):
    pass


def _env(days):
    env = {k: v for k, v in os.environ.items() if k != "CLOCK_SHIFT_FORCE"}
    parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep)
             if p and os.path.realpath(p) != os.path.realpath(HERE)]
    # 基线**显式写 0**、不是删键：外层若正被 harness 拨着，它的子进程包装「缺了就补」，删掉的键会被补回外层偏移
    # （v0.45.495 实测：嵌套审计的基线因此被拨了 30 天）。显式值包装照用不改。
    env["CLOCK_SHIFT_DAYS"] = str(days or 0)
    if days:
        parts.insert(0, HERE)
    if parts:
        env["PYTHONPATH"] = os.pathsep.join(parts)
    else:
        env.pop("PYTHONPATH", None)
    return env


def parse(output):
    """从 pytest 的 `-rfE` 摘要里取失败 / 错误的 nodeid 集合，以及各类计数。

    **只认 `short test summary info` 段之后的行**：失败用例的「Captured stdout」里也可能有 `FAILED x` 开头的行
    （v0.45.495 实测：本仓 harness 自测里嵌套审计的输出被当成 nodeid，重跑时 pytest 退出码 4）。"""
    bad = set()
    lines = output.splitlines()
    start = next((i for i, ln in enumerate(lines) if _SUMMARY_HEADER.match(ln.strip())), len(lines))
    for line in lines[start + 1:]:
        m = _RESULT_LINE.match(line.rstrip())
        if m:
            bad.add(m.group(2).strip())
    tail = "\n".join(output.splitlines()[-5:])
    counts = {k.rstrip("s") if k.startswith("error") else k: int(n) for n, k in _COUNTS.findall(tail)}
    return bad, counts


def run_pytest(days, pytest_args, label, log_dir=None):
    cmd = [sys.executable, "-m", "pytest", *pytest_args, "--maxfail=0", "-rfE", "-p", "no:cacheprovider"]
    if days:
        cmd += ["-p", "clockshift"]
    print(f"\n=== [{label}] CLOCK_SHIFT_DAYS={days or 0}: {' '.join(cmd[3:])}", flush=True)
    proc = subprocess.run(cmd, env=_env(days), capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    if log_dir:
        with open(os.path.join(log_dir, f"{label}.log"), "w", encoding="utf-8") as f:
            f.write(out)
    bad, counts = parse(proc.stdout)
    print("\n".join(out.splitlines()[-3:]), flush=True)
    ran = sum(counts.get(k, 0) for k in ("passed", "failed", "error", "xfailed", "xpassed"))
    if proc.returncode not in (0, 1) or ran == 0:
        raise RunInvalid(f"[{label}] pytest 退出码 {proc.returncode}、实际跑了 {ran} 条——这一轮没跑成，审计无效。"
                         f"输出末尾：\n" + "\n".join(out.splitlines()[-15:]))
    if proc.returncode == 1 and not bad:
        raise RunInvalid(f"[{label}] pytest 报失败却解析不出任何 FAILED/ERROR 行——摘要格式变了？")
    return bad, counts


def audit(days_list, pytest_args, rerun=True, log_dir=None):
    base_bad, base_counts = run_pytest(0, pytest_args, "baseline", log_dir)
    results = []
    for days in days_list:
        shift_bad, counts = run_pytest(days, pytest_args, f"shift{days:+g}", log_dir)
        new = sorted(shift_bad - base_bad)
        confirmed, flaky = new, []
        if new and rerun:
            again_bad, _ = run_pytest(days, [*_selection_free(pytest_args), *new], f"shift{days:+g}-rerun", log_dir)
            base_again, _ = run_pytest(0, [*_selection_free(pytest_args), *new], f"baseline-rerun{days:+g}", log_dir)
            confirmed = [n for n in new if n in again_bad and n not in base_again]
            flaky = [n for n in new if n not in confirmed]
        results.append({"days": days, "counts": counts, "confirmed": confirmed, "flaky": flaky,
                        "gone": sorted(base_bad - shift_bad)})
    return base_bad, base_counts, results


def _selection_free(pytest_args):
    """重跑时只保留选项（`-m ...` 等），去掉位置参数（文件 / 目录），换成要重跑的 nodeid。"""
    out, skip = [], False
    takes_value = {"-m", "-k", "-p", "-o", "--rootdir", "-c", "--timeout"}
    for i, a in enumerate(pytest_args):
        if skip:
            out.append(a)
            skip = False
            continue
        if a.startswith("-"):
            out.append(a)
            skip = a in takes_value
    return out


def render(base_bad, base_counts, results):
    lines = ["## 拨钟审计（tools/clockshift）", "",
             f"真实钟基线：{base_counts}；基线已红 {len(base_bad)} 条（不计入——它们由 tests.yml 负责，列在这里免得看不见）："]
    lines += [f"- `{n}`" for n in sorted(base_bad)]
    lines.append("")
    for r in results:
        head = f"### +{r['days']:g} 天：确认的新红 {len(r['confirmed'])} 条"
        lines += [head, f"计数：{r['counts']}"]
        lines += [f"- 🔴 `{n}`" for n in r["confirmed"]]
        if r["flaky"]:
            lines += ["重跑不复现（记为 flaky，不判红）："] + [f"- `{n}`" for n in r["flaky"]]
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, action="append", required=True, help="偏移天数，可多次给")
    ap.add_argument("--no-rerun", action="store_true", help="不重跑差集（默认重跑以滤 flaky）")
    ap.add_argument("--log-dir", help="各轮 pytest 完整输出写到这里")
    ap.add_argument("pytest_args", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    pytest_args = a.pytest_args[1:] if a.pytest_args[:1] == ["--"] else a.pytest_args
    if any(d == 0 for d in a.days):
        ap.error("--days 不能是 0（那就是基线本身）")
    if a.log_dir:
        os.makedirs(a.log_dir, exist_ok=True)
    try:
        base_bad, base_counts, results = audit(a.days, pytest_args, rerun=not a.no_rerun, log_dir=a.log_dir)
    except RunInvalid as e:
        print(f"\n拨钟审计无效：{e}", file=sys.stderr)
        return 2
    report = render(base_bad, base_counts, results)
    print("\n" + report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    return 1 if any(r["confirmed"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
