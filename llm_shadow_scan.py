#!/usr/bin/env python3
"""LLM 影子 A/B 入口（v0.45.555）：同一天、同一批输入，开着 LLM 再跑一遍蜂群，**只记录、不进评分**。

为什么存在
----------
想知道「LLM 臂是否比规则臂好」，历史重跑回答不了（输入不是当时的快照；模型知识截止 2026-06 有前视偏差），
只能前瞻：每天并行记一份 LLM 版，攒够样本再比。生产扫描永远 `--no-llm`（编排器有守卫钉死），所以这条
路必须是**独立入口**，且自己带硬上限。

它怎么保证不碰生产
------------------
· 独立数据根（缺省 `$HOME/alpha-hive-llm-shadow`，与生产根互不嵌套）。每次运行先用 SQLite 备份 API
  只读拷一份生产 `pheromone.db`、再拷一份 `cache/`（含 IV 历史与期权快照——否则两条臂的 IV Rank 等输入就不同了），
  然后 `ALPHA_HIVE_HOME` 指向影子根、**之后**才 import 蜂群。生产根只被读。
· 只调 `run_swarm_scan`，不调 `save_report` / `deploy_and_notify` / Slack / 账本发布；扫描产物都落影子根。
· 比较所需的结果自己存：`<影子根>/shadow_status/<日期>.json`（逐只标的的最终分 / 方向 / 规则分 / 蒸馏模式 +
  用量与质量计数）与 `swarm_results-<日期>.json` 全文。影子库每次重拷，**不**累积——所以别指望从它的库里读历史。

为什么是两个进程
----------------
`config.py` 在 **import 时**就把 `PATHS.db`、`PATHS.home / "cache"`、日志路径等固化成模块级常量。
若在已 import 过 `config` 的进程里再改 `ALPHA_HIVE_HOME`，这些常量仍指向**生产**——扫描会把预测写进生产库。
所以父进程只做计划与拷贝（它 import 的 `config` 只读，永不扫描），扫描放进一个**全新的子进程**，
子进程启动时环境变量就已指向影子根；子进程开扫前还会逐项核对 `config` 里没有任何值指向生产根。

两条臂是什么（⚠️ `rule_score` 不是规则臂）
------------------------------------------
· LLM 臂 = 本扫描的 `final_score` / `direction`。
· 规则臂 = **生产当天自己的规则引擎结果**（生产库同日同标的）。生产收盘后 14:00 才跑、本入口应在它**之后**跑，
  价格静止、`cache/` 也已被生产更新过，两边输入一致。读者见 `llm_shadow_report.py`，协议见 `experiments/llm_shadow_prereg.md`。
· `swarm_results` 里的 `rule_score` 由各蜂的维度分算出，而蜂在 LLM 模式下已把 LLM 混进了自己的分数（OracleBee 以 0.6
  权重混入并可改写方向），Queen 的 LLM 只是再加一层——所以它**只**隔离 Queen 那一层，只用于描述性分解，不是规则臂。

花钱的闸（三道，缺一不开）
-----------------------
1. 缺省是 `--plan`：只打印计划（标的、预计费用、路径），不拷贝、不扫描、不花钱。
2. `--run` 才会调 LLM，且必须同时设环境变量 `ALPHA_HIVE_LLM_SHADOW_SPEND=1`（防误触；定时任务同样要显式设）。
3. 每日硬上限 `--daily-cap`（缺省 $3）：`llm_service` 调用前按「输入估算 + max_tokens×输出单价」预检并预留在途费用，
   超了就拒绝（该蜂回落规则引擎）并计入 `budget_refusals`。**30 只跑不满**：精简后 Opus 5.5 低 effort 约 $0.118/只
   （2026-10-19 实测分项），$3 约够 22 只 ⇒ 每天轮换覆盖一个子集，不让上限在当天中途随机截掉后几只。

`--control`：LLM 关闭、其余同 `--run`，免费；用来核对影子根的输入复现度（它的 `final_score` 应与同日生产一致；
须在生产 14:00 扫描**之后**跑，否则没有同日生产值可比）。

退出码：0 健康；2 用法 / 未批准 / 当天已有付费记录；3 扫描异常（已记状态）；5 跑完了但 LLM 臂不健康（有预算 / 定价拒绝，
或截断率 / JSON 失败率 / 请求失败率 / 蒸馏未走 LLM 的比例超阈值）——**不健康的一天不要拿去比**。

一天只许付费跑一次（v0.45.560）：每日硬上限只在 `llm_service` 进程内计，同一天再起一个 `--run` 就是第二份预算。
所以 `--run` 起飞前先写占位 `shadow_status/<日期>.json`（子进程崩了它就留在那儿、按不健康日计），已有该文件即拒绝；
`--control` 写 `<日期>.control.json` 与 `swarm_results-<日期>.control.json`，不会覆盖付过费的记录。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

MODEL = "claude-opus-5-5"
DAILY_CAP_USD = 3.0
# 精简后 Opus 5.5 低 effort 每只约 $0.117（2026-10-19：输入 11.6K×$4/MTok + 输出 3.5K×$20/MTok），留约 15% 余量
PER_TICKER_USD = 0.135
SPEND_ENV = "ALPHA_HIVE_LLM_SHADOW_SPEND"
SHADOW_DIRNAME = "alpha-hive-llm-shadow"
STATUS_DIRNAME = "shadow_status"
# 「不健康」阈值：超过即这一天的 LLM 臂不算数
MAX_FAIL_RATE = 0.10
MIN_QUEEN_LLM_RATE = 0.80
EXIT_OK, EXIT_USAGE, EXIT_SCAN_ERROR, EXIT_UNHEALTHY = 0, 2, 3, 5


# ==================== 纯函数（有单测） ====================

def max_tickers(cap_usd: float, per_ticker_usd: float = PER_TICKER_USD) -> int:
    """在 cap 内最多能放几只（向下取整）。"""
    if cap_usd <= 0 or per_ticker_usd <= 0:
        return 0
    return int(cap_usd // per_ticker_usd)


def pick_tickers(universe: Sequence[str], day: date, k: int) -> List[str]:
    """从全名单里按日期轮换取连续 k 只（环形）。k >= 全名单 ⇒ 全取。

    起点 = (日期序数 × k) mod n：相邻日起点错开 k 个位置，所以长期看每只被覆盖的天数大致相同。
    不用随机数（可复现、可事后核对）；覆盖是否均匀不影响逐只标的的成对比较，且实际覆盖名单记在状态文件里。
    """
    uni = list(universe)
    n = len(uni)
    if k <= 0 or n == 0:
        return []
    if k >= n:
        return uni
    start = (day.toordinal() * k) % n
    return [uni[(start + i) % n] for i in range(k)]


def default_shadow_root(prod_root: Path) -> Path:
    """缺省影子根：生产数据根的**同级**目录（不在生产根里 ⇒ 不被数据备份 / 根目录闸扫到，也不在 iCloud 桌面里）。"""
    return Path(prod_root).parent / SHADOW_DIRNAME


def check_roots(prod_root: Path, shadow_root: Path) -> None:
    """生产根与影子根必须互不相同、互不嵌套，否则「只读生产」的承诺不成立。"""
    p = Path(prod_root).resolve()
    s = Path(shadow_root).resolve()
    if p == s or p in s.parents or s in p.parents:
        raise ValueError(f"影子根 {s} 与生产根 {p} 相同或互相嵌套，拒绝运行")


def quality_report(usage: Dict, per_ticker: Dict[str, Dict], *, llm_on: bool) -> Dict:
    """由 llm_service 用量计数 + 逐只结果判这一天的 LLM 臂健不健康。control 模式（LLM 关）恒健康但标明。"""
    calls = int(usage.get("call_count", 0) or 0)
    trunc = int(usage.get("truncated_calls", 0) or 0)
    jfail = int(usage.get("json_parse_failures", 0) or 0)
    bref = int(usage.get("budget_refusals", 0) or 0)
    uref = int(usage.get("unpriced_refusals", 0) or 0)
    failed = int(usage.get("failed_calls", 0) or 0)          # 发出了请求却没拿到响应（call_count 只数成功的）
    attempts = calls + failed
    n_t = len(per_ticker)
    queen_llm = sum(1 for v in per_ticker.values() if v.get("distill_mode") == "llm_enhanced")
    rates = {
        "truncation_rate": round(trunc / calls, 4) if calls else None,
        "json_parse_failure_rate": round(jfail / calls, 4) if calls else None,
        "failed_call_rate": round(failed / attempts, 4) if attempts else None,
        "queen_llm_rate": round(queen_llm / n_t, 4) if n_t else None,
    }
    reasons: List[str] = []
    if llm_on:
        if bref:
            reasons.append(f"budget_refusals={bref}（部分蜂回落规则引擎）")
        if uref:
            reasons.append(f"unpriced_refusals={uref}（模型未登记定价）")
        if calls == 0:
            reasons.append("call_count=0（LLM 一次都没调成）")
        if rates["truncation_rate"] is not None and rates["truncation_rate"] > MAX_FAIL_RATE:
            reasons.append(f"truncation_rate={rates['truncation_rate']:.0%} > {MAX_FAIL_RATE:.0%}")
        if rates["json_parse_failure_rate"] is not None and rates["json_parse_failure_rate"] > MAX_FAIL_RATE:
            reasons.append(f"json_parse_failure_rate={rates['json_parse_failure_rate']:.0%} > {MAX_FAIL_RATE:.0%}")
        if rates["failed_call_rate"] is not None and rates["failed_call_rate"] > MAX_FAIL_RATE:
            reasons.append(f"failed_call_rate={rates['failed_call_rate']:.0%} > {MAX_FAIL_RATE:.0%}"
                           f"（{failed} 次请求没拿到响应，对应的蜂回落规则引擎）")
        if rates["queen_llm_rate"] is not None and rates["queen_llm_rate"] < MIN_QUEEN_LLM_RATE:
            reasons.append(f"queen_llm_rate={rates['queen_llm_rate']:.0%} < {MIN_QUEEN_LLM_RATE:.0%}（蒸馏没走 LLM）")
    return {**rates, "healthy": not reasons, "reasons": reasons, "llm_on": llm_on}


def per_ticker_from_swarm_results(swarm_results: Dict, tickers: Sequence[str]) -> Dict[str, Dict]:
    """从 `.swarm_results_<日期>.json` 抽出比较所需的逐只字段（缺的写 None，不写 0）。"""
    out: Dict[str, Dict] = {}
    for tk in tickers:
        v = swarm_results.get(tk)
        if not isinstance(v, dict):
            out[tk] = {"present": False}
            continue
        out[tk] = {
            "present": True,
            "final_score": v.get("final_score"),
            "direction": v.get("direction"),
            "rule_score": v.get("rule_score"),
            "rule_direction": v.get("rule_direction"),
            "distill_mode": v.get("distill_mode"),
            "llm_confidence": v.get("llm_confidence"),
            "data_real_pct": v.get("data_real_pct"),
        }
    return out


def build_status(*, day: date, mode: str, model: Optional[str], cap: float, requested: Sequence[str],
                 usage: Dict, per_ticker: Dict[str, Dict], elapsed: float, error: Optional[str]) -> Dict:
    scanned = [tk for tk, v in per_ticker.items() if v.get("present")]
    llm_on = mode == "run"
    return {
        "schema": 1,
        "date": day.isoformat(),
        "mode": mode,
        "model": model if llm_on else None,
        "daily_cap_usd": cap,
        "tickers_requested": list(requested),
        "tickers_scanned": scanned,
        "elapsed_sec": round(elapsed, 1),
        "usage": {k: usage.get(k) for k in (
            "total_cost_usd", "call_count", "input_tokens", "output_tokens",
            "truncated_calls", "json_parse_failures", "budget_refusals", "unpriced_refusals", "failed_calls")},
        "quality": quality_report(usage, per_ticker, llm_on=llm_on),
        "per_ticker": per_ticker,
        "error": error,
        "written_at": datetime.now().isoformat(timespec="seconds"),
    }


# ==================== 有副作用的部分 ====================

def seed_shadow_root(src_db: Path, src_cache: Path, shadow_root: Path) -> None:
    """把生产库与缓存拷进影子根（**只读生产**）。每次重拷：影子根的库 / 缓存永远是「生产此刻的样子」。

    · 库：SQLite 备份 API，源以只读 URI 打开（WAL 里未检查点的页也一并读到，且不会写生产）。
    · cache/：整目录拷（含 iv_history_*.jsonl 与 options_snapshot_*，两条臂的 IV Rank / VRP 输入因此一致）。
    · `shadow_status/` 不动——比较结果存在那里，跨天累积。
    """
    shadow_root.mkdir(parents=True, exist_ok=True)
    dst_db = shadow_root / "pheromone.db"
    for suffix in ("", "-wal", "-shm"):
        try:
            (shadow_root / f"pheromone.db{suffix}").unlink()
        except FileNotFoundError:
            pass
    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dst_db))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    dst_cache = shadow_root / "cache"
    if dst_cache.exists():
        shutil.rmtree(dst_cache)
    if src_cache.exists():
        shutil.copytree(src_cache, dst_cache)


def status_path(shadow_root: Path, day_iso: str, mode: str) -> Path:
    """状态文件路径。**control 与 run 分文件**（v0.45.560）：以前同日同名，晚到的 --control 会把
    已经花了钱的 --run 记录（与 swarm_results 全文）覆盖掉。"""
    suffix = ".json" if mode == "run" else ".control.json"
    return Path(shadow_root) / STATUS_DIRNAME / f"{day_iso}{suffix}"


def swarm_results_path(shadow_root: Path, day_iso: str, mode: str) -> Path:
    suffix = ".json" if mode == "run" else ".control.json"
    return Path(shadow_root) / STATUS_DIRNAME / f"swarm_results-{day_iso}{suffix}"


def started_stub(*, day: date, mode: str, cap: float, requested: Sequence[str]) -> Dict:
    """子进程起飞前先落的占位记录：子进程若中途崩溃 / 被杀，这一天留下的是「不健康、已开始」，
    而不是「什么都没有」——后者会让同一天可以再付一次费（每日硬上限只在进程内计，跨进程要靠这个文件）。"""
    return {
        "schema": 1, "date": day.isoformat(), "mode": mode, "state": "started",
        "model": MODEL if mode == "run" else None, "daily_cap_usd": cap,
        "tickers_requested": list(requested), "tickers_scanned": [], "elapsed_sec": None,
        "usage": {},      # 子进程没写完 ⇒ 花费未知（报告的累计花费会偏低）
        "quality": {"healthy": False, "llm_on": mode == "run",
                    "reasons": ["子进程没有写完状态（崩溃 / 被中断）——按已开始、无可用数据计"]},
        "per_ticker": {}, "error": None, "written_at": datetime.now().isoformat(timespec="seconds"),
    }


def drop_stub(path: Path) -> None:
    """子进程没真正开跑（用法错误、起不来）时撤掉占位，免得一天白白被锁住；不是占位就不动。"""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(doc, dict) and doc.get("state") == "started":
        Path(path).unlink()


def write_status(shadow_root: Path, status: Dict) -> Path:
    import ledger_io
    path = status_path(shadow_root, status["date"], status["mode"])
    path.parent.mkdir(parents=True, exist_ok=True)
    ledger_io.write_json(path, status)
    return path


def default_scan(tickers: List[str]) -> Dict:
    """真正跑蜂群（只在子进程里调用：此时 ALPHA_HIVE_HOME 已指向影子根）。返回 `.swarm_results_<日期>.json` 的内容。"""
    from alpha_hive_daily_report import AlphaHiveDailyReporter
    from hive_logger import PATHS
    reporter = AlphaHiveDailyReporter()
    reporter.run_swarm_scan(focus_tickers=tickers)
    path = Path(PATHS.home) / f".swarm_results_{reporter.date_str}.json"
    if not path.exists():
        raise RuntimeError(f"扫描结束但没有 {path.name}——无从记录结果")
    return json.loads(path.read_text(encoding="utf-8"))


def build_child_env(environ: Dict[str, str], shadow_root: Path, cap: float) -> Dict[str, str]:
    """子进程环境：数据根指向影子根、预算上限交给 config、清掉会把路径指回别处的覆盖变量。

    这是本模块**唯一**写出数据根环境变量的地方（登记在 tests/test_data_root_default.py::ENV_KEY_SITES）。
    """
    env = dict(environ)
    env["ALPHA_HIVE_HOME"] = str(shadow_root)
    env["ALPHA_HIVE_LLM_BUDGET_USD"] = repr(float(cap))
    for k in ("ALPHA_HIVE_DB_PATH", "ALPHA_HIVE_CACHE_DIR", "ALPHA_HIVE_TARGET_DATE", "ALPHA_HIVE_PHEROMONE_DB"):
        env.pop(k, None)
    return env


def frozen_prod_paths(config_module, prod_root: Path) -> List[str]:
    """`config` 里所有指向生产根的字符串 / 路径值（最深看两层字典）。非空 ⇒ 路径在 import 期被固化到了生产。"""
    needle = str(prod_root)
    bad: List[str] = []

    def hit(v) -> bool:
        return isinstance(v, (str, Path)) and needle in str(v)

    for name in dir(config_module):
        if name.startswith("__"):
            continue
        v = getattr(config_module, name, None)
        if hit(v):
            bad.append(name)
        elif isinstance(v, dict):
            for k1, v1 in v.items():
                if hit(v1):
                    bad.append(f"{name}[{k1!r}]")
                elif isinstance(v1, dict):
                    bad.extend(f"{name}[{k1!r}][{k2!r}]" for k2, v2 in v1.items() if hit(v2))
    return bad


def run_child(args: argparse.Namespace, *, scan: Callable[[List[str]], Dict] = default_scan,
              now: Optional[date] = None) -> int:
    """子进程：此时环境已指向影子根。核对隔离 → 扫描 → 写状态。"""
    if args.run and os.environ.get(SPEND_ENV) != "1":
        # 父进程已核对过；这里再核一次，免得有人直接 `--child --run` 绕过花钱闸
        print(f"❌ 拒绝：--run 会调用 LLM 花钱，必须显式设置环境变量 {SPEND_ENV}=1")
        return EXIT_USAGE
    import hive_logger
    shadow_root = Path(args.shadow_root)
    prod_root = Path(args.prod_root)
    got = hive_logger.explicit_data_root()
    if got is None or Path(got).resolve() != shadow_root.resolve():
        raise RuntimeError(f"子进程数据根={got!r}，不是影子根 {shadow_root}——拒绝扫描")
    if Path(hive_logger.PATHS.db).resolve() != (shadow_root / "pheromone.db").resolve():
        raise RuntimeError(f"PATHS.db={hive_logger.PATHS.db} 没有指向影子库——拒绝扫描（会写到别处）")
    import config
    bad = frozen_prod_paths(config, prod_root)
    if bad:
        raise RuntimeError(f"config 里有 {len(bad)} 处路径固化在生产根下（{bad[:5]}）——拒绝扫描")

    import llm_service
    mode = "run" if args.run else "control"
    if float(config.LLM_CONFIG.get("daily_budget_usd", -1)) != float(args.daily_cap):
        raise RuntimeError(f"LLM_CONFIG.daily_budget_usd={config.LLM_CONFIG.get('daily_budget_usd')} "
                           f"≠ --daily-cap {args.daily_cap}——预算上限没传到位，拒绝扫描")
    if mode == "run":
        llm_service.set_model_override(MODEL)
        if not llm_service.is_available():
            print("❌ LLM 不可用（没有 API Key？）——不扫描")
            return EXIT_USAGE
    else:
        llm_service.disable()

    day = date.fromisoformat(args.child_day) if args.child_day else (now or date.today())
    tickers = [t for t in args.child_tickers.split(",") if t]
    t0 = time.time()
    error: Optional[str] = None
    swarm_results: Dict = {}
    try:
        swarm_results = scan(tickers)
    except Exception as e:  # noqa: BLE001 —— 记录进状态文件并以退出码 3 告警，而不是吞掉
        error = f"{type(e).__name__}: {e}"
        print(f"❌ 扫描异常：{error}")
    usage = llm_service.get_usage()
    per_ticker = per_ticker_from_swarm_results(swarm_results, tickers)
    status = build_status(day=day, mode=mode, model=MODEL, cap=args.daily_cap, requested=tickers,
                          usage=usage, per_ticker=per_ticker, elapsed=time.time() - t0, error=error)
    path = write_status(shadow_root, status)
    if swarm_results:
        import ledger_io
        ledger_io.write_json(swarm_results_path(shadow_root, day.isoformat(), mode), swarm_results)
    q = status["quality"]
    print(f"状态已写 {path}")
    print(f"用量 ${usage.get('total_cost_usd', 0):.4f} / {usage.get('call_count', 0)} 次调用 | "
          f"截断 {usage.get('truncated_calls', 0)} | JSON 失败 {usage.get('json_parse_failures', 0)} | "
          f"预算拒绝 {usage.get('budget_refusals', 0)} | 定价拒绝 {usage.get('unpriced_refusals', 0)} | "
          f"请求失败 {usage.get('failed_calls', 0)}")
    print(f"健康 {q['healthy']} {q['reasons'] if q['reasons'] else ''}")
    if error:
        return EXIT_SCAN_ERROR
    return EXIT_OK if q["healthy"] else EXIT_UNHEALTHY


def run_shadow(args: argparse.Namespace, *, environ: Optional[Dict[str, str]] = None,
               now: Optional[date] = None,
               spawn: Callable[..., "subprocess.CompletedProcess"] = subprocess.run) -> int:
    """父进程：计划 → 闸 → 拷贝 → 起子进程。**不扫描**（它 import 的 config 固化的是生产路径）。"""
    env = os.environ if environ is None else environ
    day = now or date.today()
    mode = "plan" if args.plan else ("run" if args.run else "control")

    import hive_logger
    prod_root = Path(hive_logger.resolve_data_root(env))
    src_db = Path(hive_logger.PATHS.db)
    src_cache = Path(hive_logger.PATHS.cache_dir_unmade())
    shadow_root = Path(args.shadow_root) if args.shadow_root else default_shadow_root(prod_root)
    check_roots(prod_root, shadow_root)

    import config
    universe = list(config.WATCHLIST)
    k = args.tickers if args.tickers else max_tickers(args.daily_cap)
    tickers = pick_tickers(universe, day, k)
    est = len(tickers) * PER_TICKER_USD

    print(f"模式 {mode} | 日期 {day} | 模型 {MODEL if mode == 'run' else '（LLM 关）'}")
    print(f"生产根 {prod_root}（只读）→ 影子根 {shadow_root}")
    print(f"标的 {len(tickers)}/{len(universe)}：{' '.join(tickers)}")
    print(f"每日硬上限 ${args.daily_cap:.2f} | 预计 ≈ ${est:.2f}（{PER_TICKER_USD}/只 × {len(tickers)}）")
    if est > args.daily_cap:
        print("⚠️ 预计费用超过上限：上限会在当天中途拦住后几只（它们回落规则引擎）。请减少 --tickers。")
    if mode == "plan":
        return EXIT_OK

    if mode == "run" and env.get(SPEND_ENV) != "1":
        print(f"❌ 拒绝：--run 会调用 LLM 花钱，必须显式设置环境变量 {SPEND_ENV}=1（用户批准后）")
        return EXIT_USAGE
    if not tickers:
        print("❌ 没有可扫描的标的")
        return EXIT_USAGE

    sp = status_path(shadow_root, day.isoformat(), mode)
    if mode == "run" and sp.exists():
        # 每日硬上限由 llm_service 在**进程内**计；同一天再起一个进程就是第二份预算（$3 变 $6）。
        # 所以跨进程的「今天花过没有」只能由这个文件回答——有了就拒绝（删它要人手动，且看得见）。
        print(f"❌ 拒绝：{day} 已有付费记录 {sp}（含子进程中途崩溃留下的占位）。再跑一次会重复花钱并覆盖它；"
              f"确认要重跑请先人工移走该文件。")
        return EXIT_USAGE

    seed_shadow_root(src_db, src_cache, shadow_root)
    if mode == "run":
        write_status(shadow_root, started_stub(day=day, mode=mode, cap=args.daily_cap, requested=tickers))
    cmd = [sys.executable, str(Path(__file__).resolve()), "--child", "--run" if mode == "run" else "--control",
           "--daily-cap", repr(float(args.daily_cap)), "--shadow-root", str(shadow_root),
           "--prod-root", str(prod_root), "--child-tickers", ",".join(tickers),
           "--child-day", day.isoformat()]
    try:
        proc = spawn(cmd, env=build_child_env(dict(env), shadow_root, args.daily_cap),
                     cwd=str(Path(__file__).resolve().parent))
    except BaseException:
        if mode == "run":
            drop_stub(sp)          # 进程根本没起来 ⇒ 没花钱，别锁住这一天
        raise
    if mode == "run" and int(proc.returncode) == EXIT_USAGE:
        drop_stub(sp)              # 子进程在花钱之前就以用法错误退出（如没有 API Key）
    return int(proc.returncode)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="LLM 影子 A/B：只记录、不进评分（详见模块 docstring）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--plan", action="store_true", help="缺省：只打印计划，不拷贝、不扫描、不花钱")
    g.add_argument("--control", action="store_true", help="LLM 关闭的对照扫描（免费）")
    g.add_argument("--run", action="store_true", help=f"开 LLM 扫描（花钱；还需环境变量 {SPEND_ENV}=1）")
    ap.add_argument("--daily-cap", type=float, default=DAILY_CAP_USD, help="每日硬上限（美元），缺省 3.0")
    ap.add_argument("--tickers", type=int, default=0, help="只扫 N 只（缺省按上限自动算）")
    ap.add_argument("--shadow-root", default="", help="影子数据根（缺省生产根的同级 alpha-hive-llm-shadow）")
    # 内部：父进程起子进程用，不是给人用的
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--prod-root", default="", help=argparse.SUPPRESS)
    ap.add_argument("--child-tickers", default="", help=argparse.SUPPRESS)
    ap.add_argument("--child-day", default="", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if not (args.plan or args.control or args.run):
        args.plan = True
    if args.daily_cap <= 0:
        ap.error("--daily-cap 必须 > 0")
    if args.child and not (args.shadow_root and args.prod_root and args.child_tickers):
        ap.error("--child 需要 --shadow-root / --prod-root / --child-tickers")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    return run_child(args) if args.child else run_shadow(args)


if __name__ == "__main__":
    sys.exit(main())
