#!/usr/bin/env python3
"""用 `origin/cloud-snapshots` 里目标日的 CBOE 链，补那一天缺失的期权快照 + IV 索引（v0.45.497）。

**为什么有它。** 10-12 扫描全程 TLS EOF（v0.45.496），期权快照 0 份；当天收盘后云端 routine 抓到了 29/30 只
（缺 BILI）。现成读者只有 `alpha_hive_daily_report.py --swarm --date D`，那是**整天全量重扫**——10-12 的
report_snapshots 已被纸面组合消费，重扫会改写已执行决策的输入（10-05 事故）。本工具只补期权快照这一层：

  · 产物：`cache/options_snapshot_{T}_{D}_backfilled-{跑批会话}.json`（系统既有的补跑槽位，8 月有 3 批先例）。
    `_SNAP_RE` 读者（price_history / iv_history 迁移 / earnings_vol / vrp 的 record_day）**认不出**这个名字；
    有意读它的只有 `vrp_signal._snapshot_rv`（取 rv_30d）。
  · `cache/iv_history_{T}.jsonl` 追加一行 `{"date": D, "iv": iv_raw_observed}`（读者按日期排序去重）。
  · 不碰 pheromone.db / signal_archive / 报告 / 纸面组合 / 卖权账本 / straddle 账本。

**同一份 `OptionsAgent.analyze`，在隔离里跑。** 不另写一套指标口径。隔离的理由逐条：
  · 缓存目录用临时目录：fetcher 先读链缓存再走快照供给器（生产 cache 里若有当天的实时链会被直接读走）；
    「最后有效 IV」/ hist_hv 缓存也都落在临时目录，不碰生产。
  · IV 历史预置 = 生产索引里 **< D** 的行（与 D 当天正常扫描时看到的一致：先追加当日、再读历史）。
    不截的话，今天扫描已写进的 10-13 会进 IV Rank 的分母。
  · yfinance 日线截到 ≤ D（`period="1y"` 现拉会含 D 之后的 K 线 ⇒ HV 代理口径的 IV Rank 混进未来）。
  · rv_30d 只认 `twelve_data@D`（`calculate_iv_rv_spread` 的 TD 路径认 `ALPHA_HIVE_TARGET_DATE`，yfinance 兜底不认）；
    期限结构只认 `cboe`（即快照里的）。其余来源 ⇒ 置空并在 `_backfill.nulled` 写明原因，不拿别的日子冒充。

**自证（任一不过 ⇒ 该标的不写）：** 快照标签 `_options_source == "cloud_snapshot"` 且 `_options_as_of_mismatch is False`
（v0.45.497：只有链就是目标日那一场时 analyze 才这么标）；`_snapshot_stock_price` 等于云端 `price_at_fetch`。

**默认 dry-run**：只在临时目录里算，打印将写入的内容；结束时比对生产 cache 目录指纹（名字 + 大小 + mtime），
变了就退出码 3。`--apply` 才写生产；已有常规快照或已补过的标的跳过（幂等）。

    /usr/local/bin/python3 experiments/backfill_options_snapshot_from_cloud.py --date 2026-10-12
    /usr/local/bin/python3 experiments/backfill_options_snapshot_from_cloud.py --date 2026-10-12 --apply
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TOOL_VERSION = "0.45.497"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _fingerprint(d: Path) -> dict:
    out = {}
    for p in d.iterdir():
        try:
            st = p.stat()
        except OSError:
            continue
        out[p.name] = (st.st_size, st.st_mtime_ns)
    return out


def _seed_iv_index(prod_cache: Path, tmp: Path, ticker: str, date: str) -> int:
    """生产索引里 < date 的行原样抄进临时目录；返回抄了几行。并写迁移标记，免得它去扫（不存在的）快照。"""
    import iv_history
    src = prod_cache / f"iv_history_{ticker}.jsonl"
    n = 0
    if src.exists():
        with src.open(encoding="utf-8") as fi, (tmp / src.name).open("w", encoding="utf-8") as fo:
            for line in fi:
                try:
                    if json.loads(line).get("date", "9999") < date:
                        fo.write(line)
                        n += 1
                except ValueError:
                    continue
    Path(iv_history._migration_marker(ticker, str(tmp))).write_text("seeded by backfill tool\n")
    return n


def _index_has(prod_cache: Path, ticker: str, date: str) -> bool:
    p = prod_cache / f"iv_history_{ticker}.jsonl"
    if not p.exists():
        return False
    with p.open(encoding="utf-8") as f:
        for line in f:
            try:
                if json.loads(line).get("date") == date:
                    return True
            except ValueError:
                continue
    return False


def _as_of_yfinance(real_yf, date: str):
    """yfinance 的代理模块：`Ticker(t).history(...)` 截到 ≤ date，其余属性原样转发。"""
    import datetime as _dt
    cutoff = _dt.date.fromisoformat(date)

    class _AsOfTicker:
        def __init__(self, t):
            self._t = real_yf.Ticker(t)

        def history(self, *a, **k):
            h = self._t.history(*a, **k)
            if h is None or getattr(h, "empty", True):
                return h
            return h[[ts.date() <= cutoff for ts in h.index]]

        def __getattr__(self, name):
            return getattr(self._t, name)

    proxy = types.ModuleType("yfinance_as_of")
    proxy.__getattr__ = lambda name: getattr(real_yf, name)   # PEP 562
    proxy.Ticker = _AsOfTicker
    return proxy


def _honest_fixups(snap: dict, date: str) -> list:
    """价格派生量里证明不了「截到 D」的，置空；返回置空清单（写进 `_backfill.nulled`）。"""
    nulled = []
    det = snap.get("iv_rv_detail") or {}
    src = det.get("source")
    if snap.get("rv_30d") is not None and src != f"twelve_data@{date}":
        nulled.append(f"rv_30d/iv_rv_*: 来源 {src!r} 不是 twelve_data@{date}，窗口不能证明截到目标日")
        snap["rv_30d"] = None
        snap["iv_rv_spread"] = None
        snap["iv_rv_signal"] = "unknown"
        snap["iv_rv_detail"] = {"data_available": False, "error": "补跑：RV 来源不能证明截到目标日", "source": src}
    ts = snap.get("iv_term_structure") or {}
    if ts.get("data_available") and ts.get("source") != "cboe":
        nulled.append(f"iv_term_structure: 来源 {ts.get('source')!r} 不是快照里的 cboe")
        snap["iv_term_structure"] = {"shape": "unknown", "term_structure": [], "front_iv": None, "back_iv": None,
                                     "iv_spread": None, "data_available": False, "source": "none",
                                     "error": "补跑：期限结构不是目标日快照里的"}
    return nulled


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", required=True)
    ap.add_argument("--tickers", nargs="*")
    ap.add_argument("--apply", action="store_true", help="写生产（缺省 dry-run）")
    args = ap.parse_args(argv)
    if not _DATE_RE.match(args.date):
        print(f"--date 格式非法：{args.date!r}")
        return 2
    D = args.date

    import cloud_snapshot_loader as csl
    import iv_history
    import options_analyzer as oa
    from hive_logger import PATHS

    prod_cache = Path(PATHS.cache_dir)
    session, _ = oa._snapshot_session(oa._snapshot_now())
    if session <= D:
        print(f"此刻会话 {session} 不晚于目标日 {D}：那一场还能（或刚）正常扫描，不该用补跑槽位。退出。")
        return 2

    rf = csl.refresh()
    print(f"cloud-snapshots 刷新：ok={rf['ok']} newest={rf['newest_date']} {rf.get('error') or ''}")
    man = csl.load_manifest(D)
    if man is None:
        print(f"{D} 无云端 manifest。退出。")
        return 2
    cloud_ok = [t for t in (man.get("ok") or []) if isinstance(t, str)]
    wanted = [t.upper() for t in args.tickers] if args.tickers else cloud_ok
    missing_cloud = [t for t in wanted if t not in cloud_ok]

    todo, skipped = [], {}
    for t in wanted:
        if t in missing_cloud:
            skipped[t] = "云端快照里没有"
        elif (prod_cache / f"options_snapshot_{t}_{D}.json").exists():
            skipped[t] = "生产已有常规快照"
        elif list(prod_cache.glob(f"options_snapshot_{t}_{D}_backfilled-*.json")):
            skipped[t] = "生产已有补跑快照"
        else:
            todo.append(t)
    print(f"目标日 {D}｜跑批会话 {session}｜云端 {len(cloud_ok)} 只｜待补 {len(todo)}｜跳过 {len(skipped)}")
    for t, why in sorted(skipped.items()):
        print(f"  跳过 {t}: {why}")
    if not todo:
        return 0

    before = _fingerprint(prod_cache)
    tmp = Path(tempfile.mkdtemp(prefix=f"opt_backfill_{D}_"))
    seeded = {t: _seed_iv_index(prod_cache, tmp, t, D) for t in todo}

    os.environ["ALPHA_HIVE_TARGET_DATE"] = D
    os.environ.pop("OPTIONS_SNAPSHOT_DISABLE", None)
    real_yf = oa.yf
    if real_yf is not None:
        oa.yf = _as_of_yfinance(real_yf, D)

    results, rejected = {}, {}
    try:
        with csl.snapshot_mode(D):
            agent = oa.OptionsAgent()
            agent.fetcher = oa.OptionsDataFetcher(cache_dir=str(tmp))
            for t in todo:
                cloud = csl.load_ticker(D, t) or {}
                try:
                    agent.analyze(t)
                except Exception as e:  # noqa: BLE001 —— 记下、继续下一只，结尾汇总
                    rejected[t] = f"analyze 抛异常 {type(e).__name__}: {e}"
                    continue
                files = list(tmp.glob(f"options_snapshot_{t}_{D}_backfilled-*.json"))
                if len(files) != 1:
                    rejected[t] = f"临时目录里补跑快照 {len(files)} 份（应为 1）"
                    continue
                snap = json.loads(files[0].read_text(encoding="utf-8"))
                why = []
                if snap.get("_options_source") != "cloud_snapshot" or snap.get("_options_as_of_mismatch") is not False:
                    why.append(f"链不是目标日云端快照（_options_source={snap.get('_options_source')!r}, "
                               f"mismatch={snap.get('_options_as_of_mismatch')!r}）")
                try:
                    px_ok = abs(float(snap.get("_snapshot_stock_price")) - float(cloud.get("price_at_fetch"))) < 1e-6
                except (TypeError, ValueError):
                    px_ok = False
                if not px_ok:
                    why.append(f"现价 {snap.get('_snapshot_stock_price')!r} ≠ 云端 {cloud.get('price_at_fetch')!r}")
                if why:
                    rejected[t] = "；".join(why)
                    continue
                nulled = _honest_fixups(snap, D)
                snap["_backfill"] = {
                    "tool": "experiments/backfill_options_snapshot_from_cloud.py", "version": TOOL_VERSION,
                    "target_date": D, "run_session": session,
                    "cloud_vintage": cloud.get("vintage_date"), "cloud_fetched_at_utc": cloud.get("fetched_at_utc"),
                    "cloud_last_trade_time_et": cloud.get("last_trade_time_et"),
                    "cloud_price_source": cloud.get("price_source"),
                    "iv_history_seeded_rows": seeded[t], "price_history_cutoff": D, "nulled": nulled,
                }
                results[t] = (files[0].name, snap)
    finally:
        oa.yf = real_yf

    print()
    print(f"{'标的':<6}{'现价':>10}{'IV观测':>8}{'P/C':>7}{'总OI':>12}{'IVRank':>8}{'来源':>10}{'RV30':>7}  置空")
    for t, (_, s) in sorted(results.items()):
        def f(v, fmt):
            return format(v, fmt) if isinstance(v, (int, float)) else "—"
        print(f"{t:<6}{f(s.get('_snapshot_stock_price'), '10.2f')}{f(s.get('iv_raw_observed'), '8.2f')}"
              f"{f(s.get('put_call_ratio'), '7.2f')}{f(s.get('total_oi'), '12,.0f')}{f(s.get('iv_rank'), '8.1f')}"
              f"{str(s.get('iv_rank_source') or '—'):>10}{f(s.get('rv_30d'), '7.1f')}  {len(s['_backfill']['nulled'])}")
    for t, why in sorted(rejected.items()):
        print(f"  ✗ {t}: {why}")
    nulled_all = [(t, n) for t, (_, s) in sorted(results.items()) for n in s["_backfill"]["nulled"]]
    for t, n in nulled_all:
        print(f"  置空 {t}: {n}")

    if not args.apply:
        after = _fingerprint(prod_cache)
        shutil.rmtree(tmp, ignore_errors=True)
        if after != before:
            changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            print(f"\n❌ dry-run 期间生产 cache 有变化（{len(changed)} 个）：{changed[:10]}")
            print("   可能是并发写入者（扫描 / Alpha Bot），也可能是本工具漏了隔离——核实前不要 --apply。")
            return 3
        print(f"\ndry-run：将写 {len(results)} 份快照 + {sum(1 for _, s in results.values() if s.get('iv_raw_observed') is not None)} "
              f"行 IV 索引；生产 cache 目录指纹未变。加 --apply 写入。")
        return 1 if rejected else 0

    wrote, idx = 0, 0
    for t, (name, snap) in sorted(results.items()):
        oa._write_snapshot(str(prod_cache / name), snap, indent=2)
        wrote += 1
        iv = snap.get("iv_raw_observed")
        if iv is not None and not _index_has(prod_cache, t, D):
            if iv_history.append_observation(t, str(prod_cache), D, iv):
                idx += 1
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n已写 {wrote} 份快照、{idx} 行 IV 索引（{D}）。拒绝 {len(rejected)} 只。")
    return 1 if rejected else 0


if __name__ == "__main__":
    sys.exit(main())
